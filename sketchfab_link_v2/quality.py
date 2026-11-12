"""Organização espacial e verificações do lote dentro do Blender."""
import json
import math
from itertools import combinations

import bpy
from mathutils import Vector, Matrix


def isolar(collection):
    states = {}
    for layer in bpy.context.view_layer.layer_collection.children:
        if layer.collection.get('source_uid'):
            states[layer.name] = layer.hide_viewport
            layer.hide_viewport = layer.collection != collection
    return states


def restaurar_visibilidade(states):
    for layer in bpy.context.view_layer.layer_collection.children:
        if layer.name in states:
            layer.hide_viewport = states[layer.name]


def limites(collection):
    bounds = collection.get('animation_bounds')
    if bounds:
        root = next(o for o in collection.objects if o.type == 'EMPTY' and o.name.startswith('MODELO_'))
        child = next(o for o in collection.objects if o.type == 'MESH')
        transform = root.matrix_world @ child.matrix_parent_inverse
        points = [transform @ Vector((x,y,z)) for x in (bounds[0],bounds[3]) for y in (bounds[1],bounds[4]) for z in (bounds[2],bounds[5])]
        return Vector([min(p[i] for p in points) for i in range(3)]), Vector([max(p[i] for p in points) for i in range(3)])
    points = [obj.matrix_world @ Vector(corner) for obj in collection.all_objects
              if obj.type == 'MESH' for corner in obj.bound_box]
    if not points:
        raise ValueError('Coleção sem malhas: ' + collection.name)
    return Vector([min(p[i] for p in points) for i in range(3)]), Vector([max(p[i] for p in points) for i in range(3)])


def organizar(scene, colunas=4):
    candidates = [c for c in scene.collection.children if c.get('source_url')]
    collections = [c for c in candidates if any(o.type == 'MESH' and len(o.data.vertices) for o in c.all_objects)]
    ignored = [c.name for c in candidates if c not in collections]
    for collection in collections:
        root = next((o for o in collection.objects if o.type == 'EMPTY' and o.name.startswith('MODELO_')), None)
        if root:
            mn, mx = limites(collection)
            old_root = root.matrix_world.copy()
            root.matrix_world.translation = (mn + mx) * 0.5
            bpy.context.view_layer.update()
            correction = root.matrix_world.inverted() @ old_root
            for obj in collection.objects:
                if obj.parent == root:
                    obj.matrix_parent_inverse = correction @ obj.matrix_parent_inverse
            bpy.context.view_layer.update()
            extent = max(mx - mn)
            factor = 10.0 / extent if extent > 1e-9 else 1.0
            root.scale *= factor
            collection['gallery_scale'] = collection.get('gallery_scale', 1.0) * factor
            bpy.context.view_layer.update()
    cursor_x = cursor_y = altura_fila = 0.0
    for index, collection in enumerate(collections):
        if index and index % colunas == 0:
            cursor_x = 0.0
            cursor_y += altura_fila
            altura_fila = 0.0
        mn, mx = limites(collection)
        size = mx - mn
        gap = max(3.0, max(size.x, size.y) * 0.08)
        delta = Vector((cursor_x - mn.x, cursor_y - mn.y, -mn.z))
        for obj in collection.all_objects:
            if obj.parent is None:
                obj.matrix_world.translation += delta
        collection['layout_offset'] = list(Vector(collection.get('layout_offset', (0, 0, 0))) + delta)
        cursor_x += size.x + gap
        altura_fila = max(altura_fila, size.y + gap)
    bpy.context.view_layer.update()
    boxes = {c.name: limites(c) for c in collections}
    overlaps = []
    for (a, (amn, amx)), (b, (bmn, bmx)) in combinations(boxes.items(), 2):
        if amn.x < bmx.x - 1e-4 and amx.x > bmn.x + 1e-4 and amn.y < bmx.y - 1e-4 and amx.y > bmn.y + 1e-4:
            overlaps.append([a, b])
    if overlaps:
        raise RuntimeError('Coleções sobrepostas: ' + str(overlaps))
    return {'collections': len(collections), 'overlaps': overlaps, 'ignored_empty': ignored}


def posicionar_novo(collection, scene):
    """Só posiciona a nova importação. Não move nem oculta objetos existentes."""
    root = next(o for o in collection.objects if o.type == 'EMPTY' and o.name.startswith('MODELO_'))
    mn, mx = limites(collection)
    old_root = root.matrix_world.copy()
    root.matrix_world.translation = (mn + mx) * 0.5
    bpy.context.view_layer.update()
    correction = root.matrix_world.inverted() @ old_root
    for obj in collection.objects:
        if obj.parent == root:
            obj.matrix_parent_inverse = correction @ obj.matrix_parent_inverse
    extent = max(mx - mn)
    factor = 10.0 / extent if extent > 1e-9 else 1.0
    root.scale *= factor
    collection['gallery_scale'] = collection.get('gallery_scale', 1) * factor
    bpy.context.view_layer.update()
    occupied = []
    new_objects = set(collection.all_objects)
    animated_objects = set()
    for other in scene.collection.children:
        if other != collection and other.get('animation_bounds') and any(o.type == 'MESH' for o in other.all_objects):
            occupied.append(limites(other))
            animated_objects.update(other.all_objects)
    for obj in scene.objects:
        if obj in new_objects or obj in animated_objects or obj.type != 'MESH' or not len(obj.data.vertices):
            continue
        points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        occupied.append((Vector([min(p[i] for p in points) for i in range(3)]), Vector([max(p[i] for p in points) for i in range(3)])))
    mn, mx = limites(collection)
    size = mx - mn
    gap = 3.0
    for index in range(10000):
        origin = Vector(((index % 4) * 13.0, (index // 4) * 13.0, 0))
        end = origin + size
        overlaps = any(origin.x < b.x + gap and end.x > a.x - gap and origin.y < b.y + gap and end.y > a.y - gap for a, b in occupied)
        if not overlaps:
            delta = origin - mn
            root.matrix_world.translation += delta
            collection['layout_offset'] = list(delta)
            bpy.context.view_layer.update()
            return {'overlaps': [], 'existing_objects_moved': 0, 'slot': index}
    raise RuntimeError('Sem espaço disponível para posicionar o modelo')


def validar(collection):
    errors = []
    checked_images = set()
    objects = [o for o in collection.all_objects if o.type == 'MESH']
    if not objects:
        raise RuntimeError('A coleção importada não contém malhas')
    for obj in objects:
        mesh = obj.data
        if not mesh.polygons and not obj.get('source_point_count'):
            errors.append(obj.name + ': sem faces')
        if any(not math.isfinite(v) for vertex in mesh.vertices for v in vertex.co):
            errors.append(obj.name + ': coordenadas inválidas')
        if mesh.uv_layers.active and any(not math.isfinite(v) for uv in mesh.uv_layers.active.data for v in uv.uv):
            errors.append(obj.name + ': UV inválida')
        for mat in mesh.materials:
            if not mat or not mat.use_nodes:
                continue
            reachable = set()
            pending = [n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output]
            while pending:
                node = pending.pop()
                if node in reachable:
                    continue
                reachable.add(node)
                pending.extend(link.from_node for socket in node.inputs for link in socket.links)
            textures = [n for n in reachable if n.type == 'TEX_IMAGE']
            # Texturas podem usar coordenadas constantes, geradas ou de objeto sem UV.
            for node in textures:
                image = node.image
                if image and image not in checked_images:
                    # O Blender carrega buffers de imagem sob demanda.
                    pixel_count = len(image.pixels)
                    checked_images.add(image)
                    if pixel_count == 0:
                        errors.append(obj.name + ': buffer de imagem vazio')
                if not image or not all(image.size):
                    errors.append(obj.name + ': imagem ausente ou vazia')
    if errors:
        raise RuntimeError('\n'.join(errors))
    return {'objects': len(objects), 'triangles': sum(len(o.data.polygons) for o in objects), 'errors': errors}


def enquadrar(collection):
    # A câmera do viewer usa normalização própria; não somar offsets à câmera bruta.
    # Uma vista geral estável evita posicionar a inspeção abaixo do modelo.
    mn, mx = limites(collection)
    center = (mn + mx) * 0.5
    size = (mx - mn).length
    distance = size * 1.2
    rotation = Vector((-1, 1, -0.65)).to_track_quat('-Z', 'Y')
    target = center
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                space = area.spaces.active
                space.shading.type = 'MATERIAL'
                space.region_3d.view_rotation = rotation
                space.region_3d.view_location = target
                space.region_3d.view_distance = distance
                space.region_3d.view_perspective = 'PERSP'
                area.tag_redraw()
