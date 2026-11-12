"""Importação própria de capturas do viewer. Chamado pelo serviço transacional."""
import json
import math
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
from . import bridge, primitives


def vidro_preview(mat, shader, ior=1.45, opacity=0, color=(0, 0, 0)):
    # Aproximação para inspecionar vidro sem depender de refração da viewport.
    shader.inputs['Transmission Weight'].default_value = 0
    shader.inputs['Alpha'].default_value = max(opacity, 0.2)
    shader.inputs['Base Color'].default_value = (*color[:3], 1)
    output = next(n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL')
    mat.node_tree.links.new(shader.outputs[0], output.inputs['Surface'])
    mat.use_raytrace_refraction = False
    modes = {e.identifier for e in mat.bl_rna.properties['surface_render_method'].enum_items}
    if 'BLENDED' in modes:
        mat.surface_render_method = 'BLENDED'


LAYERED_OPAQUE_FRACTION = 0.2
NEAR_UNIT_IOR = 1.05


def opaque_fraction(image, channel):
    """Fração de texels totalmente opacos (>=0.99) numa cópia reduzida da imagem."""
    import numpy as np
    sample = image.copy()
    try:
        sample.scale(128, 128)
        pixels = np.empty(128 * 128 * sample.channels, np.float32)
        sample.pixels.foreach_get(pixels)
        pixels = pixels.reshape(-1, sample.channels)
        values = pixels[:, 3] if channel.get('component') == 'alpha' and sample.channels == 4 else pixels[:, 0]
        if channel.get('invert'):
            values = 1 - values
        return float((values * channel.get('factor', 1) >= 0.99).mean())
    finally:
        bpy.data.images.remove(sample)

def _opacity_samples(image, channel, size=256):
    import numpy as np
    sample = image.copy()
    try:
        sample.scale(size, size)
        pixels = np.empty(size * size * sample.channels, np.float32)
        sample.pixels.foreach_get(pixels)
        pixels = pixels.reshape(size, size, sample.channels)
        values = pixels[..., 3] if channel.get('component') == 'alpha' and sample.channels == 4 else pixels[..., 0]
        if channel.get('invert'):
            values = 1 - values
        return values * channel.get('factor', 1)
    finally:
        bpy.data.images.remove(sample)


def split_opaque_refraction(mesh, material, image, channel, uv_name, twins):
    """Viewer: pixels com opacidade 1 vão ao passe opaco e só o resto refrata (uDrawOpaque).
    No EEVEE a refração de um material não vê faces do mesmo material; as faces opacas
    recebem uma cópia sem transmissão para entrar no passe opaco (ex.: íris sob a córnea)."""
    import numpy as np
    layer = mesh.uv_layers.get(uv_name) or (mesh.uv_layers[0] if mesh.uv_layers else None)
    if layer is None or not len(mesh.polygons):
        return 0
    values = _opacity_samples(image, channel)
    size = values.shape[0]
    uv = np.empty(len(mesh.loops) * 2, np.float32)
    layer.data.foreach_get('uv', uv)
    uv = uv.reshape(-1, 2)
    starts = np.empty(len(mesh.polygons), np.int32)
    totals = np.empty(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get('loop_start', starts)
    mesh.polygons.foreach_get('loop_total', totals)
    centers = np.add.reduceat(uv, starts) / totals[:, None]
    px = np.clip(((centers[:, 0] % 1.0) * size).astype(int), 0, size - 1)
    py = np.clip(((centers[:, 1] % 1.0) * size).astype(int), 0, size - 1)
    opaque = values[py, px] >= 0.99
    if opaque.all() or not opaque.any():
        return 0
    twin = twins.get(material.name)
    if twin is None:
        twin = material.copy()
        twin.name = material.name + ' • opaco'
        tree = twin.node_tree
        mix = next((n for n in tree.nodes if n.type == 'MIX_SHADER' and n.label.startswith('mix(refração')), None)
        output = next(n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL')
        if mix and mix.inputs[2].is_linked:
            tree.links.new(mix.inputs[2].links[0].from_socket, output.inputs['Surface'])
        if hasattr(twin, 'use_raytrace_refraction'):
            twin.use_raytrace_refraction = False
        if hasattr(twin, 'surface_render_method'):
            twin.surface_render_method = 'DITHERED'
        twin['linkv2_opaque_pass'] = True
        twins[material.name] = twin
    mesh.materials.append(twin)
    index = len(mesh.materials) - 1
    indices = np.zeros(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get('material_index', indices)
    indices[opaque] = index
    mesh.polygons.foreach_set('material_index', indices)
    return int(opaque.sum())

def importar(caminho):
    caminho = Path(caminho)
    data = json.loads(caminho.read_text(encoding='utf-8'))
    data['meshes'] = bridge.resolve_meshes(data)
    if data.get('version') != 1:
        raise ValueError('Formato de captura desconhecido')
    for index, source in enumerate(data['meshes']):
        source['name'] = source.get('name') or ('Parte_' + str(index + 1))
        if source.get('points'):
            matrices=[f['meshes'][index] for clip in data.get('animations',[]) for f in clip['frames']] or [source['matrix']]
            scales=[max(math.sqrt(sum(matrix[offset+j]**2 for j in range(3))) for offset in (0,4,8)) for matrix in matrices]
            maximum=max(scales)
            source['_point_reference_scale']=maximum
            source['_point_visibility_threshold']=maximum*1e-7 if maximum and min(scales)<maximum*1e-7 else 0
    for index, source in enumerate(data['materials']):
        source['name'] = source.get('name') or ('Material_' + str(index + 1))
    for source in data['meshes']:
        count = len(source['positions']) // 3
        if any(i < 0 or i >= count for face in source['triangles'] for i in face):
            raise ValueError('Índice inválido em ' + source['name'])

    collection = bpy.data.collections.new(data['title'])
    bpy.context.scene.collection.children.link(collection)
    collection['source_url'] = data['source']
    collection['author'] = data['author']
    collection['license'] = data['license']
    collection['source_uid'] = data.get('uid', '')
    collection['capture_path'] = str(caminho)
    images = []
    for item in data['images']:
        image = bpy.data.images.load(str(caminho.parent / (item['uid'] + '.png')), check_existing=True)
        image.pack()
        images.append(image)
    materials = []
    data_images = {}
    for material_index, item in enumerate(data['materials']):
        mat = bpy.data.materials.new(item['name'])
        mat.use_nodes = True
        shader = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
        shader.inputs['Base Color'].default_value = (*[v * item['factor'] for v in item['color']], 1)
        shader.inputs['Roughness'].default_value = item['roughness']
        shader.inputs['Metallic'].default_value = item['metallic']
        shader.location = (300, 0)
        mat.use_backface_culling = item.get('cullFace') == 1029
        available_units={int(k) for m in data['meshes'] if m['material']==material_index for k in (m.get('uvLayers') or ({'0':m.get('uv')} if m.get('uv') else {}))}
        def uv_name(unit):
            if unit not in available_units:unit=min(available_units) if available_units else 0
            return 'UVMap' if unit==0 else 'UVMap'+str(unit)

        def mapa(image_index, dados=False, info=None):
            tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
            tex.image = images[image_index]
            if dados:
                if image_index not in data_images:
                    data_images[image_index] = tex.image.copy()
                    data_images[image_index].colorspace_settings.name = 'Non-Color'
                    data_images[image_index].reload()
                    data_images[image_index].pack()
                tex.image = data_images[image_index]
            if info:
                uv = mat.node_tree.nodes.new('ShaderNodeUVMap')
                unit = info.get('texCoord', 0)
                uv.uv_map = uv_name(unit)
                vector = uv.outputs['UV']
                transform = info.get('transform')
                if transform:
                    mapping = mat.node_tree.nodes.new('ShaderNodeMapping')
                    scale = transform.get('scale', [1, 1])
                    offset = transform.get('offset', [0, 0])
                    mapping.inputs['Scale'].default_value = (*scale[:2], 1)
                    mapping.inputs['Location'].default_value = (*offset[:2], 0)
                    mapping.inputs['Rotation'].default_value = (0, 0, transform.get('rotation', 0))
                    mat.node_tree.links.new(vector, mapping.inputs['Vector'])
                    vector = mapping.outputs['Vector']
                wraps=(info.get('wrapS') or 10497,info.get('wrapT') or 10497)
                extensions={e.identifier for e in tex.bl_rna.properties['extension'].enum_items}
                same={10497:'REPEAT',33071:'EXTEND',33648:'MIRROR'}
                if wraps[0]==wraps[1] and same.get(wraps[0]) in extensions:
                    tex.extension=same[wraps[0]]
                elif wraps!=(10497,10497):
                    separate=mat.node_tree.nodes.new('ShaderNodeSeparateXYZ')
                    combine=mat.node_tree.nodes.new('ShaderNodeCombineXYZ')
                    mat.node_tree.links.new(vector,separate.inputs[0])
                    for axis,wrap in enumerate(wraps):
                        coordinate=separate.outputs[axis]
                        if wrap==33648:
                            ping=mat.node_tree.nodes.new('ShaderNodeMath')
                            ping.operation='PINGPONG'
                            ping.inputs[1].default_value=1
                            mat.node_tree.links.new(coordinate,ping.inputs[0])
                            coordinate=ping.outputs[0]
                        if wrap in (33071,33648):
                            clamp=mat.node_tree.nodes.new('ShaderNodeClamp')
                            size=max(1,tex.image.size[axis])
                            clamp.inputs['Min'].default_value=0.5/size
                            clamp.inputs['Max'].default_value=1-0.5/size
                            mat.node_tree.links.new(coordinate,clamp.inputs['Value'])
                            coordinate=clamp.outputs[0]
                        mat.node_tree.links.new(coordinate,combine.inputs[axis])
                    vector=combine.outputs[0]
                mat.node_tree.links.new(vector, tex.inputs['Vector'])
                choices={e.identifier for e in tex.bl_rna.properties['interpolation'].enum_items}
                interpolation='Closest' if info.get('magFilter')==9728 else 'Linear'
                if interpolation in choices:tex.interpolation=interpolation
            return tex

        def escalar(channel, invert=False):
            tex = mapa(channel['image'], dados=True, info=channel)
            separate = mat.node_tree.nodes.new('ShaderNodeSeparateColor')
            mat.node_tree.links.new(tex.outputs['Color'], separate.inputs['Color'])
            output = separate.outputs['Red']
            if channel.get('component') == 'alpha' and data['images'][channel['image']].get('format') != 6409:
                output = tex.outputs['Alpha']
            if bool(channel.get('invert', False)):
                inv = mat.node_tree.nodes.new('ShaderNodeMath')
                inv.operation = 'SUBTRACT'
                inv.inputs[0].default_value = 1
                mat.node_tree.links.new(output, inv.inputs[1])
                output = inv.outputs[0]
            factor = channel.get('factor', 1)
            if factor != 1:
                mul = mat.node_tree.nodes.new('ShaderNodeMath')
                mul.operation = 'MULTIPLY'
                mul.inputs[1].default_value = factor
                mat.node_tree.links.new(output, mul.inputs[0])
                output = mul.outputs[0]
            if invert:
                inv = mat.node_tree.nodes.new('ShaderNodeMath')
                inv.operation = 'SUBTRACT'
                inv.inputs[0].default_value = 1
                mat.node_tree.links.new(output, inv.inputs[1])
                output = inv.outputs[0]
            return output

        if item['image'] is not None:
            tex = mapa(item['image'], info=item.get('textureInfo'))
            color = tex.outputs['Color']
            if item['factor'] != 1:
                factor = mat.node_tree.nodes.new('ShaderNodeMixRGB')
                factor.blend_type = 'MULTIPLY'
                factor.inputs[0].default_value = 1
                factor.inputs[2].default_value = (item['factor'], item['factor'], item['factor'], 1)
                mat.node_tree.links.new(color, factor.inputs[1])
                color = factor.outputs[0]
            mat.node_tree.links.new(color, shader.inputs['Base Color'])
        if item.get('vertexColor'):
            vcolor = mat.node_tree.nodes.new('ShaderNodeVertexColor')
            vcolor.layer_name = 'Color'
            multiply = mat.node_tree.nodes.new('ShaderNodeMixRGB')
            multiply.blend_type = 'MULTIPLY'
            multiply.inputs[0].default_value = 1
            if shader.inputs['Base Color'].is_linked:
                source = shader.inputs['Base Color'].links[0].from_socket
                mat.node_tree.links.new(source, multiply.inputs[1])
            else:
                multiply.inputs[1].default_value = shader.inputs['Base Color'].default_value
            mat.node_tree.links.new(vcolor.outputs['Color'], multiply.inputs[2])
            mat.node_tree.links.new(multiply.outputs[0], shader.inputs['Base Color'])
        channels = item.get('channels', {})
        for channel_name in ('AOPBR', 'CavityPBR'):
            channel = channels.get(channel_name)
            if not channel or channel.get('image') is None or channel.get('factor', 1) == 0:
                continue
            # Intensidade de oclusão: ler mapa em linear e interpolar desde branco.
            value = escalar(dict(channel, factor=1))
            amount = channel.get('factor', 1)
            if amount != 1:
                blend = mat.node_tree.nodes.new('ShaderNodeMath')
                blend.operation = 'MULTIPLY_ADD'
                mat.node_tree.links.new(value, blend.inputs[0])
                blend.inputs[1].default_value = amount
                blend.inputs[2].default_value = 1 - amount
                value = blend.outputs[0]
            multiply = mat.node_tree.nodes.new('ShaderNodeMixRGB')
            multiply.blend_type = 'MULTIPLY'
            multiply.inputs[0].default_value = 1
            multiply.label = channel_name
            if shader.inputs['Base Color'].is_linked:
                mat.node_tree.links.new(shader.inputs['Base Color'].links[0].from_socket, multiply.inputs[1])
            else:
                multiply.inputs[1].default_value = shader.inputs['Base Color'].default_value
            mat.node_tree.links.new(value, multiply.inputs[2])
            mat.node_tree.links.new(multiply.outputs[0], shader.inputs['Base Color'])
        for name, socket, invert in [('MetalnessPBR', 'Metallic', False), ('RoughnessPBR', 'Roughness', False), ('GlossinessPBR', 'Roughness', True), ('SpecularF0', 'Specular IOR Level', False), ('ClearCoat', 'Coat Weight', False), ('ClearCoatRoughness', 'Coat Roughness', False)]:
            ch = channels.get(name)
            if ch and ch['image'] is not None:
                mat.node_tree.links.new(escalar(ch, invert), shader.inputs[socket])
            elif ch and name in ('SpecularF0', 'ClearCoat', 'ClearCoatRoughness'):
                shader.inputs[socket].default_value = ch['factor']
        specular = channels.get('SpecularPBR')
        if specular and 'SpecularF0' not in channels:
            # Spec/gloss usa refletância RGB, diferente do nível F0 normalizado.
            color = specular.get('color') or [1, 1, 1]
            tint = shader.inputs.get('Specular Tint')
            if tint:
                tint.default_value = (*color[:3], 1)
            if specular.get('image') is None:
                shader.inputs['Specular IOR Level'].default_value = min(1, specular.get('factor', 1) / 0.08)
            else:
                mat.node_tree.links.new(escalar(dict(specular, factor=specular.get('factor', 1) / 0.08)), shader.inputs['Specular IOR Level'])
        opacity = channels.get('Opacity')
        mask = channels.get('AlphaMask')
        if opacity and opacity.get('type') == 'refraction':
            # Montagem completa no fim (precisa do normal map): ver bloco PBR_Refract abaixo.
            shader.inputs['Alpha'].default_value = 1
            mat.use_raytrace_refraction = True
            mat['source_refraction_roughness']=opacity.get('roughnessFactor') or 0
        elif opacity or mask:
            if opacity and opacity['image'] is None:
                shader.inputs['Alpha'].default_value = opacity['factor']
            elif opacity:
                mat.node_tree.links.new(escalar(opacity), shader.inputs['Alpha'])
            if mask and mask.get('image') is not None:
                cutoff = mat.node_tree.nodes.new('ShaderNodeMath')
                cutoff.operation = 'LESS_THAN'
                cutoff.label = 'Recorte da máscara'
                cutoff.inputs[1].default_value = mask.get('factor', 0.5)
                mat.node_tree.links.new(escalar(dict(mask, factor=1)), cutoff.inputs[0])
                keep = mat.node_tree.nodes.new('ShaderNodeMath')
                keep.operation = 'SUBTRACT'
                keep.inputs[0].default_value = 1
                mat.node_tree.links.new(cutoff.outputs[0],keep.inputs[1])
                alpha = keep.outputs[0]
                # Um atlas usado nos dois canais representa o mesmo recorte.
                same_map = opacity and opacity.get('image') == mask['image'] and all(opacity.get(k) == mask.get(k) for k in ('texCoord', 'transform', 'invert', 'component'))
                if opacity and (not same_map or opacity.get('factor', 1) != 1):
                    mul = mat.node_tree.nodes.new('ShaderNodeMath')
                    mul.operation = 'MULTIPLY'
                    mat.node_tree.links.new(alpha, mul.inputs[0])
                    if same_map or not shader.inputs['Alpha'].is_linked:
                        mul.inputs[1].default_value = opacity.get('factor', 1)
                    else:
                        mat.node_tree.links.new(shader.inputs['Alpha'].links[0].from_socket, mul.inputs[1])
                    alpha = mul.outputs[0]
                mat.node_tree.links.new(alpha, shader.inputs['Alpha'])
            prop = mat.bl_rna.properties.get('surface_render_method')
            if prop:
                modes = {entry.identifier for entry in prop.enum_items}
                method = 'BLENDED' if opacity and opacity.get('type') == 'alphaBlend' and not mask else 'DITHERED'
                if method in modes:
                    mat.surface_render_method = method
            if hasattr(mat, 'use_transparency_overlap'):
                # O viewer desenha alpha=1 num passe opaco e o translúcido depois. Material com
                # camada opaca relevante + parte translúcida (capa sob plástico) precisa ver as
                # camadas internas; nos demais, sobreposição desligada evita escurecer vidros.
                layered = (mat.surface_render_method == 'BLENDED' and opacity and opacity.get('image') is not None
                           and opaque_fraction(images[opacity['image']], opacity) >= LAYERED_OPAQUE_FRACTION)
                mat.use_transparency_overlap = bool(layered)
                if layered:
                    mat['linkv2_layered_transparency'] = True
        def apply_normal(normal, socket):
            tex = mapa(normal['image'], dados=True, info=normal)
            node = mat.node_tree.nodes.new('ShaderNodeNormalMap')
            unit = normal.get('texCoord', 0)
            node.uv_map = uv_name(unit)
            node.inputs['Strength'].default_value = normal['factor']
            color = tex.outputs['Color']
            if normal.get('flipY'):
                separate = mat.node_tree.nodes.new('ShaderNodeSeparateColor')
                combine = mat.node_tree.nodes.new('ShaderNodeCombineColor')
                invert = mat.node_tree.nodes.new('ShaderNodeMath')
                invert.operation = 'SUBTRACT'
                invert.inputs[0].default_value = 1
                mat.node_tree.links.new(color, separate.inputs['Color'])
                mat.node_tree.links.new(separate.outputs['Green'], invert.inputs[1])
                mat.node_tree.links.new(separate.outputs['Red'], combine.inputs['Red'])
                mat.node_tree.links.new(invert.outputs[0], combine.inputs['Green'])
                mat.node_tree.links.new(separate.outputs['Blue'], combine.inputs['Blue'])
                color = combine.outputs['Color']
            mat.node_tree.links.new(color, node.inputs['Color'])
            mat.node_tree.links.new(node.outputs['Normal'], shader.inputs[socket])
        normal = channels.get('NormalMap')
        bump = channels.get('BumpMap')
        if normal and normal['image'] is not None:
            apply_normal(normal, 'Normal')
        elif bump and bump['image'] is not None:
            node = mat.node_tree.nodes.new('ShaderNodeBump')
            node.inputs['Strength'].default_value = min(1, bump['factor'])
            node.inputs['Distance'].default_value = 0.001 * bump['factor']
            mat.node_tree.links.new(escalar(dict(bump, factor=1)), node.inputs['Height'])
            mat.node_tree.links.new(node.outputs['Normal'], shader.inputs['Normal'])
        coat_normal = channels.get('ClearCoatNormalMap')
        if channels.get('ClearCoat') and coat_normal and coat_normal.get('image') is not None:
            apply_normal(coat_normal, 'Coat Normal')
        def scalar_input(channel, socket_name):
            # Canal escalar do viewer: textura×fator quando houver mapa, senão só o fator.
            socket = shader.inputs.get(socket_name)
            if not socket or not channel:
                return
            if channel.get('image') is not None:
                mat.node_tree.links.new(escalar(channel), socket)
            else:
                socket.default_value = channel.get('factor', 0)
        sheen = channels.get('Sheen')
        if sheen and (sheen.get('factor') or 0) > 0:
            scalar_input(sheen, 'Sheen Weight')
            tint = sheen.get('colorFactor') or sheen.get('color')
            if tint and shader.inputs.get('Sheen Tint'):
                shader.inputs['Sheen Tint'].default_value = (*tint[:3], 1)
            scalar_input(channels.get('SheenRoughness'), 'Sheen Roughness')
        anisotropy = channels.get('Anisotropy')
        if anisotropy and (anisotropy.get('factor') or 0) > 0:
            scalar_input(anisotropy, 'Anisotropic')
            rotation = (anisotropy.get('source_properties') or {}).get('direction')
            if rotation is not None and shader.inputs.get('Anisotropic Rotation'):
                shader.inputs['Anisotropic Rotation'].default_value = (float(rotation) / 360.0) % 1.0
        scattering = channels.get('SubsurfaceScattering')
        if scattering and (scattering.get('factor') or 0) > 0:
            scalar_input(scattering, 'Subsurface Weight')
        displacement = channels.get('Displacement')
        if displacement and (displacement.get('factor') or 0) > 0 and displacement.get('image') is not None:
            output_node = next(n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL')
            # Viewer (vertex shader): posição_mundo += normal·(tex.r − 0.5)·fator, em unidades do
            # mundo do viewer. Em espaço do objeto, dividir pela escala do objeto capturado.
            offset = mat.node_tree.nodes.new('ShaderNodeDisplacement')
            offset.space = 'OBJECT'
            offset.inputs['Midlevel'].default_value = 0.5
            owner = next((m for m in data['meshes'] if m.get('material') == material_index), None)
            object_scale = 1.0
            if owner and owner.get('matrix'):
                matrix = owner['matrix']
                object_scale = max(math.sqrt(sum(matrix[offset_index + j] ** 2 for j in range(3))) for offset_index in (0, 4, 8)) or 1.0
            offset.inputs['Scale'].default_value = displacement['factor'] / object_scale
            mat.node_tree.links.new(escalar(dict(displacement, factor=1)), offset.inputs['Height'])
            mat.node_tree.links.new(offset.outputs['Displacement'], output_node.inputs['Displacement'])
            if hasattr(mat, 'displacement_method'):
                mat.displacement_method = 'BOTH'
        emission = channels.get('EmitColor')
        if emission and emission['factor'] > 0:
            shader.inputs['Emission Strength'].default_value = emission['factor']
            shader.inputs['Emission Color'].default_value = (*(emission['color'] or [1, 1, 1]), 1)
            if emission['image'] is not None:
                tex = mapa(emission['image'], info=emission)
                mat.node_tree.links.new(tex.outputs['Color'], shader.inputs['Emission Color'])
        if opacity and opacity.get('type') == 'refraction' and not item.get('shadeless'):
            # Shader do viewer (PBR_Refract): metal *= alpha; difuso = mix(refração, difuso, alpha);
            # especular somado. Alpha vem da opacidade (combinada no .a do albedo pelo viewer).
            tree = mat.node_tree
            output = next(n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL')
            if opacity.get('image') is not None:
                alpha = escalar(opacity)
            else:
                constant = tree.nodes.new('ShaderNodeValue')
                constant.outputs[0].default_value = opacity['factor']
                alpha = constant.outputs[0]
            metal = tree.nodes.new('ShaderNodeMath')
            metal.operation = 'MULTIPLY'
            metal.label = 'Metal × opacidade (viewer)'
            if shader.inputs['Metallic'].is_linked:
                tree.links.new(shader.inputs['Metallic'].links[0].from_socket, metal.inputs[0])
            else:
                metal.inputs[0].default_value = shader.inputs['Metallic'].default_value
            tree.links.new(alpha, metal.inputs[1])
            tree.links.new(metal.outputs[0], shader.inputs['Metallic'])
            ior = float(opacity.get('ior') or 1.45)
            if ior <= NEAR_UNIT_IOR and not (opacity.get('roughnessFactor') or 0):
                # IOR ~1 sem rugosidade: a refração do viewer quase não desvia o raio e mostra o que
                # está atrás. Transparente em BLENDED reproduz isso no EEVEE e no Cycles (a refração
                # do EEVEE não vê faces atrás na mesma região, ex.: íris sob a córnea).
                glass = tree.nodes.new('ShaderNodeBsdfTransparent')
                glass.label = 'Refração (IOR≈1)'
                tint = opacity.get('refractionColor') or (opacity.get('source_properties') or {}).get('refractionColor')
                if isinstance(tint, list) and len(tint) >= 3:
                    glass.inputs['Color'].default_value = (*tint[:3], 1)
                if 'BLENDED' in {e.identifier for e in mat.bl_rna.properties['surface_render_method'].enum_items}:
                    mat.surface_render_method = 'BLENDED'
                mat.use_raytrace_refraction = False
                mat['linkv2_refraction_see_through'] = True
            else:
                glass = tree.nodes.new('ShaderNodeBsdfPrincipled')
                glass.label = 'Refração'
                glass.inputs['Metallic'].default_value = 0
                glass.inputs['Transmission Weight'].default_value = 1
                glass.inputs['Roughness'].default_value = opacity.get('roughnessFactor') or 0
                glass.inputs['IOR'].default_value = ior
                if opacity.get('useAlbedoTint') and shader.inputs['Base Color'].is_linked:
                    tree.links.new(shader.inputs['Base Color'].links[0].from_socket, glass.inputs['Base Color'])
                else:
                    glass.inputs['Base Color'].default_value = (1, 1, 1, 1)
                for name in ('Coat Weight', 'Coat Roughness', 'Coat IOR', 'Normal', 'Coat Normal'):
                    source, target = shader.inputs.get(name), glass.inputs.get(name)
                    if not source or not target:
                        continue
                    if source.is_linked:
                        tree.links.new(source.links[0].from_socket, target)
                    elif hasattr(source, 'default_value') and name not in ('Normal', 'Coat Normal'):
                        target.default_value = source.default_value
                thin = glass.inputs.get('Thin Wall')
                if thin and opacity.get('thinLayer'):
                    thin.default_value = True
                    if output.inputs.get('Thickness'):
                        output.inputs['Thickness'].default_value = 0
                    if hasattr(mat, 'thickness_mode') and 'SLAB' in {e.identifier for e in mat.bl_rna.properties['thickness_mode'].enum_items}:
                        mat.thickness_mode = 'SLAB'
            mix = tree.nodes.new('ShaderNodeMixShader')
            mix.label = 'mix(refração, material, opacidade)'
            tree.links.new(alpha, mix.inputs[0])
            tree.links.new(glass.outputs[0], mix.inputs[1])
            tree.links.new(shader.outputs[0], mix.inputs[2])
            tree.links.new(mix.outputs[0], output.inputs['Surface'])
        translucency = channels.get('SubsurfaceTranslucency')
        if translucency and (translucency.get('factor') or 0) > 0 and not item.get('shadeless') and shader.inputs.get('Subsurface Weight'):
            # Viewer (computeLightSSS): fator·wrap(0.3−NoL)·luz·albedo·exp(−espessura/cor), espessura =
            # distância de sombra/thicknessFactor. Caminho livre médio = thicknessFactor·cor, que é o
            # subsurface do Principled: peso=fator, raio=cor, escala=thicknessFactor (unidades do mundo
            # do viewer; a escala do organizador é aplicada depois via linkv2_subsurface_viewer_scale).
            tint = translucency.get('color') or [1, 0.37, 0.3]
            thickness = float((translucency.get('source_properties') or {}).get('thicknessFactor') or 0)
            scalar_input(translucency, 'Subsurface Weight')
            shader.inputs['Subsurface Radius'].default_value = tuple(max(0.001, c) for c in tint[:3])
            viewer_scale = thickness if thickness > 0 else 0.01
            shader.inputs['Subsurface Scale'].default_value = viewer_scale
            mat['linkv2_subsurface_viewer_scale'] = viewer_scale
        if item.get('shadeless'):
            output = next(n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL')
            unlit = mat.node_tree.nodes.new('ShaderNodeEmission')
            unlit.inputs['Color'].default_value = (*[v * item['factor'] for v in item['color']], 1)
            if shader.inputs['Base Color'].is_linked:
                mat.node_tree.links.new(shader.inputs['Base Color'].links[0].from_socket, unlit.inputs['Color'])
            surface = unlit.outputs[0]
            if opacity or mask:
                transparent = mat.node_tree.nodes.new('ShaderNodeBsdfTransparent')
                mix = mat.node_tree.nodes.new('ShaderNodeMixShader')
                mat.node_tree.links.new(transparent.outputs[0], mix.inputs[1])
                mat.node_tree.links.new(surface, mix.inputs[2])
                if shader.inputs['Alpha'].is_linked:
                    mat.node_tree.links.new(shader.inputs['Alpha'].links[0].from_socket, mix.inputs[0])
                else:
                    mix.inputs[0].default_value = shader.inputs['Alpha'].default_value
                surface = mix.outputs[0]
            mat.node_tree.links.new(surface, output.inputs['Surface'])
        if opacity and opacity.get('type') == 'additive':
            # O viewer soma a luz dos pontos: preto não deve ocultar o fundo.
            output = next(n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL')
            surface = output.inputs['Surface'].links[0].from_socket
            if item.get('shadeless'):
                surface = unlit.outputs[0]
                if shader.inputs['Alpha'].is_linked:
                    mat.node_tree.links.new(shader.inputs['Alpha'].links[0].from_socket,unlit.inputs['Strength'])
                else:
                    unlit.inputs['Strength'].default_value = shader.inputs['Alpha'].default_value
            transparent = mat.node_tree.nodes.new('ShaderNodeBsdfTransparent')
            additive = mat.node_tree.nodes.new('ShaderNodeAddShader')
            mat.node_tree.links.new(transparent.outputs[0],additive.inputs[0])
            mat.node_tree.links.new(surface,additive.inputs[1])
            mat.node_tree.links.new(additive.outputs[0],output.inputs['Surface'])
            modes = {e.identifier for e in mat.bl_rna.properties['surface_render_method'].enum_items}
            if 'BLENDED' in modes:
                mat.surface_render_method = 'BLENDED'
            if hasattr(mat,'use_transparency_overlap'):
                mat.use_transparency_overlap = True
        materials.append(mat)
    objects = []
    mesh_cache = {}
    point_materials = {}
    opaque_twins = {}
    for mesh_index, item in enumerate(data['meshes']):
        reference = item.get('geometry_ref')
        if reference is not None and item.get('points'):
            mesh = mesh_cache[reference]
            obj = bpy.data.objects.new(item['name'],mesh)
            obj['source_mesh_index'] = mesh_index
            collection.objects.link(obj)
            obj.matrix_world = Matrix([item['matrix'][i:i+4] for i in range(0,16,4)]).transposed()
            primitives.display_points(obj,item,materials[item['material']],point_materials)
            mesh_cache[mesh_index] = mesh
            objects.append(obj)
            continue
        values = item['positions']
        verts = [values[i:i + 3] for i in range(0, len(values), 3)]
        mesh = bpy.data.meshes.new(item['name'])
        mesh.from_pydata(verts, [], item['triangles'])
        mesh.update()
        uv_layers = item.get('uvLayers') or ({'0': item['uv']} if item['uv'] else {})
        for unit, uv in uv_layers.items():
            layer = mesh.uv_layers.new(name='UVMap' if unit == '0' else 'UVMap' + unit)
            layer.data.foreach_set('uv', [v for loop in mesh.loops for v in uv[loop.vertex_index * 2:loop.vertex_index * 2 + 2]])
        material = materials[item['material']]
        if not uv_layers and any(n.type == 'TEX_IMAGE' for n in material.node_tree.nodes):
            # WebGL: atributo TexCoord ausente/desabilitado usa (0, 0), não é erro.
            # Manter mapas e transformações também em materiais compartilhados com peças com UV.
            names = {n.uv_map for n in material.node_tree.nodes if n.type == 'UVMAP'} or {'UVMap'}
            for name in sorted(names):
                layer = mesh.uv_layers.new(name=name)
                unit = name[len('UVMap'):] or '0'
                uv = item.get('constantUV', {}).get(unit, [0.0, 0.0])
                layer.data.foreach_set('uv', list(uv) * len(mesh.loops))
            mesh['linkv2_constant_uv'] = True
        if item.get('colors'):
            colors = item['colors']
            count = item.get('colorSize', 4)
            attribute = mesh.color_attributes.new(name='Color', type='FLOAT_COLOR', domain='POINT')
            values = [v for i in range(0, len(colors), count) for v in (colors[i:i+3] + [colors[i+3] if count == 4 else 1])]
            attribute.data.foreach_set('color_srgb' if data['materials'][item['material']].get('vertexColorSpace') == 'srgb' else 'color', values)
        for polygon in mesh.polygons:
            polygon.use_smooth = True
        if item['normals']:
            n = item['normals']
            mesh.normals_split_custom_set_from_vertices([Vector(n[i:i + 3]).normalized() for i in range(0, len(n), 3)])
        obj = bpy.data.objects.new(item['name'], mesh)
        obj['source_mesh_index'] = mesh_index
        collection.objects.link(obj)
        obj.matrix_world = Matrix([item['matrix'][i:i + 4] for i in range(0, 16, 4)]).transposed()
        mesh.materials.append(materials[item['material']])
        source_opacity = data['materials'][item['material']].get('channels', {}).get('Opacity') or {}
        if source_opacity.get('type') == 'refraction' and source_opacity.get('image') is not None:
            unit = source_opacity.get('texCoord', 0)
            split_opaque_refraction(mesh, materials[item['material']], images[source_opacity['image']], source_opacity,
                                    'UVMap' if unit == 0 else 'UVMap' + str(unit), opaque_twins)
        if item.get('points'):
            primitives.display_points(obj,item,materials[item['material']],point_materials)
        mesh_cache[mesh_index] = mesh
        objects.append(obj)
    model_root = bpy.data.objects.new('MODELO_' + data.get('uid', collection.name)[:8], None)
    collection.objects.link(model_root)
    for obj in objects:
        world = obj.matrix_world.copy()
        obj.parent = model_root
        obj.matrix_world = world
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.shading.type = 'MATERIAL'
                if any(s.name == 'studio.exr' for s in bpy.context.preferences.studio_lights):
                    area.spaces.active.shading.studiolight_rotate_z = 0
                    area.spaces.active.shading.studio_light = 'studio.exr'
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        bpy.ops.view3d.view_selected(use_all_regions=False)
    return {'objects': len(objects), 'triangles': sum(len(o.data.polygons) for o in objects), 'points':sum(o.get('source_point_count',0) for o in objects),'textures': len(images), 'collection': collection.name}
