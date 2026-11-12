// Executar como função no evaluate_script do Liberty, com o viewer aberto.
// Implementação própria: lê a cena em memória e as texturas usadas pela GPU.
async () => {
  const frame = [...document.querySelectorAll('iframe')].find(f => f.src.includes('/embed'));
  const w = frame?.contentWindow || (location.pathname.includes('/embed') ? window : null);
  if (!w) throw new Error('Viewer Sketchfab não encontrado');
  let req;
  w.webpackChunksketchfab.push([['link3d_capture_' + Date.now()], {}, r => { req = r; }]);
  const candidates = Object.entries(req.m).filter(([, f]) => {
    const s = String(f);
    return s.includes('getScenePositionFromScreenPoint') && s.includes('getMaterialFromHit');
  });
  let context;
  for (const [id] of candidates) {
    // Módulos ainda em inicialização lançam ReferenceError (TDZ): tratar como "carregando".
    try {
      const exports = req(id);
      context = Object.values(exports).find(v => v && v._rootModel && v._features);
    } catch (error) { context = null; }
    if (context) break;
  }
  if (!context) throw new Error('A cena do viewer ainda não está acessível');
  w.__link3d_context = context;
  const model = context._context.getModel();
  const shading = model.get('options')?.shading || {};
  const images = [];
  const materialList = [];
  const meshes = [];
  const seenImages = new Map();
  const seenGeometries = new Set();
  const geometrySources = new Map();
  function sampleTexture(gl, source, width, height) {
    // Texturas compactadas e formatos de luminância não são anexáveis a FBO.
    // Amostrá-las para RGBA preserva a decodificação feita pela própria GPU.
    const ext = gl.createVertexArray ? null : gl.getExtension('OES_vertex_array_object');
    const old = {program:gl.getParameter(gl.CURRENT_PROGRAM), array:gl.getParameter(gl.ARRAY_BUFFER_BINDING),
      active:gl.getParameter(gl.ACTIVE_TEXTURE), viewport:gl.getParameter(gl.VIEWPORT), mask:gl.getParameter(gl.COLOR_WRITEMASK),
      vao:gl.createVertexArray ? gl.getParameter(gl.VERTEX_ARRAY_BINDING) : ext ? gl.getParameter(ext.VERTEX_ARRAY_BINDING_OES) : null};
    const caps = [gl.BLEND, gl.DEPTH_TEST, gl.CULL_FACE, gl.SCISSOR_TEST, gl.STENCIL_TEST];
    const enabled = caps.map(c=>gl.isEnabled(c));
    gl.activeTexture(gl.TEXTURE0);
    const oldTexture = gl.getParameter(gl.TEXTURE_BINDING_2D);
    const output = gl.createTexture(), buffer = gl.createBuffer(), program = gl.createProgram();
    const vao = gl.createVertexArray ? gl.createVertexArray() : ext?.createVertexArrayOES();
    const shaders = [];
    try {
      if (gl.bindVertexArray) gl.bindVertexArray(vao); else if (ext) ext.bindVertexArrayOES(vao);
      for (const [type, code] of [[gl.VERTEX_SHADER, 'attribute vec2 p; varying vec2 uv; void main(){uv=p*0.5+0.5;gl_Position=vec4(p,0.0,1.0);}'],
        [gl.FRAGMENT_SHADER, 'precision highp float; varying vec2 uv; uniform sampler2D tex; void main(){gl_FragColor=texture2D(tex,uv);}']]) {
        const shader = gl.createShader(type); shaders.push(shader);
        gl.shaderSource(shader, code); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(shader));
        gl.attachShader(program, shader);
      }
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(program));
      gl.bindTexture(gl.TEXTURE_2D, output);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, output, 0);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw Error('Falha no framebuffer RGBA');
      caps.forEach(c=>gl.disable(c)); gl.colorMask(true,true,true,true);
      gl.viewport(0,0,width,height); gl.useProgram(program);
      gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
      gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
      const location=gl.getAttribLocation(program,'p');
      gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location,2,gl.FLOAT,false,0,0);
      gl.bindTexture(gl.TEXTURE_2D,source); gl.uniform1i(gl.getUniformLocation(program,'tex'),0);
      gl.drawArrays(gl.TRIANGLES,0,3);
      const pixels=new Uint8Array(width*height*4);
      gl.readPixels(0,0,width,height,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
      return pixels;
    } finally {
      gl.useProgram(old.program); gl.bindBuffer(gl.ARRAY_BUFFER,old.array);
      if(gl.bindVertexArray)gl.bindVertexArray(old.vao);else if(ext)ext.bindVertexArrayOES(old.vao);
      caps.forEach((c,i)=>enabled[i]?gl.enable(c):gl.disable(c));
      gl.viewport(...old.viewport); gl.colorMask(...old.mask);
      gl.bindTexture(gl.TEXTURE_2D,oldTexture); gl.activeTexture(old.active);
      gl.deleteTexture(output);gl.deleteBuffer(buffer);gl.deleteProgram(program);shaders.forEach(s=>gl.deleteShader(s));
      if(gl.deleteVertexArray)gl.deleteVertexArray(vao);else if(ext)ext.deleteVertexArrayOES(vao);
    }
  }
  async function readTexture(model) {
    const texture = model.get('multiresTexture');
    const sourceUid = model.get('uid');
    const uid = sourceUid + '_' + texture._internalFormat;
    if (seenImages.has(uid)) return seenImages.get(uid);
    const maximum = texture.getImageJSONMaxSupported?.();
    if (texture.setImageMaxSupported) {
      const entry = texture.setImageMaxSupported();
      if (entry?.promise) {
        let timeout;
        try {
          await Promise.race([entry.promise, new Promise((_, reject) => { timeout = setTimeout(() => reject(Error('Tempo esgotado ao carregar textura completa: ' + uid)), 90000); })]);
        } finally { clearTimeout(timeout); }
      }
    }
    if (texture && texture.getImage?.()?.isReady()) {
      // Canais fora da vista podem ter a imagem carregada e o upload adiado.
      texture.setForceUpload(true);
      try { context._viewerOSGJS.getState().applyTextureAttribute(0, texture); }
      finally { texture.setForceUpload(false); }
    }
    if (!texture?._textureObject) throw new Error('Textura ainda não carregada: ' + uid);
    const gl = texture._gl;
    const width = texture._textureWidth, height = texture._textureHeight;
    if (maximum && (width < maximum.width || height < maximum.height)) throw Error('Textura ainda está na prévia de baixa resolução: ' + uid);
    const previousDraw = gl.getParameter(gl.DRAW_FRAMEBUFFER_BINDING);
    const previousRead = gl.getParameter(gl.READ_FRAMEBUFFER_BINDING);
    const fb = gl.createFramebuffer();
    let pixels = new Uint8Array(width * height * 4);
    try {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture._textureObject.id(), 0);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) pixels = sampleTexture(gl, texture._textureObject.id(), width, height);
      else gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    } finally {
      gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, previousDraw);
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, previousRead);
      gl.deleteFramebuffer(fb);
    }
    const upright = new Uint8ClampedArray(pixels.length);
    const row = width * 4;
    for (let y = 0; y < height; y++) upright.set(pixels.subarray(y * row, (y + 1) * row), (height - y - 1) * row);
    const canvas = w.document.createElement('canvas');
    canvas.width = width; canvas.height = height;
    canvas.getContext('2d').putImageData(new w.ImageData(upright, width, height), 0, 0);
    const index = images.length;
    const item={uid,source_uid:sourceUid,format:texture._internalFormat,width,height,expected_width:maximum?.width,expected_height:maximum?.height};
    const encoded=canvas.toDataURL('image/png').split(',')[1];
    if(typeof w.__link3d_imageSink==='function'){
      await w.__link3d_imageSink(item,encoded);
      item.streamed=true;
    }else item.data=encoded;
    images.push(item);
    seenImages.set(uid, index);
    return index;
  }
  for (const mat of Object.values(context._features.material.getModel()._materialByStateSetID)) {
    if (!mat.get('geometryList').length) continue;
    const hasUV = mat.get('geometryList').some(g => Object.keys(g.getVertexAttributeList()).some(k => /^TexCoord[0-9]+$/.test(k)));
    const channels = mat.get('channels');
    const baseOrder = shading.renderer === 'classic' ? ['DiffuseColor', 'AlbedoPBR', 'DiffusePBR'] : ['AlbedoPBR', 'DiffusePBR', 'DiffuseColor'];
    const baseName = baseOrder.find(k=>channels[k]?.get('enable')) || 'AlbedoPBR';
    const base = channels[baseName];
    const rough = channels.RoughnessPBR, gloss = channels.GlossinessPBR;
    const metallic = channels.MetalnessPBR;
    const texture = hasUV ? base.get('textureModel') : null;
    const index = materialList.length;
    const extraChannels = {};
    for (const key of ['Opacity', 'AlphaMask', 'NormalMap', 'BumpMap', 'RoughnessPBR', 'GlossinessPBR', 'MetalnessPBR', 'SpecularF0', 'SpecularPBR', 'AOPBR', 'CavityPBR', 'ClearCoat', 'ClearCoatRoughness', 'ClearCoatNormalMap', 'SubsurfaceTranslucency', 'EmitColor', 'Sheen', 'SheenRoughness', 'Displacement', 'Anisotropy', 'SubsurfaceScattering']) {
      const c = channels[key];
      if (!c?.get('enable')) continue;
      const tm = hasUV ? c.get('textureModel') : null;
      extraChannels[key] = {
        factor: c.get('factor') ?? 1, type: c.get('type'), invert: c.get('invert'),
        flipY: c.get('flipY'), color: c.get('color') ? Array.from(c.get('color')) : null,
        colorFactor: c.get('colorFactor') ? Array.from(c.get('colorFactor')) : null, tint: c.get('tint') ? Array.from(c.get('tint')) : null,
        image: tm ? await readTexture(tm) : null,
        component: tm && ['Opacity', 'AlphaMask'].includes(key) && [6406, 6408].includes(tm.get('multiresTexture')._internalFormat) ? 'alpha' : 'red',
        texCoord: tm?.get('texCoordUnit') || 0, transform: c.get('UVTransforms') || null,
        wrapS:tm?.get('wrapS'),wrapT:tm?.get('wrapT'),magFilter:tm?.get('magFilter'),minFilter:tm?.get('minFilter'),
        ior: c.get('ior'), roughnessFactor: c.get('roughnessFactor'), useAlbedoTint: c.get('useAlbedoTint'),
        thinLayer:c.get('thinLayer'),useMicrosurfaceTexture:c.get('useMicrosurfaceTexture'),useNormalOffset:c.get('useNormalOffset'),
        source_properties:Object.fromEntries(Object.entries(c.attributes).filter(([k,v])=>v===null||['number','string','boolean'].includes(typeof v))),
      };
    }
    materialList.push({
      name: mat.get('name'), shadeless: mat.get('shadeless') || shading.type === 'shadeless', color: Array.from(base.get('color') || [1, 1, 1]),
      factor: base.get('factor') ?? 1,
      base_channel:baseName,workflow:shading.renderer==='classic'?'classic':baseName==='DiffusePBR'?'specular_glossiness':'metallic_roughness',
      source_channels:Object.entries(channels).filter(([k,c])=>c?.get('enable')).map(([k])=>k),
      roughness: rough.get('enable') ? rough.get('factor') : 1 - (gloss.get('enable') ? gloss.get('factor') : 0),
      metallic: metallic.get('enable') ? metallic.get('factor') : 0,
      image: texture ? await readTexture(texture) : null,
      channels: extraChannels,
      textureInfo: {texCoord: texture?.get('texCoordUnit') || 0, transform: base.get('UVTransforms') || null,wrapS:texture?.get('wrapS'),wrapT:texture?.get('wrapT'),magFilter:texture?.get('magFilter'),minFilter:texture?.get('minFilter')},
      cullFace: mat.get('cullFace'), vertexColor: shading.vertexColor?.enable !== false && mat.get('vertexColorEnable'), vertexColorSpace: mat.get('vertexColorColorSpace'),
    });
    for (const geometry of mat.get('geometryList')) {
      const geometryKey = index + ':' + geometry.getInstanceID();
      if (seenGeometries.has(geometryKey)) continue;
      seenGeometries.add(geometryKey);
      const attrs = geometry.getVertexAttributeList();
      const bakeDeformation = !model.get('animationCount') && typeof geometry.computeTransformedVertices === 'function';
      if (bakeDeformation) geometry._rigTransformImplementation?.update(geometry);
      const positions = Array.from(bakeDeformation ? geometry.computeTransformedVertices() : attrs.Vertex.getElements());
      const normals = !bakeDeformation && attrs.Normal ? Array.from(attrs.Normal.getElements()) : null;
      const uv = attrs.TexCoord0 ? Array.from(attrs.TexCoord0.getElements()) : null;
      const uvLayers = Object.fromEntries(Object.entries(attrs).filter(([k]) => /^TexCoord[0-9]+$/.test(k)).map(([k,a])=>[k.slice(8), Array.from(a.getElements())]));
      const colors = attrs.Color ? Array.from(attrs.Color.getElements(), v => attrs.Color.getElements().BYTES_PER_ELEMENT === 1 ? v / 255 : v) : null;
      const triangles = [];
      const points = [];
      for (const primitive of geometry.getPrimitiveSetList()) {
        const raw = primitive.getIndices?.()?.getElements();
        const count = primitive.getCount();
        const offset = raw ? (primitive.offset || 0) / raw.BYTES_PER_ELEMENT : primitive.getFirst?.() || 0;
        const indices = raw ? Array.from(raw.subarray(offset, offset + count)) : Array.from({length: count}, (_, i) => offset + i);
        const mode = primitive.getMode();
        function tri(a, b, c) { if (a !== b && b !== c && a !== c) triangles.push([a, b, c]); }
        if (mode === 0) for (const vertex of indices) points.push(vertex);
        else if (mode === 4) for (let i = 0; i + 2 < indices.length; i += 3) tri(indices[i], indices[i + 1], indices[i + 2]);
        else if (mode === 5) for (let i = 2; i < indices.length; i++) {
          if (i % 2) tri(indices[i - 1], indices[i - 2], indices[i]);
          else tri(indices[i - 2], indices[i - 1], indices[i]);
        }
        else if (mode === 6) for (let i = 2; i < indices.length; i++) tri(indices[0], indices[i - 1], indices[i]);
        else throw new Error('Primitiva não suportada: ' + mode);
      }
      const camera = context._viewerOSGJS.getCamera();
      const viewport = camera.getViewport();
      const pointRadius = (shading.pointSize || 1) / (camera.getProjectionMatrix()[5] * viewport.height());
      for (const [instanceIndex, matrix] of geometry.getWorldMatrices().entries()) {
        const source = geometrySources.get(geometryKey);
        const mesh = {
        name: geometry.getName(), positions, normals, uv, triangles, points,
        point_radius: pointRadius, point_count: points.length,
        material: index, matrix: Array.from(matrix), uvLayers, colors, colorSize: attrs.Color?.getItemSize(),
        geometryId: geometry.getInstanceID(), instanceIndex,
        deformation_baked: bakeDeformation,
        };
        if (points.length && source !== undefined) {
          mesh.geometry_ref = source;
          for (const key of ['positions','normals','uv','uvLayers','triangles','points','colors','colorSize']) delete mesh[key];
        } else geometrySources.set(geometryKey, meshes.length);
        meshes.push(mesh);
      }
    }
  }
  const user = model.get('user') || {};
  const license = model.get('license') || {};
  const data = {version: 1, download_type: model.get('downloadType'), source: 'https://sketchfab.com/3d-models/' + model.get('uid'), uid: model.get('uid'), title: model.get('name'), author: user.displayName || user.username || '', license: license.label || '', license_url: license.url || '', materials: materialList, images, meshes};
  data.animation_count = model.get('animationCount') || 0;
  data.viewer = {engine:'OSG.js',shading,options_version:model.get('options')?.version,environment:model.get('options')?.environment};
  const camera = context._viewerOSGJS.getCamera();
  data.camera = {view: Array.from(camera.getViewMatrix()), projection: Array.from(camera.getProjectionMatrix())};
  // Uniforms reais do shader num único quadro: ambiente (transform, SH) e luzes do viewer,
  // todos no espaço de câmera da mesma view.
  const probe = (() => {
    const result = {transform: null, view: null, sph: null, lights: {}, shadows: [], lens_radius: null};
    try {
      const viewer = context._viewerOSGJS;
      const gl = viewer.getGraphicContext?.() || viewer.getState().getGraphicContext?.();
      const missingUV = new Map();
      for (const mat of Object.values(context._features.material.getModel()._materialByStateSetID)) {
        for (const geometry of mat.get('geometryList')) {
          const attrs = geometry.getVertexAttributeList();
          if (Object.keys(attrs).some(k => /^TexCoord/.test(k))) continue;
          const attribute = attrs.Vertex?.getBufferArray?.() || attrs.Vertex;
          const targets = meshes.filter(m => m.geometryId === geometry.getInstanceID());
          if (attribute?._buffer && targets.length) missingUV.set(attribute._buffer, targets);
        }
      }
      const hooks = ['drawElements', 'drawArrays'].map(name => {
        const original = gl[name];
        gl[name] = function (...args) {
          const program = gl.getParameter(gl.CURRENT_PROGRAM);
          if (program && missingUV.size) {
            const vertex = gl.getAttribLocation(program, 'Vertex');
            const targets = vertex < 0 ? null : missingUV.get(gl.getVertexAttrib(vertex, gl.VERTEX_ATTRIB_ARRAY_BUFFER_BINDING));
            if (targets) for (let i = 0; i < gl.getProgramParameter(program, gl.ACTIVE_ATTRIBUTES); i++) {
              const info = gl.getActiveAttrib(program, i);
              const unit = info.name.match(/^TexCoord(\d+)$/)?.[1];
              if (unit === undefined) continue;
              const location = gl.getAttribLocation(program, info.name);
              if (gl.getVertexAttrib(location, gl.VERTEX_ATTRIB_ARRAY_ENABLED)) continue;
              const value = gl.getVertexAttrib(location, gl.CURRENT_VERTEX_ATTRIB);
              const quantized = gl.getUniformLocation(program, 'uQUV' + unit);
              const decode = quantized ? gl.getUniform(program, quantized) : [0, 0, 1, 1];
              const uv = [value[0]*decode[2]+decode[0], value[1]*decode[3]+decode[1]];
              if (uv.every(Number.isFinite)) for (const mesh of targets) (mesh.constantUV ||= {})[unit] = uv;
            }
          }
          const lens = program && !result.lens_radius && gl.getUniformLocation(program, 'uLensRadius');
          if (lens) result.lens_radius = Array.from(gl.getUniform(program, lens));
          if (program && !result.transform && gl.getUniformLocation(program, 'uEnvironmentTransform')) {
            result.view = Array.from(viewer.getCamera().getViewMatrix());
            result.transform = Array.from(gl.getUniform(program, gl.getUniformLocation(program, 'uEnvironmentTransform')));
            if (gl.getUniformLocation(program, 'uDiffuseSPH[0]')) {
              result.sph = [];
              for (let i = 0; i < 9; i++) result.sph.push(...Array.from(gl.getUniform(program, gl.getUniformLocation(program, 'uDiffuseSPH[' + i + ']'))));
            }
            for (let i = 0; i < gl.getProgramParameter(program, gl.ACTIVE_UNIFORMS); i++) {
              const info = gl.getActiveUniform(program, i);
              const light = info.name.match(/^uSketchfabLight(\d+)_(\w+)$/);
              const shadow = info.name.match(/^uShadow_Texture(\d+)_viewLook$/);
              if (shadow) result.shadows.push(Number(shadow[1]));
              if (!light || info.size > 1) continue;
              const value = gl.getUniform(program, gl.getUniformLocation(program, info.name));
              (result.lights[light[1]] ||= {})[light[2]] = ArrayBuffer.isView(value) ? Array.from(value) : value;
            }
          }
          return original.apply(this, args);
        };
        return () => { gl[name] = original; };
      });
      try { context._viewer.getScreenShot(64, 64); } finally { hooks.forEach(restore => restore()); }
    } catch (error) { result.error = String(error?.message || error); }
    return result;
  })();
  data.lights = {view: probe.view, shadows: [...new Set(probe.shadows)], items: probe.lights,
    options: (() => { try { return JSON.parse(JSON.stringify(context._features.lighting?._model?.get('lights') || null)); } catch (error) { return null; } })()};
  // Fundo do viewer: 'fixed' é imagem 2D presa à tela; 'color' é cor sólida; 'ambient'/'environment' vêm do ambiente.
  data.background = (() => {
    try {
      const model = context._features.background?._model;
      if (!model) return null;
      const uid = model.get('uid');
      const entry = model.backgroundCollection?.models?.find(m => m.get?.('uid') === uid);
      const images = (entry?.attributes?.images || []).filter(i => i.url);
      const best = images.sort((a, b) => b.width * b.height - a.width * a.height)[0];
      const color = model.get('color');
      return {mode: model.get('enable') || null, uid, name: entry?.attributes?.name || '',
        color: color ? [color[0], color[1], color[2]].map(Number) : null,
        image: best ? {url: best.url, width: best.width, height: best.height} : null};
    } catch (error) { return {error: String(error?.message || error)}; }
  })();
  // Pós-processamento do viewer (opções do modelo) + raio real da vinheta usado no shader.
  data.postprocess = (() => {
    try {
      const attrs = context._features.postProcess?.getModel()?.attributes || {};
      const keys = Object.keys(attrs).filter(k => /^(enable|toneMapping|bloom|sharpen|vignette|grain|chromaticAberration|colorBalance|dof|ssao|ssr)/.test(k));
      const out = Object.fromEntries(keys.map(k => [k, (v => v && typeof v === 'object' ? Object.values(v).map(Number) : v)(attrs[k])]));
      out.lens_radius = probe.lens_radius;
      return out;
    } catch (error) { return {error: String(error?.message || error)}; }
  })();
  // Ambiente do viewer: metadados + uEnvironmentTransform lido no mesmo quadro da view.
  data.environment = (() => {
    try {
      const envModel = context._features.environment?._model;
      if (!envModel || !envModel.get('enable')) return null;
      const uid = envModel.get('uid');
      const collection = envModel.environmentCollection;
      const entry = collection?.get?.(uid) || collection?.models?.find(m => m.get?.('uid') === uid);
      const attrs = entry?.attributes || {};
      const background = (attrs.textures || []).find(t => t.type === 'background' && t.format === 'cubemap' && t.encoding === 'luv');
      const pick = background ? [...background.images].sort((a, b) => ((a.blur || 0) - (b.blur || 0)) || (b.width - a.width))[0] : null;
      return {uid, name: attrs.name || '', exposure: envModel.get('exposure'), background_exposure: envModel.get('backgroundExposure'),
        rotation: envModel.get('rotation'), blur: envModel.get('blur'), light_intensity: envModel.get('lightIntensity'),
        background_mode: context._features.background?._model?.get('enable') || null,
        background_color: (c => c ? [c[0], c[1], c[2]].map(Number) : null)(context._features.background?._model?.get('color')),
        transform: probe.transform, view: probe.view, sph: probe.sph, probe_error: probe.error,
        cubemap: pick ? {url: pick.file, size: pick.width, blur: pick.blur || 0, encoding: 'luv', bytes: pick.sizeUncompressed} : null};
    } catch (error) { return {error: String(error?.message || error)}; }
  })();
  w.__link3d_capture = data;
  // Retorne apenas o resumo: o conteúdo completo deve ser enviado em uma
  // transferência local, pois pode exceder o limite de resposta do MCP.
  return {meshes: meshes.length, triangles: meshes.reduce((sum, m) => sum + (m.triangles?.length || 0), 0), points:meshes.reduce((sum,m)=>sum+(m.point_count||0),0),textures: images.length, animations: data.animation_count};
}
