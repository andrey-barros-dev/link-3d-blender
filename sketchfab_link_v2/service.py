"""Coordenação na thread principal do Blender e importação em cena temporária."""
import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

import bpy

from . import animation, bridge, browser, coverage, environment, importer, postprocess, quality

receiver = None
destination = None
destination_window = None
workers = []
events = queue.Queue()
last_result = {}
status = 'Pronto'
VERSION = '2.5.3'


def default_root():
    project = Path(__file__).resolve().parent.parent
    if (project / 'knowledge').exists():
        return project / 'dados'
    return Path.home() / 'Documents' / 'Sketchfab Link V2' / 'dados'


def record(root, event):
    path = Path(root).resolve() / 'historico.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    event = dict(event, time=datetime.now(timezone.utc).isoformat(), addon_version=VERSION)
    with path.open('a', encoding='utf-8') as output:
        output.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')


def import_capture(path, scene=None, hide_others=False, window=None, use_environment=False):
    """Acrescenta nova coleção; preserva todas as importações e posições anteriores."""
    global last_result, status
    path = Path(path).resolve()
    data = bridge.validate(json.loads(path.read_text(encoding='utf-8')))
    observed_features = coverage.identify(data)
    for image in data['images']:
        if not (path.parent / (image['uid'] + '.png')).is_file():
            raise ValueError('Textura ausente: ' + image['uid'])
    scene = scene or bpy.context.scene
    window = window or bpy.context.window
    original_scene = window.scene
    original_frame = scene.frame_current
    settings = (scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end)
    kinds = ('objects', 'collections', 'scenes', 'meshes', 'armatures', 'node_groups', 'materials', 'images', 'actions', 'shape_keys')
    before = {kind: set(getattr(bpy.data, kind)) for kind in kinds}
    transforms = {o: (o.matrix_basis.copy(), o.matrix_parent_inverse.copy()) for o in scene.objects}
    visibility = {layer.collection: layer.hide_viewport for layer in scene.view_layers[0].layer_collection.children}
    stage = bpy.data.scenes.new('V2 • importação temporária')
    imported = None
    try:
        window.scene = stage
        result = importer.importar(path)
        imported = bpy.data.collections[result['collection']]
        if data.get('animations'):
            result['animation'] = animation.importar_animacao(path, imported.name)
            try:
                result['animation_validation'] = animation.validar_animacao(path, imported.name)
            except RuntimeError as exc:
                # Preservar o comportamento da instalação: registrar divergências para revisão.
                result['animation_validation'] = {'warning': str(exc)}
            for obj in imported.objects:
                if obj.type == 'ARMATURE':
                    obj.hide_set(True)
        result['validation'] = quality.validar(imported)
        result['features'] = observed_features
        imported['addon_version'] = VERSION
        imported['observed_features'] = json.dumps(observed_features,ensure_ascii=False)
        imported['review_status'] = 'Aguardando comparação visual com o Liberty'
        imported['import_validation'] = json.dumps(result, ensure_ascii=False)
        window.scene = scene
        scene.collection.children.link(imported)
        bpy.context.view_layer.update()
        result['layout'] = quality.posicionar_novo(imported, scene)
        # Subsurface vem em unidades do mundo do viewer; converter pela escala aplicada ao modelo.
        gallery = float(imported.get('gallery_scale', 1.0))
        for material in {m for o in imported.all_objects if o.type == 'MESH' for m in o.data.materials if m}:
            if material.get('linkv2_subsurface_viewer_scale') is not None:
                principled = next((n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
                if principled:
                    principled.inputs['Subsurface Scale'].default_value = material['linkv2_subsurface_viewer_scale'] * gallery
        if any(m.get('channels',{}).get('Opacity',{}).get('type')=='refraction' for m in data['materials']):
            if hasattr(scene,'eevee') and hasattr(scene.eevee,'use_raytracing'):
                scene.eevee.use_raytracing=True
        if data.get('animations'):
            scene.render.fps = data['animations'][0]['fps']
            scene.render.fps_base = 1
            scene.frame_start = 1
            other_animations = any(o.animation_data and o.animation_data.action for o in scene.objects if o not in set(imported.all_objects))
            scene.frame_end = max(scene.frame_end,len(data['animations'][0]['frames'])) if other_animations else len(data['animations'][0]['frames'])
            scene.frame_set(1)
        if all(m.get('shadeless') for m in data['materials']):
            scene.view_settings.view_transform = 'Standard'
        bpy.data.scenes.remove(stage)
        if hide_others:
            for layer in bpy.context.view_layer.layer_collection.children:
                if layer.collection.get('source_uid'):
                    layer.hide_viewport = layer.collection != imported
        for obj in imported.all_objects:
            if obj.type == 'ARMATURE':
                # Timer do Baixar não garante a camada de visualização ativa: ocultar em todas.
                for layer in scene.view_layers:
                    obj.hide_set(True, view_layer=layer)
        quality.enquadrar(imported)
        if use_environment:
            # Opcional (desligado por padrão): falha no ambiente não desfaz o modelo.
            try:
                result['environment'] = environment.apply(scene, path, data['title'])
            except Exception as exc:
                result['environment'] = {'applied': False, 'reason': str(exc)}
            try:
                result['lights'] = environment.apply_lights(path, imported)
            except Exception as exc:
                result['lights'] = {'created': [], 'reason': str(exc)}
            try:
                result['postprocess'] = postprocess.apply(scene, path, data['title'])
            except Exception as exc:
                result['postprocess'] = {'applied': False, 'reason': str(exc)}
        result.update(uid=data['uid'], title=data['title'], path=str(path), review_status=imported['review_status'])
        last_result = result
        status = 'Importado: ' + data['title'] + ' — revisão visual pendente'
        return result
    except Exception:
        window.scene = scene
        if imported and imported.name in scene.collection.children:
            scene.collection.children.unlink(imported)
        for obj, (basis, inverse) in transforms.items():
            obj.matrix_basis = basis
            obj.matrix_parent_inverse = inverse
        for kind in kinds:
            blocks = getattr(bpy.data, kind)
            for block in set(blocks) - before[kind]:
                if kind == 'shape_keys':
                    continue  # removidas com as malhas
                blocks.remove(block, do_unlink=True)
        scene.render.fps, scene.render.fps_base, scene.frame_start, scene.frame_end = settings
        scene.frame_set(original_frame)
        for layer in bpy.context.view_layer.layer_collection.children:
            if layer.collection in visibility:
                layer.hide_viewport = visibility[layer.collection]
        raise
    finally:
        window.scene = original_scene


def poll():
    global status
    if receiver is None:
        return None
    while not events.empty():
        kind, message = events.get_nowait()
        status = message
        if kind == 'error':
            record(receiver.root, {'event':'download_failed','error':message})
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    try:
        item = receiver.pending.get_nowait()
    except queue.Empty:
        return 0.5
    # A ponte de estudo envia só o caminho; o botão Baixar envia o destino de cada download.
    path, target_scene, target_window, use_environment = item if isinstance(item, tuple) else (item, destination, destination_window, False)
    try:
        if target_scene is None or target_scene.name not in bpy.data.scenes:
            raise RuntimeError('A cena de destino foi fechada; a captura ficou salva em ' + str(path))
        status = 'Importando captura recebida…'
        if target_window not in list(bpy.context.window_manager.windows):
            target_window = bpy.context.window
        with bpy.context.temp_override(window=target_window):
            result = import_capture(path, target_scene, window=target_window, use_environment=use_environment)
            # Alterações vindas do timer entram no histórico de undo do Blender.
            bpy.ops.ed.undo_push(message='Baixar modelo — Sketchfab Link V2')
        record(receiver.root, dict(event='imported', **result))
    except Exception as exc:
        status = 'Falha: ' + str(exc)
        record(receiver.root, {'event': 'failed', 'path': str(path), 'error': str(exc)})
        print('Sketchfab Link V2:', status)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return 0.5


def start(root=None):
    global receiver, destination, destination_window, status
    if receiver:
        if not bpy.app.timers.is_registered(poll):
            bpy.app.timers.register(poll, first_interval=0.5)
        return receiver.url
    destination = bpy.context.scene
    destination_window = bpy.context.window
    receiver = bridge.Receiver(root or default_root())
    url = receiver.start()
    bpy.app.timers.register(poll, first_interval=0.5)
    status = 'Recepção ligada — destino: ' + destination.name
    return url


def stop():
    global receiver, destination, status
    if bpy.app.timers.is_registered(poll):
        bpy.app.timers.unregister(poll)
    if receiver:
        receiver.stop()
    receiver = None
    destination = None
    status = 'Recepção desligada'


def busy():
    """Há download ou importação em andamento."""
    workers[:] = [w for w in workers if w.is_alive()]
    return bool(workers or (receiver and not receiver.pending.empty()))


def active_downloads():
    workers[:] = [w for w in workers if w.is_alive()]
    return len(workers)


def download(url, root=None, use_environment=False):
    """Fluxo autônomo do botão Baixar; não usa nenhum servidor MCP.
    Um download por vez (escolha do usuário)."""
    global status
    browser.model_uid(url)
    if busy():
        raise RuntimeError('Aguarde o download atual')
    # A ponte pode já existir apontando para outra pasta. O destino pertence a este clique.
    download_root = Path(root or default_root()).expanduser().resolve()
    start(download_root)
    current_receiver = receiver
    job = (bpy.context.scene, bpy.context.window, bool(use_environment))
    status = 'Preparando download…'

    def run():
        try:
            path = browser.capture(url, download_root, lambda message: events.put(('progress', message)))
            if use_environment:
                events.put(('progress', 'Preparando ambiente…'))
                try:
                    environment.prepare(path)
                except Exception as exc:
                    events.put(('progress', 'Ambiente indisponível: ' + str(exc)[:60]))
                try:
                    postprocess.prepare(path)
                except Exception as exc:
                    events.put(('progress', 'Fundo indisponível: ' + str(exc)[:60]))
            if receiver is current_receiver:
                current_receiver.pending.put((path,) + job)
        except Exception as exc:
            message = str(exc)
            if 'download gratuito' in message:
                message = 'Este modelo não tem download gratuito liberado pelo autor'
            events.put(('error', 'Falha: ' + message))

    worker = threading.Thread(target=run, name='SketchfabLinkDownload', daemon=True)
    workers.append(worker)
    worker.start()


def browser_script(expected_uid=''):
    if receiver is None:
        raise RuntimeError('Ligue a recepção primeiro')
    assets = Path(__file__).parent / 'viewer'
    capture = (assets / 'capture.js').read_text(encoding='utf-8')
    animate = (assets / 'animation.js').read_text(encoding='utf-8')
    return '''async () => {
      const expected = EXPECTED;
      const frame = [...document.querySelectorAll('iframe')].find(f => f.src.includes('/embed'));
      if (!frame) throw Error('Abra o viewer do modelo no Sketchfab');
      if (expected && !location.pathname.includes(expected)) throw Error('A página aberta não é o modelo escolhido');
      let summary;
      for (let attempt = 0; attempt < 60; attempt++) {
        try { summary = await (CAPTURE)(); break; }
        catch (error) {
          if (!/ainda não|not found|não está acessível|webpackChunksketchfab/.test(error.message) || attempt === 59) throw error;
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
      }
      if (summary.animations) await (ANIMATE)();
      const data = frame.contentWindow.__link3d_capture;
      if (expected && data.uid !== expected) throw Error('UID diferente da origem esperada');
      data.captured_at = new Date().toISOString();
      data.capture_version = '2.5.3';
      const response = await fetch(ENDPOINT, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
      const result = await response.json();
      if (!response.ok) throw Error(result.error || 'Falha na transferência');
      return {capture:summary, transfer:result};
    }'''.replace('EXPECTED', json.dumps(expected_uid)).replace('CAPTURE', capture).replace('ANIMATE', animate).replace('ENDPOINT', json.dumps(receiver.url))
