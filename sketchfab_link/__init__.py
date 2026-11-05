bl_info = {
    'name': 'Link 3D para Blender',
    'author': 'Projeto pessoal',
    'version': (1, 0, 0),
    'blender': (3, 6, 0),
    'location': 'View3D > Sidebar > Link 3D',
    'description': 'Baixa e importa modelos Sketchfab a partir de um link',
    'category': 'Import-Export',
}

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import bpy
from bpy.props import StringProperty
from bpy.types import Operator, Panel, PropertyGroup

_job = None
_status = ''


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _valid_link(value):
    try:
        url = urlparse(value.strip())
        return (url.scheme == 'https' and url.hostname in {'sketchfab.com', 'www.sketchfab.com'}
                and re.search(r'(^|[-/])[0-9a-fA-F]{32}(?:/|$)', url.path) is not None)
    except ValueError:
        return False


def _finish_job():
    global _job, _status
    if _job is None:
        return None
    process, output = _job
    if process.poll() is None:
        return 1.0
    output.seek(0)
    text = output.read().decode('utf-8', errors='replace').strip()
    output.close()
    _job = None
    if process.returncode != 0:
        _status = 'Falhou: ' + (text.splitlines()[-1][:140] if text else 'conversão não concluída')
        _redraw()
        return None
    # --quiet writes only the path to stdout; stderr may include diagnostics.
    candidates = [line.strip() for line in text.splitlines() if line.strip().lower().endswith('.glb')]
    if not candidates or not Path(candidates[-1]).is_file():
        _status = 'Falhou: arquivo GLB não encontrado.'
        _redraw()
        return None
    try:
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=candidates[-1])
        added = set(bpy.data.objects) - before
        _status = f'Pronto: {len(added)} objetos importados.'
    except Exception as exc:
        _status = 'Arquivo baixado; importação falhou: ' + str(exc)[:100]
    _redraw()
    return None


class LINK3D_Settings(PropertyGroup):
    url: StringProperty(name='Link', description='Link do modelo Sketchfab')
    folder: StringProperty(name='Pasta', subtype='DIR_PATH',
                           default=str(Path.home() / 'Downloads' / 'Sketchfab'))


class LINK3D_OT_import(Operator):
    bl_idname = 'link3d.baixar_importar'
    bl_label = 'Baixar e importar'
    bl_description = 'Baixa o modelo e importa o GLB na cena atual'

    @classmethod
    def poll(cls, context):
        return _job is None

    def execute(self, context):
        global _job, _status
        settings = context.scene.link3d_settings
        url = settings.url.strip()
        if not _valid_link(url):
            self.report({'ERROR'}, 'Informe um link HTTPS de um modelo Sketchfab.')
            return {'CANCELLED'}
        node = shutil.which('node')
        if not node:
            self.report({'ERROR'}, 'Instale o Node.js LTS e reinicie o Blender.')
            return {'CANCELLED'}
        try:
            folder = Path(bpy.path.abspath(settings.folder)).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            cli = Path(__file__).resolve().parent / 'sfrip.mjs'
            if not cli.is_file():
                raise FileNotFoundError('Conversor não encontrado no add-on')
            output = tempfile.TemporaryFile()
            options = {'stdout': output, 'stderr': subprocess.STDOUT, 'stdin': subprocess.DEVNULL}
            if os.name == 'nt':
                options['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            try:
                process = subprocess.Popen([node, str(cli), url, '--merge=objects', '--yes',
                                            '--quiet', '--out', str(folder)], **options)
            except Exception:
                output.close()
                raise
            _job = (process, output)
            _status = 'Baixando e convertendo…'
            bpy.app.timers.register(_finish_job, first_interval=1.0)
            _redraw()
        except (OSError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class LINK3D_PT_panel(Panel):
    bl_label = 'Link 3D'
    bl_idname = 'LINK3D_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Link 3D'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.link3d_settings
        layout.prop(settings, 'url', text='Link')
        layout.prop(settings, 'folder', text='Pasta')
        layout.operator('link3d.baixar_importar', icon='IMPORT')
        if _status:
            layout.label(text=_status[:95])
        layout.label(text='Use modelos com licença ou autorização.', icon='INFO')


_classes = (LINK3D_Settings, LINK3D_OT_import, LINK3D_PT_panel)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.link3d_settings = bpy.props.PointerProperty(type=LINK3D_Settings)


def unregister():
    global _job
    if bpy.app.timers.is_registered(_finish_job):
        bpy.app.timers.unregister(_finish_job)
    if _job is not None:
        process, output = _job
        if process.poll() is None:
            process.terminate()
        output.close()
        _job = None
    del bpy.types.Scene.link3d_settings
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
