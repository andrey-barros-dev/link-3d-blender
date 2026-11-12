"""Nuvem de pontos nativa, cores por ponto e avaliação só dos quadros ativos."""
import bpy


def display_points(obj, source, material, cache):
    key = material.name
    if key not in cache:
        point_mat = material.copy()
        point_mat.name = material.name + ' • pontos'
        point_mat.use_backface_culling = True
        for node in list(point_mat.node_tree.nodes):
            if node.type != 'VERTEX_COLOR':
                continue
            attribute = point_mat.node_tree.nodes.new('ShaderNodeAttribute')
            attribute.attribute_name = 'Color'
            attribute.attribute_type = 'GEOMETRY'
            for output in node.outputs:
                for link in list(output.links):
                    replacement = attribute.outputs.get(output.name)
                    if replacement:
                        point_mat.node_tree.links.new(replacement, link.to_socket)
        cache[key] = point_mat
    point_mat = cache[key]
    indices = source['points']
    if len(indices) != len(obj.data.vertices):
        attribute = obj.data.attributes.get('linkv2_point') or obj.data.attributes.new('linkv2_point','BOOLEAN','POINT')
        selected = set(indices)
        attribute.data.foreach_set('value', [i in selected for i in range(len(obj.data.vertices))])
    scale = source.get('_point_reference_scale',max(abs(v) for v in obj.matrix_world.to_scale()))
    radius = source.get('point_radius',0.003) / (scale if scale > 0 else 1)
    group_key = ('group',point_mat.name,round(radius,10),len(indices)!=len(obj.data.vertices))
    if group_key in cache:
        group, visible_identifier = cache[group_key]
        modifier = obj.modifiers.new('Link V2 • pontos','NODES')
        modifier.node_group = group
        _visibility_driver(obj,modifier,visible_identifier,source.get('_point_visibility_threshold',0))
        obj['source_point_count'] = len(indices)
        return modifier
    group = bpy.data.node_groups.new('Link V2 • pontos', 'GeometryNodeTree')
    group['linkv2_display_only'] = True
    group.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    visible_socket = group.interface.new_socket(name='Visible',in_out='INPUT',socket_type='NodeSocketBool')
    visible_socket.default_value = True
    incoming = group.nodes.new('NodeGroupInput')
    outgoing = group.nodes.new('NodeGroupOutput')
    points = group.nodes.new('GeometryNodeMeshToPoints')
    modes = {p.identifier for p in points.bl_rna.properties['mode'].enum_items}
    if 'VERTICES' in modes: points.mode = 'VERTICES'
    points.inputs['Radius'].default_value = radius
    group.links.new(incoming.outputs['Geometry'],points.inputs['Mesh'])
    set_material = group.nodes.new('GeometryNodeSetMaterial')
    set_material.inputs['Material'].default_value = point_mat
    group.links.new(points.outputs['Points'],set_material.inputs['Geometry'])
    if len(indices) != len(obj.data.vertices):
        selection = group.nodes.new('GeometryNodeInputNamedAttribute')
        selection.data_type = 'BOOLEAN'
        selection.inputs['Name'].default_value = 'linkv2_point'
        group.links.new(selection.outputs['Attribute'],points.inputs['Selection'])
    switch = group.nodes.new('GeometryNodeSwitch')
    switch.input_type = 'GEOMETRY'
    group.links.new(incoming.outputs['Visible'],switch.inputs['Switch'])
    group.links.new(set_material.outputs['Geometry'],switch.inputs['True'])
    group['linkv2_point_backend'] = 'NATIVE_POINTCLOUD'
    group.links.new(switch.outputs['Output'],outgoing.inputs['Geometry'])
    modifier = obj.modifiers.new('Link V2 • pontos','NODES')
    modifier.node_group = group
    _visibility_driver(obj,modifier,visible_socket.identifier,source.get('_point_visibility_threshold',0))
    cache[group_key] = (group,visible_socket.identifier)
    obj['source_point_count'] = len(indices)
    return modifier


def _visibility_driver(obj,modifier,identifier,threshold):
    # Frames inativos do viewer usam escala ~1e-10. A seleção no início do
    # grupo é avaliada de forma preguiçosa: não cria instâncias desses frames.
    properties = getattr(modifier,'properties',None)
    inputs = getattr(properties,'inputs',None)
    if inputs is not None:
        socket = getattr(inputs,identifier)
        socket.value = True
        if threshold<=0:return
        curve = socket.driver_add('value')
    else:
        modifier[identifier] = True
        if threshold<=0:return
        curve = modifier.driver_add('["'+identifier+'"]')
    driver = curve.driver
    for variable in list(driver.variables):driver.variables.remove(variable)
    for axis,name in enumerate(('sx','sy','sz')):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id = obj
        variable.targets[0].data_path = 'scale['+str(axis)+']'
    driver.expression = 'max(abs(sx),abs(sy),abs(sz)) > '+repr(threshold)
