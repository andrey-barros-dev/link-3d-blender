"""Testes reais de transporte, validação e persistência sem depender do bpy."""
import base64
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('linkv2_bridge', root / 'sketchfab_link_v2' / 'bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def capture():
    return {'version': 1, 'uid': 'a'*32, 'source': 'https://sketchfab.com/3d-models/test-'+'a'*32,
            'title': 'Teste', 'author': '', 'license': '', 'images': [],
            'materials': [{'color':[1,1,1], 'factor':1, 'roughness':0.5, 'metallic':0, 'image':None}],
            'meshes': [{'positions':[0,0,0,1,0,0,0,1,0], 'normals':None, 'uv':None,
                        'triangles':[[0,1,2]], 'material':0,
                        'matrix':[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]}]}


class TransferTests(unittest.TestCase):
    def test_bad_geometry_rejected_before_writing(self):
        for invalid in ('index', 'nan', 'uv', 'animated'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                data = capture()
                if invalid == 'index': data['meshes'][0]['triangles'][0][1] = 3
                if invalid == 'nan': data['meshes'][0]['positions'][0] = float('nan')
                if invalid == 'uv': data['meshes'][0]['uv'] = [0, 0]
                if invalid == 'animated': data['animation_count'] = 1
                with self.assertRaises(ValueError): bridge.save_capture(data, directory)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_texture_path_cannot_escape(self):
        data = capture()
        data['images'] = [{'uid':'../../escape', 'data':''}]
        with self.assertRaises(ValueError): bridge.validate(data, embedded=True)

    def test_receiver_origin_queue_and_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = bridge.Receiver(directory)
            endpoint = receiver.start()
            try:
                payload = json.dumps(capture()).encode()
                denied = Request(endpoint, data=payload, headers={'Origin':'https://example.com'})
                with self.assertRaises(HTTPError) as error: urlopen(denied, timeout=5)
                self.assertEqual(error.exception.code, 403)
                self.assertTrue(receiver.pending.empty())
                paths=[]
                for _ in range(2):
                    request = Request(endpoint, data=payload, headers={'Origin':bridge.ORIGIN,'Content-Type':'application/json'})
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 202)
                        self.assertEqual(response.headers['Access-Control-Allow-Origin'], bridge.ORIGIN)
                        result=json.load(response)
                    path=receiver.pending.get_nowait()
                    self.assertEqual(path, Path(result['path']))
                    self.assertTrue(path.is_file())
                    paths.append(path)
                self.assertNotEqual(paths[0], paths[1])
                self.assertEqual(json.loads(paths[0].read_text(encoding='utf-8')), capture())
            finally:
                receiver.stop()


if __name__ == '__main__': unittest.main(verbosity=2)
