"""Interface final: link e Baixar. Ferramentas de estudo ficam nos módulos."""
import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from . import service


class LINKV2_Settings(PropertyGroup):
    source_url: StringProperty(name='Link', description='Cole o link do modelo no Sketchfab')
    use_environment: BoolProperty(name='Visual do viewer', default=False,
        description='Traz HDRI, fundo, luzes e pós-processamento do viewer (afeta a iluminação e o render de toda a cena)')
    data_root: StringProperty(name='Pasta de download', subtype='DIR_PATH',
        description='Pasta onde os modelos e as texturas serão baixados',
        default=str(service.default_root()))
    # Mantido para compatibilidade com arquivos do início do v2; não exibido.
    capture_path: StringProperty(subtype='FILE_PATH', options={'HIDDEN'})


class LINKV2_OT_download(Operator):
    bl_idname = 'linkv2.download'
    bl_label = 'Baixar'
    bl_description = 'Baixa e importa o modelo na cena atual'

    def execute(self, context):
        try:
            settings = context.scene.linkv2
            folder = settings.data_root.strip()
            service.download(settings.source_url, bpy.path.abspath(folder) if folder else None, settings.use_environment)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class LINKV2_PT_main(Panel):
    bl_label = 'Sketchfab Link V2'
    bl_idname = 'LINKV2_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Link 3D V2'

    def draw(self, context):
        self.layout.prop(context.scene.linkv2, 'source_url', text='Link')
        self.layout.prop(context.scene.linkv2, 'data_root')
        self.layout.prop(context.scene.linkv2, 'use_environment')
        row = self.layout.row()
        row.enabled = not service.busy()
        row.operator('linkv2.download', text='Baixar')
        if service.busy():
            self.layout.label(text=service.status[:48])
        elif service.status.startswith('Falha: Este modelo não tem download gratuito'):
            self.layout.label(text='Modelo sem download gratuito', icon='ERROR')
            self.layout.label(text='O autor não liberou o download deste modelo.')
        elif service.status.startswith('Falha:'):
            self.layout.label(text='Não foi possível baixar', icon='ERROR')
            self.layout.label(text=service.status[7:].strip()[:60])


CLASSES = (LINKV2_Settings, LINKV2_OT_download, LINKV2_PT_main)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.linkv2 = bpy.props.PointerProperty(type=LINKV2_Settings)


def unregister():
    if hasattr(bpy.types.Scene, 'linkv2'):
        del bpy.types.Scene.linkv2
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
