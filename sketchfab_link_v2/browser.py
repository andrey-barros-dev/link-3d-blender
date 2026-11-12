"""Navegador próprio por CDP, usando somente a biblioteca padrão do Python.

Não conecta ao Liberty, ao MCP nem ao perfil pessoal do navegador.
Fallback portátil oficial: https://googlechromelabs.github.io/chrome-for-testing/
"""
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from zipfile import ZipFile

from . import bridge

_browser_lock = threading.Lock()


def model_uid(url):
    parsed = urlparse(url.strip())
    if parsed.scheme != 'https' or parsed.hostname not in ('sketchfab.com', 'www.sketchfab.com'):
        raise ValueError('Cole um link de modelo do Sketchfab')
    match = re.search(r'([a-f0-9]{32})(?:/embed)?/?$', parsed.path)
    if not match:
        raise ValueError('Link do modelo inválido')
    return match[1]


def browser_platform(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == 'Windows':
        return 'win64' if machine in ('amd64','x86_64','arm64','aarch64') else 'win32'
    if system == 'Darwin':
        return 'mac-arm64' if machine in ('arm64','aarch64') else 'mac-x64'
    if system == 'Linux':
        return 'linux-arm64' if machine in ('arm64','aarch64') else 'linux64'
    raise RuntimeError('Sistema operacional sem navegador compatível')


def find_browser():
    paths=[]
    if os.name == 'nt':
        for env in ('PROGRAMFILES','PROGRAMFILES(X86)','LOCALAPPDATA'):
            folder=os.environ.get(env)
            if folder:
                paths += [Path(folder)/p for p in ('Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe')]
    elif platform.system() == 'Darwin':
        paths += [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'), Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge')]
    for name in ('google-chrome','google-chrome-stable','chromium','chromium-browser','microsoft-edge'):
        executable=shutil.which(name)
        if executable:
            paths.append(Path(executable))
    return next((p for p in paths if p.is_file()),None)


def prepare_browser(cache, progress):
    # Downloads simultâneos compartilham uma única preparação do navegador portátil.
    with _browser_lock:
        return _prepare_browser(cache, progress)


def _prepare_browser(cache, progress):
    executable=find_browser()
    if executable:
        return executable
    cache=Path(cache)
    marker=cache/'browser.json'
    if marker.is_file():
        executable=cache/json.loads(marker.read_text(encoding='utf-8'))['executable']
        if executable.is_file():
            return executable
    progress('Preparando navegador…')
    # Endpoint de releases mantido pelo próprio Chrome. Não depende de pip/Node.
    with urlopen('https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json',timeout=30) as response:
        metadata=json.load(response)
    target=browser_platform()
    asset=next((a for a in metadata['channels']['Stable']['downloads']['chrome'] if a['platform']==target),None)
    if not asset:
        raise RuntimeError('Navegador portátil indisponível para este sistema')
    if urlparse(asset['url']).hostname != 'storage.googleapis.com':
        raise RuntimeError('Origem inesperada do navegador')
    cache.mkdir(parents=True,exist_ok=True)
    archive=cache/'browser.zip.partial'
    with urlopen(asset['url'],timeout=120) as source, archive.open('wb') as output:
        shutil.copyfileobj(source,output,1024*1024)
    with ZipFile(archive) as zipped:
        for item in zipped.infolist():
            dest=(cache/item.filename).resolve()
            if not dest.is_relative_to(cache.resolve()):
                raise RuntimeError('Caminho inválido no navegador portátil')
        zipped.extractall(cache)
        if os.name != 'nt':
            for item in zipped.infolist():
                permissions=item.external_attr>>16
                if permissions:
                    (cache/item.filename).chmod(permissions & 0o777)
    archive.unlink()
    if target.startswith('win'):
        executable=cache/('chrome-'+target)/'chrome.exe'
    elif target.startswith('mac'):
        executable=cache/('chrome-'+target)/'Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
    else:
        executable=cache/('chrome-'+target)/'chrome'
    if not executable.is_file():
        raise RuntimeError('Navegador portátil incompleto')
    marker.write_text(json.dumps({'version':metadata['channels']['Stable']['version'],'executable':str(executable.relative_to(cache))}),encoding='utf-8')
    return executable


class CDP:
    """Cliente WebSocket RFC6455 mínimo com mensagens fragmentadas e ping/pong."""
    def __init__(self,url):
        parsed=urlparse(url)
        if parsed.scheme != 'ws' or parsed.hostname not in ('127.0.0.1','localhost'):
            raise ValueError('CDP deve ser local')
        self.sock=socket.create_connection((parsed.hostname,parsed.port),timeout=30)
        self.sock.settimeout(180)
        self.buffer=bytearray()
        self.counter=0
        self.on_event=None
        key=base64.b64encode(os.urandom(16)).decode()
        request=(f'GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(request.encode('ascii'))
        while b'\r\n\r\n' not in self.buffer:
            self.buffer.extend(self.sock.recv(4096))
            if len(self.buffer)>65536:
                raise RuntimeError('Resposta CDP inválida')
        headers,rest=self.buffer.split(b'\r\n\r\n',1)
        self.buffer=bytearray(rest)
        expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if b' 101 ' not in headers.split(b'\r\n')[0] or expected.lower() not in headers.lower():
            self.sock.close()
            raise RuntimeError('Não foi possível conectar ao navegador')

    def read(self,count):
        while len(self.buffer)<count:
            chunk=self.sock.recv(min(1024*1024,max(4096,count-len(self.buffer))))
            if not chunk:
                raise RuntimeError('O navegador encerrou a conexão')
            self.buffer.extend(chunk)
        output=bytes(self.buffer[:count])
        del self.buffer[:count]
        return output

    def send(self,payload,opcode=1):
        if isinstance(payload,str): payload=payload.encode('utf-8')
        size=len(payload)
        header=bytes([0x80|opcode])
        if size<126: header+=bytes([0x80|size])
        elif size<=65535: header+=bytes([0x80|126])+struct.pack('>H',size)
        else: header+=bytes([0x80|127])+struct.pack('>Q',size)
        mask=os.urandom(4)
        masked=bytes(v^mask[i%4] for i,v in enumerate(payload))
        self.sock.sendall(header+mask+masked)

    def receive(self):
        fragments=[]
        while True:
            first,second=self.read(2)
            size=second&127
            if size==126: size=struct.unpack('>H',self.read(2))[0]
            elif size==127: size=struct.unpack('>Q',self.read(8))[0]
            mask=self.read(4) if second&128 else None
            payload=self.read(size)
            if mask: payload=bytes(v^mask[i%4] for i,v in enumerate(payload))
            opcode=first&15
            if opcode==8: raise RuntimeError('Navegador fechou a captura')
            if opcode==9:
                self.send(payload,10)
                continue
            if opcode==10: continue
            fragments.append(payload)
            if first&128:
                return json.loads(b''.join(fragments))

    def call(self,method,**params):
        self.counter+=1
        request_id=self.counter
        self.send(json.dumps({'id':request_id,'method':method,'params':params}))
        while True:
            message=self.receive()
            if 'id' not in message and self.on_event:
                self.on_event(message)
            if message.get('id')!=request_id: continue
            if 'error' in message: raise RuntimeError(message['error'].get('message','Falha CDP'))
            return message.get('result',{})

    def evaluate(self,expression,await_promise=False):
        result=self.call('Runtime.evaluate',expression=expression,awaitPromise=await_promise,returnByValue=True,timeout=900000)
        if result.get('exceptionDetails'):
            details=result['exceptionDetails']
            raise RuntimeError(details.get('exception',{}).get('description',details.get('text','Falha no viewer')))
        return result.get('result',{}).get('value')

    def close(self):
        self.sock.close()


def capture(url,root,progress=lambda text:None):
    uid=model_uid(url)
    root=Path(root).resolve()
    executable=prepare_browser(root.parent/'navegador',progress)
    progress('Abrindo modelo…')
    assets=Path(__file__).parent/'viewer'
    static=(assets/'capture.js').read_text(encoding='utf-8')
    animate=(assets/'animation.js').read_text(encoding='utf-8')
    process=None
    client=None
    staging=None
    with tempfile.TemporaryDirectory(prefix='sketchfab-v2-browser-') as profile:
        try:
            command=[str(executable),'--headless=new','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1',
                     '--user-data-dir='+profile,'--no-first-run','--no-default-browser-check','--window-size=1280,900',
                     '--autoplay-policy=no-user-gesture-required','--disable-background-timer-throttling',
                     '--disable-renderer-backgrounding','--enable-webgl','--ignore-gpu-blocklist','about:blank']
            options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
            if os.name=='nt': options['creationflags']=subprocess.CREATE_NO_WINDOW
            process=subprocess.Popen(command,**options)
            port_file=Path(profile)/'DevToolsActivePort'
            deadline=time.monotonic()+30
            while not port_file.is_file():
                if process.poll() is not None: raise RuntimeError('Não foi possível iniciar o navegador; verifique a instalação e o suporte gráfico')
                if time.monotonic()>deadline: raise RuntimeError('O navegador demorou para iniciar')
                time.sleep(0.1)
            port=int(port_file.read_text().splitlines()[0])
            with urlopen(f'http://127.0.0.1:{port}/json/list',timeout=10) as response:
                pages=json.load(response)
            page=next(p for p in pages if p['type']=='page')
            client=CDP(page['webSocketDebuggerUrl'])
            client.call('Page.enable')
            client.call('Runtime.enable')
            client.call('Runtime.addBinding',name='linkv2_image_chunk')
            client.call('Page.navigate',url=f'https://sketchfab.com/models/{uid}/embed?autostart=1&ui_infos=0&ui_controls=0&ui_watermark=0')
            deadline=time.monotonic()+150
            while not client.evaluate("Boolean(window.webpackChunksketchfab && document.querySelector('canvas'))"):
                if time.monotonic()>deadline:
                    raise RuntimeError('O viewer não carregou. Confira o link e a conexão; o site pode pedir verificação no navegador.')
                time.sleep(0.5)
            progress('Lendo geometria e texturas…')
            staging=bridge.new_capture_folder(root,uid)

            def on_event(message):
                # Cada textura chega em blocos e vai direto para a pasta final da captura.
                if message.get('method')!='Runtime.bindingCalled':return
                params=message.get('params',{})
                if params.get('name')!='linkv2_image_chunk':return
                packet=json.loads(params['payload'])
                image_uid=packet.get('uid','')
                if not bridge.IMAGE_UID.fullmatch(image_uid):raise ValueError('UID de imagem inválido no stream')
                image_path=staging/(image_uid+'.png')
                if packet['op']=='start':
                    image_path.write_bytes(b'')
                elif packet['op']=='chunk':
                    with image_path.open('ab') as output:output.write(base64.b64decode(packet['data'],validate=True))

            client.on_event=on_event
            # O viewer carrega geometria/texturas depois do runtime webpack.
            expression='''(async () => {
              window.__link3d_imageSink=async (image,encoded)=>{
                linkv2_image_chunk(JSON.stringify({op:'start',uid:image.uid}));
                const chunk=1024*1024;
                for(let offset=0;offset<encoded.length;offset+=chunk){
                  linkv2_image_chunk(JSON.stringify({op:'chunk',uid:image.uid,data:encoded.slice(offset,offset+chunk)}));
                  await new Promise(resolve=>setTimeout(resolve,0));
                }
                linkv2_image_chunk(JSON.stringify({op:'end',uid:image.uid}));
              };
              const capture=STATIC;
              let summary;
              for(let i=0;i<120;i++) {
                try { summary=await capture(); if(!summary.meshes) throw Error('Cena ainda não carregada'); break; }
                catch(error) {
                  window.__link3d_lastError = error.message;
                  if(!/ainda não|não está acessível|undefined|não encontrado|before initialization/.test(error.message)||i===119) throw error;
                  await new Promise(resolve=>setTimeout(resolve,500));
                }
              }
              if(summary.animations) await (ANIMATE)();
              const data=window.__link3d_capture;
              data.requested_url=SOURCE;
              data.source='https://sketchfab.com/3d-models/' + data.uid;
              data.captured_at=new Date().toISOString();
              data.capture_version='2.5.3';
              const metadata={...data, images:data.images.map(({data,...image})=>({...image,encoded_length:data?.length||0}))};
              window.__link3d_metadata=JSON.stringify(metadata);
              return {uid:data.uid,metadata_length:window.__link3d_metadata.length};
            })()'''.replace('STATIC',static).replace('ANIMATE',animate).replace('SOURCE',json.dumps(url.strip()))
            summary=client.evaluate(expression,await_promise=True)
            progress('Salvando modelo…')
            # Metadados em blocos: o CDP não lida bem com um único resultado gigante.
            chunk_size=1024*1024
            data=json.loads(''.join(client.evaluate(f'window.__link3d_metadata.slice({offset},{offset+chunk_size})')
                                    for offset in range(0,summary['metadata_length'],chunk_size)))
            path=bridge.finish_capture(data,staging)
            staging=None
            # Guardar a referência visual real do navegador para revisão posterior.
            try:
                screenshot=client.call('Page.captureScreenshot',format='png',captureBeyondViewport=False)
                (path.parent/'referencia_viewer.png').write_bytes(base64.b64decode(screenshot['data']))
            except Exception:
                pass
            return path
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if client:
                try: client.call('Browser.close')
                except Exception: pass
                client.close()
            if process:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
