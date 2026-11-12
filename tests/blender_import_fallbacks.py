"""Regressões para modelos válidos sem UV e sem amostras opcionais de conferência."""
import copy
import json
import sys
import tempfile
from pathlib import Path

import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from sketchfab_link_v2 import quality, service

matrix = [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]
uid = 'c'*32
data = {'version':1,'uid':uid,'source':'https://sketchfab.com/3d-models/'+uid,
        'title':'UV fallback regression','author':'Test','license':'Test',
        'images':[{'uid':'d'*32,'width':2,'height':2}],
        'materials':[{'name':'Shared texture','image':0,'color':[1,1,1],
                      'factor':1,'metallic':0,'roughness':0.5,
                      'textureInfo':{'texCoord':0}}],
        'meshes':[{'name':'No UV','positions':[0,0,0,1,0,0,0,1,0],
                   'triangles':[[0,1,2]],'normals':None,'uv':None,
                   'material':0,'matrix':matrix}]}

with tempfile.TemporaryDirectory(prefix='linkv2-uv-') as directory:
    folder = Path(directory)
    image = bpy.data.images.new('Test texture', width=2, height=2)
    image.pixels[:] = [1,0,0,1]*4
    image.filepath_raw = str(folder/('d'*32+'.png'))
    image.file_format = 'PNG'
    image.save()
    bpy.data.images.remove(image)
    path = folder/'modelo.json'

    for shared, constant in ((False, [0.0,0.0]), (True, [0.0,0.0]), (True, [0.1,0.3])):
        source = copy.deepcopy(data)
        source['meshes'][0]['constantUV'] = {'0':constant}
        if shared:
            mesh = copy.deepcopy(source['meshes'][0])
            mesh.update(name='With UV',uv=[0,0,1,0,0,1])
            source['meshes'].append(mesh)
        path.write_text(json.dumps(source), encoding='utf-8')
        result = service.import_capture(path)
        collection = bpy.data.collections[result['collection']]
        obj = next(o for o in collection.objects if o.type=='MESH' and o['source_mesh_index']==0)
        assert obj.data.get('linkv2_constant_uv')
        assert all(max(abs(uv.uv[i]-constant[i]) for i in range(2))<1e-6 for uv in obj.data.uv_layers[0].data)
        assert any(n.type=='TEX_IMAGE' for n in obj.data.materials[0].node_tree.nodes)
        assert not result['validation']['errors']
        if shared:
            textured = next(o for o in collection.objects if o.type=='MESH' and o['source_mesh_index']==1)
            assert any(tuple(uv.uv)!=(0.0,0.0) for uv in textured.data.uv_layers[0].data)
            assert textured.data.materials[0] == obj.data.materials[0]
        # Coordenadas Generated não exigem UV e também não podem ser rejeitadas.
        while obj.data.uv_layers:
            obj.data.uv_layers.remove(obj.data.uv_layers[0])
        mat = obj.data.materials[0]
        texture = next(n for n in mat.node_tree.nodes if n.type=='TEX_IMAGE')
        coordinates = mat.node_tree.nodes.new('ShaderNodeTexCoord')
        mat.node_tree.links.new(coordinates.outputs['Generated'],texture.inputs['Vector'])
        assert not quality.validar(collection)['errors']

    source = copy.deepcopy(data)
    moved = matrix.copy()
    moved[12] = 1
    source['animation_count'] = 1
    source['animation_bounds'] = [0,0,0,2,1,0]
    source['animations'] = [{'name':'Move','uid':'test','duration':1/30,'fps':30,
        'frames':[{'meshes':[m],'bones':[],'rigWorlds':[],'morphs':[]} for m in (matrix,moved)]}]
    path.write_text(json.dumps(source), encoding='utf-8')
    result = service.import_capture(path)
    validation = result['animation_validation']
    assert validation['warnings'] and validation['vertices_checked']==0
    assert validation['max_position_error'] is None and validation['clips_checked']==0
    assert result['animation']['frames']==2
    print('IMPORT_FALLBACKS_PASSED shared_and_unshared_UV, generated_coordinates, missing_validation_samples',flush=True)
