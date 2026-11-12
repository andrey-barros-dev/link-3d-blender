"""Recepção local: somente arquivos e fila, sem acesso a bpy em threads."""
import base64
import copy
import json
import math
import queue
import re
import secrets
import shutil
import struct
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

UID = re.compile(r'^[a-f0-9]{32}$')
IMAGE_UID = re.compile(r'^[a-f0-9]{32}(?:_[0-9]+)?$')
ORIGIN = 'https://sketchfab.com'
STALE_PARTIAL_SECONDS = 6 * 3600


def resolve_meshes(data):
    output = []
    for index, mesh in enumerate(data['meshes']):
        reference = mesh.get('geometry_ref')
        if reference is not None:
            if type(reference) is not int or not 0 <= reference < index:
                raise ValueError('Referência de geometria inválida')
            mesh = dict(output[reference], **mesh)
        output.append(mesh)
    return output


def numbers(values, size=None, multiple=None):
    if not isinstance(values, list) or (size is not None and len(values) != size):
        raise ValueError('Vetor de tamanho inválido')
    if multiple and len(values) % multiple:
        raise ValueError('Vetor incompleto')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
        raise ValueError('Número inválido na captura')


def validate(data, embedded=False):
    if not isinstance(data, dict) or data.get('version') != 1 or not UID.fullmatch(data.get('uid', '')):
        raise ValueError('Formato ou UID inválido')
    parsed = urlparse(data.get('source', ''))
    if parsed.scheme != 'https' or parsed.hostname != 'sketchfab.com' or not parsed.path.rstrip('/').endswith(data['uid']):
        raise ValueError('Origem da captura inconsistente')
    if not isinstance(data.get('title'), str) or not data['title'].strip():
        raise ValueError('Título ausente')
    for key in ('images', 'materials', 'meshes'):
        if not isinstance(data.get(key), list):
            raise ValueError('Lista ausente: ' + key)
    if not data['meshes'] or not data['materials']:
        raise ValueError('Captura sem malhas ou materiais')
    image_ids = set()
    for image in data['images']:
        uid = image.get('uid', '')
        if not IMAGE_UID.fullmatch(uid) or uid in image_ids:
            raise ValueError('Identificador de imagem inválido ou duplicado')
        image_ids.add(uid)
        if any(type(image.get(k)) is not int or image[k] <= 0 for k in ('width','height')):
            raise ValueError('Dimensões da textura inválidas')
        if embedded:
            raw = base64.b64decode(image.get('data', ''), validate=True)
            if len(raw) < 24 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR':
                raise ValueError('Textura não é PNG')
            width, height = struct.unpack('>II', raw[16:24])
            if (width, height) != (image.get('width'), image.get('height')):
                raise ValueError('Dimensões da textura inconsistentes')
    def image_ref(value):
        if value is not None and (type(value) is not int or not 0 <= value < len(data['images'])):
            raise ValueError('Referência de textura inválida')
    for material in data['materials']:
        image_ref(material.get('image'))
        numbers(material.get('color'), size=3)
        for key in ('factor', 'roughness', 'metallic'):
            numbers([material.get(key)])
        for channel in material.get('channels', {}).values():
            image_ref(channel.get('image'))
            numbers([channel.get('factor', 1)])
    for mesh in resolve_meshes(data):
        positions = mesh.get('positions')
        numbers(positions, multiple=3)
        count = len(positions) // 3
        if not count:
            raise ValueError('Malha sem vértices')
        numbers(mesh.get('matrix'), size=16)
        if type(mesh.get('material')) is not int or not 0 <= mesh['material'] < len(data['materials']):
            raise ValueError('Material da malha inválido')
        faces = mesh.get('triangles')
        points = mesh.get('points', [])
        if not isinstance(faces, list) or (not faces and not points):
            raise ValueError('Malha sem triângulos')
        if not isinstance(points, list) or any(type(i) is not int or not 0 <= i < count for i in points):
            raise ValueError('Índice de ponto inválido')
        for face in faces:
            if not isinstance(face, list) or len(face) != 3 or any(type(i) is not int or not 0 <= i < count for i in face):
                raise ValueError('Índice de triângulo inválido')
        for uv in (mesh.get('uvLayers') or {'0': mesh.get('uv')}).values():
            if uv is not None:
                numbers(uv, size=count * 2)
        for uv in mesh.get('constantUV', {}).values():
            numbers(uv, size=2)
        if mesh.get('normals') is not None:
            numbers(mesh['normals'], size=count * 3)
        if mesh.get('colors'):
            numbers(mesh['colors'], size=count * mesh.get('colorSize', 4))
    if data.get('animation_count', 0) and not data.get('animations'):
        raise ValueError('Modelo animado sem captura de animação')
    # A reconstrução e a amostragem numérica completas ocorrem na cena temporária.
    for clip in data.get('animations', []):
        if not clip.get('frames') or not 0 < clip.get('fps', 0) <= 240:
            raise ValueError('Clipe de animação incompleto')
    return data


def save_capture(data, root):
    validate(data, embedded=True)
    folder = Path(root).resolve() / data['uid'] / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    staging = folder.with_name(folder.name + '.partial')
    staging.mkdir(parents=True)
    try:
        data = copy.deepcopy(data)
        for image in data['images']:
            (staging / (image['uid'] + '.png')).write_bytes(base64.b64decode(image.pop('data'), validate=True))
        (staging / 'modelo.json').write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        staging.rename(folder)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return folder / 'modelo.json'


def new_capture_folder(root, uid):
    """Pasta final da captura, criada como .partial e publicada só ao terminar."""
    if not UID.fullmatch(uid):
        raise ValueError('UID inválido')
    root = Path(root).resolve()
    cleanup_partials(root)
    folder = root / uid / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    staging = folder.with_name(folder.name + '.partial')
    staging.mkdir(parents=True)
    return staging


def finish_capture(data, staging):
    """Confere que cada PNG chegou inteiro e publica a pasta atomicamente.
    A validação completa do JSON acontece uma única vez, na importação."""
    staging = Path(staging)
    try:
        if not UID.fullmatch(data.get('uid', '')) or staging.parent.name != data['uid']:
            raise ValueError('O viewer retornou outro modelo')
        for image in data['images']:
            if not IMAGE_UID.fullmatch(image.get('uid', '')):
                raise ValueError('Identificador de imagem inválido')
            path = staging / (image['uid'] + '.png')
            if not path.is_file():
                raise ValueError('Textura não recebida: ' + image['uid'])
            with path.open('rb') as source:
                header = source.read(24)
            if len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
                raise ValueError('PNG incompleto na transferência')
            if struct.unpack('>II', header[16:24]) != (image['width'], image['height']):
                raise ValueError('Dimensões da textura inconsistentes')
            if image['width'] < image.get('expected_width', 0) or image['height'] < image.get('expected_height', 0):
                raise ValueError('Textura na resolução de prévia')
            image.pop('encoded_length', None)
            image.pop('streamed', None)
        (staging / 'modelo.json').write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        folder = staging.with_name(staging.name[:-len('.partial')])
        staging.rename(folder)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return folder / 'modelo.json'


def cleanup_partials(root):
    """Remove capturas interrompidas antigas; as recentes podem ser downloads em andamento."""
    import time
    limit = time.time() - STALE_PARTIAL_SECONDS
    for staging in Path(root).glob('*/*.partial'):
        try:
            if staging.is_dir() and staging.stat().st_mtime < limit:
                shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            pass


class Receiver:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.pending = queue.Queue()
        self.token = secrets.token_urlsafe(32)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, payload=None):
                raw = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Access-Control-Allow-Origin', ORIGIN)
                self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.send_header('Access-Control-Allow-Private-Network', 'true')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def allowed(self):
                if self.path != '/' + receiver.token:
                    self.reply(404)
                    return False
                if self.headers.get('Origin') not in (None, ORIGIN):
                    self.reply(403, {'error': 'Origem não permitida'})
                    return False
                return True

            def do_OPTIONS(self):
                if self.allowed():
                    self.reply(200)

            def do_POST(self):
                if not self.allowed():
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if length <= 0:
                        self.reply(411, {'error': 'Captura vazia'})
                        return
                    self.connection.settimeout(120)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError('Transferência interrompida')
                    data = json.loads(raw)
                    path = save_capture(data, receiver.root)
                    receiver.pending.put_nowait(path)
                    self.reply(202, {'uid': data['uid'], 'title': data['title'], 'path': str(path), 'status': 'Recebido; importação na fila'})
                except (ValueError, TypeError, KeyError) as exc:
                    self.reply(400, {'error': str(exc)})
                except Exception as exc:
                    self.reply(500, {'error': str(exc)})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='SketchfabLinkV2', daemon=True)

    @property
    def url(self):
        return 'http://127.0.0.1:' + str(self.server.server_port) + '/' + self.token

    def start(self):
        self.thread.start()
        return self.url

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
