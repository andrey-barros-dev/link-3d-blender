"""Fatos observados na captura. Presença de recurso não significa aprovação."""


def identify(data):
    features={'geometry.triangles'} if any(m.get('triangles') for m in data['meshes']) else set()
    if any(m.get('points') or m.get('point_count') for m in data['meshes']):features.add('geometry.points')
    if any(m.get('geometry_ref') is not None or m.get('instanceIndex',0)>0 for m in data['meshes']):features.add('geometry.instances')
    if any(len(m.get('uvLayers',{}))>1 for m in data['meshes']):features.add('uv.multiple_sets')
    if any(m.get('colors') for m in data['meshes']):features.add('material.vertex_colors')
    if data.get('images'):
        features.add('texture.images')
        if all(i.get('expected_width') and i['width']>=i['expected_width'] and i['height']>=i['expected_height'] for i in data['images']):
            features.add('texture.full_resolution')
    mapping={'NormalMap':'material.normal_map','BumpMap':'material.bump_map','AlphaMask':'material.alpha_mask','ClearCoat':'material.coat',
             'EmitColor':'material.emission','SpecularPBR':'material.spec_gloss','SubsurfaceTranslucency':'material.translucency','AOPBR':'material.ao','CavityPBR':'material.cavity'}
    for material in data['materials']:
        if material.get('shadeless'):features.add('material.unlit')
        if material.get('workflow'):features.add('workflow.'+material['workflow'])
        for name,channel in material.get('channels',{}).items():
            if name in mapping and (channel.get('factor',1)>0 or channel.get('image') is not None):features.add(mapping[name])
            if name=='Opacity':features.add('opacity.'+str(channel.get('type','unknown')))
            transform=channel.get('transform') or {}
            if transform.get('scale',[1,1])!=[1,1] or transform.get('offset',[0,0])!=[0,0] or transform.get('rotation',0):features.add('uv.transforms')
            if channel.get('wrapS',10497)!=10497 or channel.get('wrapT',10497)!=10497:features.add('uv.wrap_nonrepeat')
    if (data.get('environment') or {}).get('cubemap'):features.add('environment.hdri')
    clips=data.get('animations',[])
    if clips:features.add('animation.present')
    if len(clips)>1:features.add('animation.multiclip')
    if data.get('rigs'):features.add('animation.skin')
    if any(m.get('morphs') for m in data['meshes']):features.add('animation.morphs')
    return {'observed':sorted(features),'visually_verified':False}
