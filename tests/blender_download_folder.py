"""Verifica troca de pasta entre downloads sem rede e sem a cena do usuário."""
import json
import queue
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from sketchfab_link_v2 import service, ui

ui.register()
try:
    prop = ui.LINKV2_Settings.bl_rna.properties['data_root']
    assert prop.subtype == 'DIR_PATH' and not prop.is_hidden
    with tempfile.TemporaryDirectory(prefix='linkv2-folder-') as temp:
        base = Path(temp)
        service.receiver = SimpleNamespace(root=base/'antiga', pending=queue.Queue())
        uid = 'a'*32
        destinations = []

        def capture(url, destination, progress):
            path = Path(destination)/uid/'teste'/'modelo.json'
            path.parent.mkdir(parents=True)
            path.write_text('{}', encoding='utf-8')
            destinations.append(Path(destination))
            return path

        with patch.object(service, 'start'), patch.object(service.browser, 'capture', capture):
            for name in ('Modelos A', 'Modelos B'):
                folder = base/name
                bpy.context.scene.linkv2.source_url = 'https://sketchfab.com/3d-models/'+uid
                bpy.context.scene.linkv2.data_root = str(folder)
                assert bpy.ops.linkv2.download() == {'FINISHED'}
                for worker in service.workers:
                    worker.join(timeout=10)
                    assert not worker.is_alive()
                item = service.receiver.pending.get_nowait()
                assert item[0].is_file() and folder in item[0].parents
                assert item[1] == bpy.context.scene
        assert destinations == [base/'Modelos A', base/'Modelos B']
        assert not (base/'antiga').exists()
        assert not service.busy()
        print('DOWNLOAD_FOLDER_PASSED '+json.dumps([str(p) for p in destinations]), flush=True)
finally:
    service.receiver = None
    ui.unregister()
