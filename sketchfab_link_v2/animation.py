"""Reconstrói rigs e animações capturados pelo script 05 no Blender."""
import json
from collections import defaultdict
from pathlib import Path

import bpy
from mathutils import Matrix, Vector


def matriz(values):
    return Matrix([values[i:i+4] for i in range(0,16,4)]).transposed()


def nova_action(owner, name):
    owner.animation_data_create()
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    owner.animation_data.action = action
    return action


def repetir_action(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    if len(curve.keyframe_points)<2:continue
                    types={e.identifier for e in curve.modifiers.bl_rna.functions['new'].parameters['type'].enum_items}
                    if 'CYCLES' in types:curve.modifiers.new('CYCLES')


def importar_animacao(caminho, collection_name):
    data = json.loads(Path(caminho).read_text(encoding='utf-8'))
    if not data.get('animations'):
        raise ValueError('A captura não contém animação')
    collection = bpy.data.collections[collection_name]
    root = next(o for o in collection.objects if o.type == 'EMPTY' and o.name.startswith('MODELO_'))
    objects = {o['source_mesh_index']: o for o in collection.objects if o.type == 'MESH' and 'source_mesh_index' in o}
    if len(objects) != len(data['meshes']):
        raise ValueError('Índices de malha ausentes; reimportar com o script 02 atualizado')
    inverse_parent = next(iter(objects.values())).matrix_parent_inverse.copy()
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    rigs = []
    names_by_rig = []
    for rig_index, rig in enumerate(data.get('rigs', [])):
        armature = bpy.data.armatures.new(rig['name'])
        obj = bpy.data.objects.new(rig['name'] + '_RIG', armature)
        collection.objects.link(obj)
        obj.parent = root
        obj.matrix_parent_inverse = inverse_parent.copy()
        obj.matrix_basis = matriz(rig['world'])
        # Exibição discreta; não alterar escala da pose para ajustar o desenho dos ossos.
        armature.display_type = 'STICK'
        obj.show_in_front = False
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        names = {}
        rests = {b['id']: matriz(b['inverseBind']).inverted() for b in rig['bones']}
        # Comprimento só afeta a exibição (a deformação usa as matrizes de bind). Limitar a uma
        # fração do esqueleto evita ossos-raiz gigantes; folhas herdam metade do pai.
        heads = [m.translation for m in rests.values()]
        extent = max(((a - b).length for a in heads for b in heads[:64]), default=1.0) or 1.0
        cap = 0.12 * extent
        wanted = {}
        for bone in rig['bones']:
            head = rests[bone['id']].translation
            children = [rests[b['id']].translation for b in rig['bones'] if b.get('parent') == bone['id']]
            distances = [(p - head).length for p in children if (p - head).length > 1e-4 * extent]
            wanted[bone['id']] = min(min(distances), cap) if distances else None
        for bone in rig['bones']:
            if wanted[bone['id']] is None:
                parent_length = wanted.get(bone.get('parent'))
                wanted[bone['id']] = 0.5 * parent_length if parent_length else 0.05 * extent
        for bone in rig['bones']:
            edit = armature.edit_bones.new(bone['name'])
            names[bone['id']] = edit.name
            edit.length = 1.0
            edit.matrix = rests[bone['id']]
            edit.length = max(wanted[bone['id']], 1e-4 * extent)
        for bone in rig['bones']:
            if bone.get('parent') in names:
                armature.edit_bones[names[bone['id']]].parent = armature.edit_bones[names[bone['parent']]]
        # O inverse bind pode conter escala (olhos com pose 100x, por exemplo).
        # Limitar pelo comprimento efetivamente desenhado em todos os clipes, mantendo
        # cabeças, orientações, bind e animação intactos.
        inv_binds = [matriz(b['inverseBind']) for b in rig['bones']]
        edits = [armature.edit_bones[names[b['id']]] for b in rig['bones']]
        parent_ids = {b.get('parent') for b in rig['bones']}
        limits = [e.length for e in edits]
        rest_matrices = [e.matrix.copy() for e in edits]
        for clip in data['animations']:
            for frame in clip['frames']:
                values = frame['bones'][rig_index]
                poses = [matriz(values[i*16:i*16+16]) @ inv_binds[i] @ rest_matrices[i]
                         for i in range(len(edits))]
                heads = [m.translation for m in poses]
                span = Vector([max(p[a] for p in heads)-min(p[a] for p in heads)
                               for a in range(3)]).length if heads else 0.0
                if span <= 1e-12:
                    continue
                for i, pose in enumerate(poses):
                    scale = pose.col[1].to_3d().length
                    if scale <= 1e-12:
                        continue
                    limit = 0.12 * span
                    if rig['bones'][i]['id'] not in parent_ids:
                        distances = [(head-heads[i]).length for j, head in enumerate(heads)
                                     if j != i and (head-heads[i]).length > span*1e-6]
                        if distances:
                            limit = min(limit, 0.5*min(distances))
                    limits[i] = min(limits[i], limit/scale)
        for edit, length in zip(edits, limits):
            edit.length = max(length, 1e-8)
        bpy.ops.object.mode_set(mode='OBJECT')
        obj.select_set(False)
        for bone in obj.pose.bones:
            bone.rotation_mode = 'QUATERNION'
        rigs.append(obj)
        names_by_rig.append(names)
    morph_keys = {}
    for mesh_index, source in enumerate(data['meshes']):
        obj = objects[mesh_index]
        skin = source.get('skin')
        if skin:
            rig_index = skin['rig']
            palette = skin['palette']
            names = names_by_rig[rig_index]
            groups = {bid: obj.vertex_groups.new(name=names[bid]) for bid in palette}
            buckets = defaultdict(list)
            joints, weights = skin['joints'], skin['weights']
            if len(weights) != len(obj.data.vertices)*4 or len(joints) != len(weights):
                raise ValueError('Pesos incompatíveis com a malha ' + obj.name)
            for vertex in range(len(obj.data.vertices)):
                start = vertex*4
                total = sum(weights[start:start+4])
                if total <= 0:
                    continue
                for i in range(4):
                    weight = weights[start+i]/total
                    if weight > 0:
                        bid = palette[joints[start+i]]
                        buckets[(bid, round(weight, 8))].append(vertex)
            for (bid, weight), vertices in buckets.items():
                groups[bid].add(vertices, weight, 'REPLACE')
            modifier = obj.modifiers.new('Rig capturado', 'ARMATURE')
            modifier.object = rigs[rig_index]
            modifier.use_deform_preserve_volume = False
        if source.get('morphs'):
            obj.shape_key_add(name='Basis')
            keys = []
            for morph in source['morphs']:
                key = obj.shape_key_add(name=morph['name'])
                if len(morph['positions']) != len(obj.data.vertices)*3:
                    raise ValueError('Morph incompatível com ' + obj.name)
                key.data.foreach_set('co', morph['positions'])
                keys.append(key)
            morph_keys[mesh_index] = keys
    previous_interpolation = bpy.context.preferences.edit.keyframe_new_interpolation_type
    restore_actions = []
    clip_bindings = []
    try:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = 'LINEAR'
        for clip_index, clip in enumerate(data['animations']):
            actions_before = set(bpy.data.actions)
            frames = clip['frames']
            bone_tracks = []
            for rig_index, (source, obj, names) in enumerate(zip(data.get('rigs', []), rigs, names_by_rig)):
                action = nova_action(obj, data['title'] + ' | ' + clip['name'])
                if clip_index == 0:
                    restore_actions.append((obj, action))
                index_of = {b['id']: i for i,b in enumerate(source['bones'])}
                inv_binds = {b['id']: matriz(b['inverseBind']) for b in source['bones']}
                rests = {b['id']: obj.data.bones[names[b['id']]].matrix_local.copy() for b in source['bones']}
                tracks = []
                for bone_index, bone in enumerate(source['bones']):
                    pose = obj.pose.bones[names[bone['id']]]
                    rest = rests[bone['id']]
                    parent_index = index_of.get(bone.get('parent'))
                    values = []
                    for frame in frames:
                        matrices = frame['bones'][rig_index]
                        target = matriz(matrices[bone_index*16:bone_index*16+16]) @ inv_binds[bone['id']] @ rest
                        if parent_index is None:
                            basis = pose.bone.convert_local_to_pose(target, rest, invert=True)
                        else:
                            parent = obj.pose.bones[names[bone['parent']]]
                            parent_target = matriz(matrices[parent_index*16:parent_index*16+16]) @ inv_binds[bone['parent']] @ rests[bone['parent']]
                            basis = pose.bone.convert_local_to_pose(target, rest, parent_matrix=parent_target,
                                parent_matrix_local=parent.bone.matrix_local, invert=True)
                        values.append(basis)
                    first = values[0]
                    moving = any(max(abs(matrix[r][c]-first[r][c]) for r in range(4) for c in range(4)) > 1e-6 for matrix in values[1:])
                    tracks.append((pose, values, moving))
                bone_tracks.append(tracks)
            for frame_index, frame in enumerate(frames):
                number = frame_index+1
                for rig_index, obj in enumerate(rigs):
                    obj.matrix_basis = matriz(frame['rigWorlds'][rig_index])
                    if frame_index == 0 or frame['rigWorlds'][rig_index] != frames[frame_index-1]['rigWorlds'][rig_index]:
                        obj.keyframe_insert('location', frame=number)
                        obj.keyframe_insert('rotation_euler', frame=number)
                        obj.keyframe_insert('scale', frame=number)
                    for pose, values, moving in bone_tracks[rig_index]:
                        if not moving and frame_index:
                            continue
                        old_quaternion = pose.rotation_quaternion.copy()
                        pose.matrix_basis = values[frame_index]
                        if frame_index and old_quaternion.dot(pose.rotation_quaternion) < 0:
                            pose.rotation_quaternion.negate()
                        pose.keyframe_insert('location', frame=number, group=pose.name)
                        pose.keyframe_insert('rotation_quaternion', frame=number, group=pose.name)
                        pose.keyframe_insert('scale', frame=number, group=pose.name)
                for mesh_index, keys in morph_keys.items():
                    owner = objects[mesh_index].data.shape_keys
                    if frame_index == 0:
                        action = nova_action(owner, data['title'] + ' | ' + clip['name'] + ' | ' + objects[mesh_index].name)
                        if clip_index == 0:
                            restore_actions.append((owner, action))
                    weights = frame['morphs'][mesh_index]
                    for key, value in zip(keys, weights):
                        key.slider_min = min(key.slider_min, value)
                        key.slider_max = max(key.slider_max, value)
                        key.value = value
                        key.keyframe_insert('value', frame=number)
            # Objetos rígidos também podem ter movimento fora dos ossos.
            for mesh_index, obj in objects.items():
                matrices = [f['meshes'][mesh_index] for f in frames]
                if not any(m != matrices[0] for m in matrices[1:]):
                    continue
                action = nova_action(obj, data['title'] + ' | ' + clip['name'] + ' | ' + obj.name)
                if clip_index == 0:
                    restore_actions.append((obj, action))
                obj.rotation_mode = 'QUATERNION'
                for frame_index, matrix in enumerate(matrices):
                    obj.matrix_basis = matriz(matrix)
                    for channel in ('location', 'rotation_quaternion', 'scale'):
                        obj.keyframe_insert(channel, frame=frame_index+1)
            owners = rigs + list(objects.values()) + [objects[i].data.shape_keys for i in morph_keys]
            if clip.get('loop'):
                for action in set(bpy.data.actions)-actions_before:repetir_action(action)
            bindings = []
            for owner in owners:
                action = owner.animation_data.action if owner.animation_data else None
                if action and action not in actions_before:
                    action['source_uid'] = data['uid']
                    action['clip_uid'] = str(clip['uid'])
                    bindings.append({'kind': 'shape_keys' if isinstance(owner, bpy.types.Key) else 'objects', 'owner': owner.name, 'action': action.name})
            clip_bindings.append({'name': clip['name'], 'uid': clip['uid'], 'fps': clip['fps'], 'frames': len(frames), 'bindings': bindings})
        collection['clip_bindings'] = json.dumps(clip_bindings, ensure_ascii=False)
        for owner, action in restore_actions:
            owner.animation_data.action = action
            if len(action.slots):
                owner.animation_data.action_slot = action.slots[0]
        collection['animation_bounds'] = data['animation_bounds']
        collection['animation_clips'] = json.dumps([{k:c[k] for k in ('name','uid','duration','fps')} for c in data['animations']], ensure_ascii=False)
        scene = bpy.context.scene
        scene.render.fps = data['animations'][0]['fps']
        scene.frame_start = 1
        scene.frame_end = len(data['animations'][0]['frames'])
        scene.frame_set(1)
    finally:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = previous_interpolation
    return {'rigs':len(rigs),'bones':sum(len(r.data.bones) for r in rigs),
            'clips':len(data['animations']),'frames':len(data['animations'][0]['frames']),
            'morph_meshes':len(morph_keys)}


def ativar_clip(collection, index):
    clips = json.loads(collection['clip_bindings'])
    clip = clips[index]
    for binding in clip['bindings']:
        owner = getattr(bpy.data, binding['kind'])[binding['owner']]
        action = bpy.data.actions[binding['action']]
        owner.animation_data_create()
        owner.animation_data.action = action
        if len(action.slots):
            owner.animation_data.action_slot = action.slots[0]
    bpy.context.scene.render.fps = clip['fps']
    bpy.context.scene.render.fps_base = 1
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = clip['frames']
    bpy.context.scene.frame_set(1)
    return clip


def validar_animacao(caminho, collection_name, tolerance=0.01):
    data = json.loads(Path(caminho).read_text(encoding='utf-8'))
    collection = bpy.data.collections[collection_name]
    root = next(o for o in collection.objects if o.type == 'EMPTY' and o.name.startswith('MODELO_'))
    objects = {o['source_mesh_index']:o for o in collection.objects if o.type == 'MESH' and 'source_mesh_index' in o}
    child = next(iter(objects.values()))
    transform = root.matrix_world @ child.matrix_parent_inverse
    errors = []
    frames_checked = 0
    clips_checked = 0
    warnings = []
    displays = [mod for obj in objects.values() for mod in obj.modifiers if mod.type=='NODES' and mod.node_group and mod.node_group.get('linkv2_display_only')]
    display_states = [(mod,mod.show_viewport) for mod in displays]
    for mod in displays:
        mod.show_viewport = False
    try:
        for index, clip in enumerate(data['animations']):
            ativar_clip(collection, index)
            samples = clip.get('samples', [])
            if not samples:
                warnings.append('Clipe sem amostras de validação: ' + clip['name'])
                continue
            clips_checked += 1
            frames_checked += len({s['frame'] for s in samples})
            for frame in sorted({s['frame'] for s in samples}):
                bpy.context.scene.frame_set(frame)
                depsgraph = bpy.context.evaluated_depsgraph_get()
                for sample in (s for s in samples if s['frame'] == frame):
                    obj = objects[sample['mesh']]
                    evaluated = obj.evaluated_get(depsgraph)
                    mesh = evaluated.to_mesh()
                    try:
                        for vertex in sample['vertices']:
                            actual = evaluated.matrix_world @ mesh.vertices[vertex['index']].co
                            expected = transform @ Vector(vertex['position'])
                            error = (actual-expected).length
                            if not __import__('math').isfinite(error):
                                raise ValueError('Deformação não finita')
                            errors.append(error)
                    finally:
                        evaluated.to_mesh_clear()
    finally:
        ativar_clip(collection, 0)
        for mod,state in display_states:
            mod.show_viewport = state
    maximum = max(errors,default=0)
    result = {'vertices_checked':len(errors),'frames_checked':frames_checked,'clips_checked':clips_checked,'max_position_error':maximum if errors else None,'tolerance':tolerance}
    if warnings:
        result['warnings'] = warnings
    collection['animation_validation'] = json.dumps(result)
    bpy.context.scene.frame_set(1)
    if maximum > tolerance:
        raise RuntimeError('A animação difere do viewer: ' + json.dumps(result))
    return result
