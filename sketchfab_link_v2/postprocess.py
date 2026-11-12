"""Pós-processamento do viewer reproduzido no compositor (Blender 5.x).

Fórmulas lidas nos shaders do viewer (estudo do PS2):
- bloomCombine_toneMapping: cor + bloom·fator, depois toneMapping(cor):
    col = cor·exposição; lum = dot(col·(1+brilho), Rec709); col = mix(lum, col·(1+brilho), saturação);
    col = max(0, col·(1+contraste) − 0.5·contraste); método 1 reinhard: col/(1+lum);
    método 2 filmic: x=max(0,col−0.004); col=(x(6.2x+0.5))/(x(6.2x+1.7)+0.06); col=col^2.2.
  Depois linearTosRGB: equivale à vista Standard do Blender (sem curva extra).
- fxaa_sharpen_vignette_grain: sharpen c + s·(4c − vizinhos); vinheta multiplica em linear por
  smoothstep(lens.x, lens.y, distância(uv, 0.5)); granulado animado (não reproduzido).
Opcional (caixa "Visual do viewer", desligada por padrão): o compositor e a gestão de cor são da cena.
"""
import json
import math
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import bpy

REC709 = (0.2126, 0.7152, 0.0722)
BACKGROUND_FILE = 'fundo_viewer'


def _fixed_background(data):
    background = data.get('background') or {}
    image = background.get('image') or {}
    url = image.get('url') or ''
    parsed = urlparse(url)
    if background.get('mode') != 'fixed' or parsed.scheme != 'https' or parsed.hostname != 'media.sketchfab.com':
        return None
    return url


def prepare(capture_path):
    """Fora da thread principal: baixa a imagem de fundo 'fixed' do viewer ao lado da captura."""
    capture_path = Path(capture_path)
    url = _fixed_background(json.loads(capture_path.read_text(encoding='utf-8')))
    if not url:
        return None
    suffix = Path(urlparse(url).path).suffix.lower() if Path(urlparse(url).path).suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'
    target = capture_path.parent / (BACKGROUND_FILE + suffix)
    if not target.is_file():
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(raw)
        temporary.replace(target)
    return target


def _vector(tree, operation, a, b=None, value=None):
    node = tree.nodes.new('ShaderNodeVectorMath')
    node.operation = operation
    tree.links.new(a, node.inputs[0])
    if b is not None:
        tree.links.new(b, node.inputs[1])
    elif value is not None:
        if operation == 'SCALE':
            node.inputs['Scale'].default_value = value
        else:
            node.inputs[1].default_value = value if isinstance(value, (tuple, list)) else (value, value, value)
    return node.outputs['Vector'] if operation != 'DOT_PRODUCT' else node.outputs['Value']


def _math(tree, operation, a, value):
    node = tree.nodes.new('ShaderNodeMath')
    node.operation = operation
    tree.links.new(a, node.inputs[0])
    node.inputs[1].default_value = value
    return node.outputs[0]


def _tone(tree, color, post):
    method = str(post.get('toneMappingMethod') or 'default')
    exposure = float(post['toneMappingExposure']) if post.get('toneMappingExposure') is not None else 1.0
    brightness = float(post.get('toneMappingBrightness') or 0.0)
    saturation = float(post.get('toneMappingSaturation', 1.0))
    contrast = float(post.get('toneMappingContrast') or 0.0)
    col = _vector(tree, 'SCALE', color, value=exposure * (1 + brightness))
    if abs(saturation - 1) > 1e-6:
        lum = _vector(tree, 'DOT_PRODUCT', col, value=REC709)
        delta = _vector(tree, 'SUBTRACT', col, lum)
        col = _vector(tree, 'ADD', _vector(tree, 'SCALE', delta, value=saturation), lum)
    if abs(contrast) > 1e-6:
        col = _vector(tree, 'SUBTRACT', _vector(tree, 'SCALE', col, value=1 + contrast), value=0.5 * contrast)
    col = _vector(tree, 'MAXIMUM', col, value=0.0)
    if method == 'reinhard':
        lum = _math(tree, 'ADD', _vector(tree, 'DOT_PRODUCT', col, value=REC709), 1.0)
        col = _vector(tree, 'DIVIDE', col, lum)
    elif method == 'filmic':
        x = _vector(tree, 'MAXIMUM', _vector(tree, 'SUBTRACT', col, value=0.004), value=0.0)
        top = _vector(tree, 'MULTIPLY', x, _vector(tree, 'ADD', _vector(tree, 'SCALE', x, value=6.2), value=0.5))
        bottom = _vector(tree, 'ADD', _vector(tree, 'MULTIPLY', x, _vector(tree, 'ADD', _vector(tree, 'SCALE', x, value=6.2), value=1.7)), value=0.06)
        col = _vector(tree, 'POWER', _vector(tree, 'DIVIDE', top, bottom), value=2.2)
    return col, method


def _vignette(tree, color, image, lens):
    outer, inner = float(lens[0]), min(float(lens[1]), float(lens[0]) - 1e-4)
    coords = tree.nodes.new('CompositorNodeImageCoordinates')
    tree.links.new(image, coords.inputs['Image'])
    centered = _vector(tree, 'SUBTRACT', coords.outputs['Normalized'], value=(0.5, 0.5, 0.0))
    length = tree.nodes.new('ShaderNodeVectorMath')
    length.operation = 'LENGTH'
    tree.links.new(centered, length.inputs[0])
    curve = tree.nodes.new('ShaderNodeMapRange')
    curve.interpolation_type = 'SMOOTHSTEP'
    tree.links.new(length.outputs['Value'], curve.inputs['Value'])
    curve.inputs['From Min'].default_value = outer
    curve.inputs['From Max'].default_value = inner
    curve.inputs['To Min'].default_value = 0.0
    curve.inputs['To Max'].default_value = 1.0
    scaled = tree.nodes.new('ShaderNodeVectorMath')
    scaled.operation = 'SCALE'
    tree.links.new(color, scaled.inputs[0])
    tree.links.new(curve.outputs['Result'], scaled.inputs['Scale'])
    return scaled.outputs['Vector']


def apply(scene, capture_path, title):
    data = json.loads(Path(capture_path).read_text(encoding='utf-8'))
    post = data.get('postprocess') or {}
    if post.get('error') or not post.get('enable'):
        post = {}
    background = prepare(capture_path) if _fixed_background(data) else None
    if not post and not background:
        return {'applied': False, 'reason': 'Viewer sem pós-processamento nem fundo fixo'}
    tree = bpy.data.node_groups.new('Pós do viewer • ' + title, 'CompositorNodeTree')
    tree.interface.new_socket(name='Image', in_out='INPUT', socket_type='NodeSocketColor')
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    output = tree.nodes.new('NodeGroupOutput')
    layers = tree.nodes.new('CompositorNodeRLayers')
    image = layers.outputs['Image']
    color = image
    done, skipped = [], []
    if post.get('bloomEnable'):
        glare = tree.nodes.new('CompositorNodeGlare')
        glare.inputs['Type'].default_value = 'Bloom'
        glare.inputs['Quality'].default_value = 'High'
        glare.inputs['Threshold'].default_value = float(post.get('bloomThreshold') or 0.0)
        glare.inputs['Strength'].default_value = float(post.get('bloomFactor') or 0.0)
        glare.inputs['Size'].default_value = max(0.0, min(1.0, float(post.get('bloomRadius') or 0.5)))
        tree.links.new(color, glare.inputs['Image'])
        color = glare.outputs['Image']
        done.append('bloom (aproximado)')
    if post.get('toneMappingEnable'):
        color, method = _tone(tree, color, post)
        done.append('toneMapping ' + method)
    if background:
        # composeFixedBackground do viewer: imagem 2D preenchendo a tela atrás da cena.
        scene.render.film_transparent = True
        picture = tree.nodes.new('CompositorNodeImage')
        picture.image = bpy.data.images.load(str(background), check_existing=True)
        fit = tree.nodes.new('CompositorNodeScale')
        fit.inputs['Type'].default_value = 'Render Size'
        fit.inputs['Frame Type'].default_value = 'Crop'
        tree.links.new(picture.outputs['Image'], fit.inputs['Image'])
        over = tree.nodes.new('CompositorNodeAlphaOver')
        tree.links.new(fit.outputs['Image'], over.inputs['Background'])
        tree.links.new(color, over.inputs['Foreground'])
        color = over.outputs['Image']
        done.append('fundo fixo (imagem)')
    if post.get('sharpenEnable') and post.get('sharpenFactor'):
        sharpen = tree.nodes.new('CompositorNodeFilter')
        sharpen.inputs['Type'].default_value = 'Diamond Sharpen'
        sharpen.inputs['Factor'].default_value = float(post['sharpenFactor'])
        tree.links.new(color, sharpen.inputs['Image'])
        color = sharpen.outputs['Image']
        done.append('sharpen')
    lens = post.get('lens_radius')
    if post.get('vignetteEnable') and lens and len(lens) >= 2:
        color = _vignette(tree, color, image, lens)
        done.append('vignette')
    if post.get('chromaticAberrationEnable'):
        lens_node = tree.nodes.new('CompositorNodeLensdist')
        lens_node.inputs['Dispersion'].default_value = float(post.get('chromaticAberrationFactor') or 0.0)
        tree.links.new(color, lens_node.inputs['Image'])
        color = lens_node.outputs['Image']
        done.append('chromaticAberration (aproximado)')
    for name in ('grain', 'colorBalance', 'dof', 'ssao'):
        if post.get(name + 'Enable'):
            skipped.append(name)
    tree.links.new(color, output.inputs['Image'])
    tree['linkv2_source_title'] = title
    scene.compositing_node_group = tree
    scene.render.use_compositing = True
    # O viewer converte direto para sRGB depois do tone mapping: vista Standard, sem exposição extra.
    try:
        scene.view_settings.view_transform = 'Standard'
    except TypeError:
        skipped.append('vista Standard indisponível')
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    if post.get('ssrEnable') and hasattr(scene, 'eevee') and hasattr(scene.eevee, 'use_raytracing'):
        scene.eevee.use_raytracing = True
        # Qualidade padrão (0.25, meia resolução) perdia reflexos finos como folhas (Tree).
        try:
            scene.eevee.ray_tracing_method = 'SCREEN'
            scene.eevee.ray_tracing_options.screen_trace_quality = 1.0
            scene.eevee.ray_tracing_options.resolution_scale = '1'
        except (AttributeError, TypeError):
            pass
        done.append('ssr (raytracing EEVEE em tela, qualidade cheia)')
    return {'applied': True, 'tree': tree.name, 'done': done, 'skipped': skipped}
