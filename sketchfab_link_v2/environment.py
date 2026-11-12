"""Ambiente (HDRI) do viewer: cubemap LogLuv -> panorama .hdr -> mundo do Blender.

Regras lidas no próprio shader do viewer (estudo do Train):
- arquivo `background_cubemap_N_blur_luv.bin.gz`, planar por face (6, 4, N, N),
  faces na ordem GL +X, -X, +Y, -Y, +Z, -Z;
- LogLuv: Le = z*255 + w; Y = 2^((Le-127)/2); Z = Y/v; X = u*Z; RGB = LUVInverse·XYZ;
- consulta no shader: uEnvironmentTransform * direção_em_vista, logo a direção no
  ambiente é E·V·d_mundo (E e V lidos no mesmo quadro);
- força = exposição do ambiente; fundo visto pela câmera também usa backgroundExposure.
Opcional no painel e desativado por padrão: o mundo é único por cena.
"""
import gzip
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

LUV_INVERSE = ((6.0013, -2.700, -1.7995), (-1.332, 3.1029, -5.7720), (0.3007, -1.088, 5.6268))
HOSTS = {'media.sketchfab.com'}
CUBE_FILE = 'ambiente_cubemap.bin.gz'
HDR_FILE = 'ambiente.hdr'
BACKGROUND_FILE = 'ambiente_fundo.hdr'


def available(env):
    cube = (env or {}).get('cubemap') or {}
    return (isinstance(cube.get('url'), str) and type(cube.get('size')) is int and cube['size'] > 0
            and len(env.get('transform') or []) == 9 and len(env.get('view') or []) == 16)


def _check_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.query or not parsed.path.endswith('.bin.gz'):
        raise ValueError('Endereço de ambiente inesperado')


def download(env, folder):
    """Rede apenas para o arquivo público do ambiente já referenciado pelo viewer."""
    target = Path(folder) / CUBE_FILE
    if target.is_file():
        return target
    url = env['cubemap']['url']
    _check_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
    if raw[:2] != b'\x1f\x8b':
        raise ValueError('Arquivo de ambiente inválido')
    temporary = target.with_suffix('.tmp')
    temporary.write_bytes(raw)
    temporary.replace(target)
    return target


def _decode(path, size):
    import numpy as np
    expected = 6 * 4 * size * size
    with gzip.open(path, 'rb') as source:
        raw = source.read()
    if len(raw) != expected:
        raise ValueError('Cubemap de ambiente com tamanho inesperado')
    data = np.frombuffer(raw, np.uint8).reshape(6, 4, size, size).transpose(0, 2, 3, 1).astype(np.float32) / 255.0
    luminance = data[..., 2] * 255.0 + data[..., 3]
    y = np.exp2((luminance - 127.0) / 2.0)
    z = y / np.maximum(data[..., 1], 1e-6)
    x = data[..., 0] * z
    inverse = np.array(LUV_INVERSE, np.float32).T  # mat3 GLSL é coluna-major
    return np.maximum(np.stack([x, y, z], -1) @ inverse.T, 0.0)


def _rotation(env):
    import numpy as np
    transform = np.array(env['transform'], np.float64).reshape(3, 3).T
    view = np.array(env['view'], np.float64).reshape(4, 4).T[:3, :3]
    rotation = transform @ view
    if abs(np.linalg.det(rotation) - 1.0) > 1e-2:
        raise ValueError('Orientação do ambiente inválida')
    return rotation


def _lookup(cube, directions):
    import numpy as np
    x, y, z = directions[..., 0], directions[..., 1], directions[..., 2]
    ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
    face = np.zeros(x.shape, int)
    sc = np.zeros(x.shape)
    tc = np.zeros(x.shape)
    major = np.ones(x.shape)
    on_x = (ax >= ay) & (ax >= az)
    on_y = ~on_x & (ay >= az)
    on_z = ~on_x & ~on_y
    for mask, index, s_value, t_value, m_value in (
            (on_x & (x > 0), 0, -z, -y, ax), (on_x & (x <= 0), 1, z, -y, ax),
            (on_y & (y > 0), 2, x, z, ay), (on_y & (y <= 0), 3, x, -z, ay),
            (on_z & (z > 0), 4, x, -y, az), (on_z & (z <= 0), 5, -x, -y, az)):
        face[mask] = index
        sc[mask] = s_value[mask]
        tc[mask] = t_value[mask]
        major[mask] = m_value[mask]
    size = cube.shape[1]
    s = np.clip(((sc / major + 1) / 2 * size).astype(int), 0, size - 1)
    t = np.clip(((tc / major + 1) / 2 * size).astype(int), 0, size - 1)
    return cube[face, t, s]


def _equirect(cube, rotation):
    """Padrão do Blender: u = -atan2(y,x)/2π + 0.5, v = atan2(z, |xy|)/π + 0.5."""
    import numpy as np
    width = cube.shape[1] * 4
    height = width // 2
    u = (np.arange(width) + 0.5) / width
    v = 1 - (np.arange(height) + 0.5) / height  # primeira linha do .hdr é o topo
    azimuth = -(u - 0.5) * 2 * np.pi
    elevation = (v - 0.5) * np.pi
    a, e = np.meshgrid(azimuth, elevation)
    directions = np.stack([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)], -1)
    return _lookup(cube, directions @ rotation.T)


def _write_hdr(path, image):
    import numpy as np
    height, width, _ = image.shape
    peak = image.max(-1)
    lit = peak > 1e-32
    exponent = np.where(lit, np.ceil(np.log2(np.maximum(peak, 1e-32))), -128).astype(int)
    scale = np.where(lit, 256.0 / np.exp2(exponent), 0.0)
    rgbe = np.zeros((height, width, 4), np.uint8)
    rgbe[..., :3] = np.clip(image * scale[..., None], 0, 255).astype(np.uint8)
    rgbe[..., 3] = np.where(lit, exponent + 128, 0)
    temporary = Path(path).with_suffix('.tmp')
    with temporary.open('wb') as output:
        output.write(b'#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n' + f'-Y {height} +X {width}\n'.encode())
        output.write(rgbe.tobytes())
    temporary.replace(path)


def _sph_background(env, rotation, width=512):
    """Modo de fundo 'ambient': o viewer desenha o SH difuso (shader Environment)."""
    import numpy as np
    s = np.array(env['sph'], np.float64).reshape(9, 3)
    height = width // 2
    u = (np.arange(width) + 0.5) / width
    v = 1 - (np.arange(height) + 0.5) / height
    a, e = np.meshgrid(-(u - 0.5) * 2 * np.pi, (v - 0.5) * np.pi)
    n = np.stack([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)], -1) @ rotation.T
    x, y, z = n[..., 0:1], n[..., 1:2], n[..., 2:3]
    result = (s[0] + s[1] * y + s[2] * z + s[3] * x + s[4] * y * x + s[5] * y * z
              + s[6] * (3 * z * z - 1) + s[7] * z * x + s[8] * (x * x - y * y))
    return np.maximum(result, 0.0)


def prepare(capture_path):
    """Executável fora da thread principal: baixa e gera o .hdr ao lado da captura."""
    capture_path = Path(capture_path)
    folder = capture_path.parent
    target = folder / HDR_FILE
    if target.is_file():
        return target
    env = json.loads(capture_path.read_text(encoding='utf-8')).get('environment')
    if not available(env):
        return None
    rotation = _rotation(env)
    if env.get('background_mode') == 'ambient' and len(env.get('sph') or []) == 27:
        _write_hdr(folder / BACKGROUND_FILE, _sph_background(env, rotation))
    cube = _decode(download(env, folder), env['cubemap']['size'])
    _write_hdr(target, _equirect(cube, rotation))
    return target


def apply(scene, capture_path, title):
    """Cria o mundo do ambiente e o aplica na cena. Não altera objetos."""
    import bpy
    capture_path = Path(capture_path)
    env = json.loads(capture_path.read_text(encoding='utf-8')).get('environment') or {}
    hdr = prepare(capture_path)
    if hdr is None:
        return {'applied': False, 'reason': env.get('error') or 'Captura sem ambiente legível'}
    world = bpy.data.worlds.new('Ambiente • ' + (env.get('name') or title))
    world['linkv2_environment_uid'] = env.get('uid', '')
    world['linkv2_source_title'] = title
    world.use_nodes = True
    tree = world.node_tree
    background = next(node for node in tree.nodes if node.type == 'BACKGROUND')
    texture = tree.nodes.new('ShaderNodeTexEnvironment')
    texture.image = bpy.data.images.load(str(hdr), check_existing=True)
    tree.links.new(texture.outputs['Color'], background.inputs['Color'])
    # Zero é valor real (ambiente que não ilumina): só usar o padrão quando ausente.
    exposure = float(env['exposure']) if env.get('exposure') is not None else 1.0
    background_exposure = float(env['background_exposure']) if env.get('background_exposure') is not None else 1.0
    background.inputs['Strength'].default_value = exposure
    if abs(background_exposure - 1.0) > 1e-6:
        # Só o fundo visto pela câmera recebe backgroundExposure, como no viewer.
        path = tree.nodes.new('ShaderNodeLightPath')
        camera = tree.nodes.new('ShaderNodeMath')
        camera.operation = 'MULTIPLY_ADD'
        camera.inputs[1].default_value = background_exposure - 1.0
        camera.inputs[2].default_value = 1.0
        strength = tree.nodes.new('ShaderNodeMath')
        strength.operation = 'MULTIPLY'
        strength.inputs[1].default_value = exposure
        tree.links.new(path.outputs['Is Camera Ray'], camera.inputs[0])
        tree.links.new(camera.outputs[0], strength.inputs[0])
        tree.links.new(strength.outputs[0], background.inputs['Strength'])
    _background_for_camera(tree, background, env, hdr.parent, exposure * background_exposure)
    scene.world = world
    return {'applied': True, 'background': env.get('background_mode'), 'world': world.name, 'name': env.get('name', ''), 'exposure': exposure}


def _background_for_camera(tree, lighting, env, folder, strength):
    """Câmera (e refração, que no viewer é em espaço de tela) vê o fundo do viewer;
    iluminação e reflexos continuam vindo do HDRI."""
    import bpy
    mode = env.get('background_mode')
    color = None
    if mode == 'ambient' and (folder / BACKGROUND_FILE).is_file():
        node = tree.nodes.new('ShaderNodeTexEnvironment')
        node.image = bpy.data.images.load(str(folder / BACKGROUND_FILE), check_existing=True)
        color = node.outputs['Color']
    elif mode == 'color' and len(env.get('background_color') or []) == 3:
        rgb = tree.nodes.new('ShaderNodeRGB')
        # Cor de fundo do viewer é sRGB de interface; converter para linear.
        rgb.outputs[0].default_value = (*[c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in env['background_color']], 1)
        color = rgb.outputs[0]
        strength = 1.0
    if color is None:
        return
    output = next(node for node in tree.nodes if node.type == 'OUTPUT_WORLD')
    backdrop = tree.nodes.new('ShaderNodeBackground')
    backdrop.label = 'Fundo do viewer (' + str(mode) + ')'
    tree.links.new(color, backdrop.inputs['Color'])
    backdrop.inputs['Strength'].default_value = strength
    # Só raio de câmera: no EEVEE a sonda do mundo também se marca como transmissão
    # e os reflexos perderiam o HDRI; a refração em tela já vê o fundo renderizado.
    path = tree.nodes.new('ShaderNodeLightPath')
    mix = tree.nodes.new('ShaderNodeMixShader')
    tree.links.new(path.outputs['Is Camera Ray'], mix.inputs[0])
    tree.links.new(lighting.outputs[0], mix.inputs[1])
    tree.links.new(backdrop.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs['Surface'])


def apply_lights(capture_path, collection):
    """Luzes do viewer lidas dos uniforms (espaço de câmera da view capturada).
    Direcional: difuso = albedo·NoL·cor (sem 1/π) => Sun com força π·intensidade."""
    import math
    import bpy
    from mathutils import Matrix, Vector
    lights = json.loads(Path(capture_path).read_text(encoding='utf-8')).get('lights') or {}
    view = lights.get('view')
    if not view or len(view) != 16:
        return {'created': [], 'skipped': [], 'reason': 'Captura sem luzes legíveis'}
    rotation = Matrix([view[i:i + 4] for i in range(0, 16, 4)]).transposed().to_3x3()
    shadows = set(lights.get('shadows') or [])
    created, skipped = [], []
    root = next((o for o in collection.objects if o.name.startswith('MODELO_')), None)
    # Mesma transformação aplicada pelo organizador ao modelo: T(c+delta)·S(f)·T(-c).
    scale, placement = 1.0, Matrix.Identity(4)
    if root and collection.get('layout_offset') is not None:
        scale = root.scale.x
        center = root.matrix_world.translation - Vector(collection['layout_offset'])
        placement = Matrix.Translation(root.matrix_world.translation) @ Matrix.Scale(scale, 4) @ Matrix.Translation(-center)
    view_inverse = Matrix([view[i:i + 4] for i in range(0, 16, 4)]).transposed().inverted()
    for index, item in sorted((lights.get('items') or {}).items(), key=lambda pair: int(pair[0])):
        color = item.get('diffuse')
        direction = item.get('viewDirection')
        position = item.get('viewPosition')
        attenuation = item.get('attenuation') or [1, 0, 0, 0]
        if color and position:
            # Pontual/spot: albedo·NoL·cor·cone/(c + l·d + q·d²). O Blender cai com 1/d²; casar a
            # intensidade na distância da luz ao centro do modelo (d0) vale para qualquer c, l, q.
            peak = max(color[:3])
            if peak <= 0:
                continue
            location = placement @ (view_inverse @ Vector(position[:3]))
            reference = root.matrix_world.translation if root else Vector()
            d0 = max((location - reference).length, 1e-6)
            d0_viewer = d0 / scale
            c0, linear, quadratic = (list(attenuation) + [1, 0, 0])[:3]
            falloff = max(c0 + linear * d0_viewer + quadratic * d0_viewer ** 2, 1e-9)
            spot = 'spotCutOff' in item and direction
            data = bpy.data.lights.new('Luz do viewer ' + index, 'SPOT' if spot else 'POINT')
            data.energy = 4 * math.pi ** 2 * peak * d0 ** 2 / falloff
            data.color = [c / peak for c in color[:3]]
            data.shadow_soft_size = scale / math.sqrt(quadratic) if quadratic > 0 and c0 > 0 and scale / math.sqrt(quadratic) < d0 else 0.02 * d0
            data.use_shadow = int(index) in shadows
            obj = bpy.data.objects.new('Luz do viewer ' + index, data)
            collection.objects.link(obj)
            obj.location = location
            if spot:
                cutoff = max(-1.0, min(1.0, float(item['spotCutOff'])))
                data.spot_size = min(math.pi, 2 * math.acos(cutoff))
                data.spot_blend = max(0.0, min(1.0, float(item.get('spotBlend', 0)) / max(1e-6, 1 - cutoff)))
                aim = (rotation.transposed() @ Vector(direction[:3])).normalized()
                obj.rotation_euler = (-aim).to_track_quat('Z', 'Y').to_euler()
            obj['linkv2_viewer_light'] = int(index)
            created.append({'name': obj.name, 'type': data.type, 'power_w': round(data.energy, 3), 'shadow': data.use_shadow})
            continue
        if not color or position or not direction:
            skipped.append({'index': index, 'uniforms': sorted(item)})
            continue
        peak = max(color[:3])
        if peak <= 0:
            continue
        data = bpy.data.lights.new('Luz do viewer ' + index, 'SUN')
        data.energy = math.pi * peak
        data.color = [c / peak for c in color[:3]]
        data.angle = math.radians(2)
        data.use_shadow = int(index) in shadows
        obj = bpy.data.objects.new('Luz do viewer ' + index, data)
        collection.objects.link(obj)
        world_direction = (rotation.transposed() @ Vector(direction[:3])).normalized()
        obj.rotation_euler = (-world_direction).to_track_quat('Z', 'Y').to_euler()
        if root:
            obj.location = root.matrix_world.translation
        obj['linkv2_viewer_light'] = int(index)
        created.append({'name': obj.name, 'type': 'SUN', 'strength': round(data.energy, 3), 'shadow': data.use_shadow})
    return {'created': created, 'skipped': skipped}
