// Diagnóstico próprio: relaciona a cena em memória com chamadas reais da GPU.
// Não altera shaders nem extrai URLs com credenciais/query strings.
async (expectedUid) => {
  const frame=[...document.querySelectorAll('iframe')].find(f=>f.src.includes('/embed'));
  const w=frame?.contentWindow || window;
  const context=w.__link3d_context;
  if(!context)throw Error('Execute a inspeção do viewer antes do diagnóstico');
  const model=context._context.getModel();
  if(expectedUid && model.get('uid')!==expectedUid)throw Error('Modelo diferente do esperado');
  const viewer=context._viewerOSGJS;
  const gl=viewer.getGraphicContext?.() || viewer.getState().getGraphicContext?.();
  if(!gl)throw Error('Contexto WebGL não encontrado');
  const materials=Object.values(context._features.material.getModel()._materialByStateSetID).filter(m=>m.get('geometryList').length);
  const geometries=[...new Set(materials.flatMap(m=>m.get('geometryList')))];
  const buffers=new Map();
  for(const geometry of geometries){
    let attribute=geometry.getVertexAttributeList().Vertex;
    attribute=attribute?.getBufferArray?.()||attribute;
    const buffer=attribute?._buffer;
    if(buffer){const ids=buffers.get(buffer)||[];ids.push(geometry.getInstanceID());buffers.set(buffer,ids);}
  }
  const names=new Map(geometries.map(g=>[g.getInstanceID(),materials.filter(m=>m.get('geometryList').includes(g)).map(m=>m.get('name'))]));
  const programIds=new Map(),programs=[],calls=[],restore=[];
  let activeGeometry=null;
  const hash=s=>{let h=2166136261;for(let i=0;i<s.length;i++)h=Math.imul(h^s.charCodeAt(i),16777619);return (h>>>0).toString(16);};
  function getProgram() {
    const program=gl.getParameter(gl.CURRENT_PROGRAM);
    if(!program)return null;
    if(programIds.has(program))return programIds.get(program);
    const id=programs.length;
    const shaders=(gl.getAttachedShaders(program)||[]).map(s=>{
      const source=gl.getShaderSource(s)||'';
      const shaderName=source.match(/#define\s+SHADER_NAME\s+([^\n]+)/)?.[1]||'';
      const rules=[];
      if(shaderName.startsWith('PBR_')&&gl.getShaderParameter(s,gl.SHADER_TYPE)===gl.FRAGMENT_SHADER){
        for(const keyword of ['translucency','refractionRoughness','alphamask','specularF0']){
          const at=source.toLowerCase().indexOf(keyword.toLowerCase());
          if(at>=0)rules.push({keyword,excerpt:source.slice(Math.max(0,at-200),at+650)});
        }
      }
      return {stage:gl.getShaderParameter(s,gl.SHADER_TYPE),name:shaderName,length:source.length,fingerprint:hash(source),rules,
        defines:[...new Set((source.match(/^\s*#define\s+[A-Za-z0-9_]+[^\n]*/gm)||[]).map(s=>s.trim()).filter(s=>/POINT|COLOR|ALPHA|OPACITY|SKIN|MORPH|UV|NORMAL|SPECULAR|COAT|PBR|SRGB|BLOOM|RGBM/i.test(s)))]};
    });
    const attributes=[];
    for(let i=0;i<gl.getProgramParameter(program,gl.ACTIVE_ATTRIBUTES);i++)attributes.push(gl.getActiveAttrib(program,i)?.name);
    const uniformNames=[];
    for(let i=0;i<gl.getProgramParameter(program,gl.ACTIVE_UNIFORMS);i++){
      const uniform=gl.getActiveUniform(program,i);
      if(!/opacity|alphamask|cutoff|specular|roughness|gloss|f0|pointsize|color|coat|ior|transluc|modelviewmatrix|projectionmatrix/i.test(uniform.name)||uniform.size>4)continue;
      const value=gl.getUniform(program,gl.getUniformLocation(program,uniform.name));
      uniformNames.push({name:uniform.name,type:uniform.type,value:ArrayBuffer.isView(value)?Array.from(value):value});
    }
    programs.push({id,shaders,attributes,uniforms:uniformNames});programIds.set(program,id);return id;
  }
  function beforeDraw(mode,count) {
    const program=getProgram();
    let geometry=activeGeometry;
    const activeProgram=gl.getParameter(gl.CURRENT_PROGRAM);
    const vertex=activeProgram?gl.getAttribLocation(activeProgram,'Vertex'):-1;
    const candidates=vertex>=0?buffers.get(gl.getVertexAttrib(vertex,gl.VERTEX_ATTRIB_ARRAY_BUFFER_BINDING))||[]:[];
    if(geometry===null&&candidates.length===1)geometry=candidates[0];
    const uniforms=program===null?[]:programs[program].uniforms.map(u=>{
      const v=gl.getUniform(activeProgram,gl.getUniformLocation(activeProgram,u.name));
      return {name:u.name,value:ArrayBuffer.isView(v)?Array.from(v):v};
    });
    calls.push({geometry,geometry_candidates:candidates,association:activeGeometry!==null?'draw_callback':geometry!==null?'vertex_buffer':'unresolved',materials:names.get(geometry)||[],program,uniforms,mode,count,
      blend:gl.isEnabled(gl.BLEND),srcRGB:gl.getParameter(gl.BLEND_SRC_RGB),dstRGB:gl.getParameter(gl.BLEND_DST_RGB),
      srcAlpha:gl.getParameter(gl.BLEND_SRC_ALPHA),dstAlpha:gl.getParameter(gl.BLEND_DST_ALPHA),
      depthWrite:gl.getParameter(gl.DEPTH_WRITEMASK),depthTest:gl.isEnabled(gl.DEPTH_TEST),cull:gl.isEnabled(gl.CULL_FACE)});
  }
  function wrap(object,key,create) {
    const original=object[key];if(typeof original!=='function')return;
    const descriptor=Object.getOwnPropertyDescriptor(object,key);
    object[key]=create(original);
    restore.push(()=>{if(descriptor)Object.defineProperty(object,key,descriptor);else delete object[key];});
  }
  try {
    for(const geometry of geometries)wrap(geometry,'drawImplementation',original=>function(...args){
      const previous=activeGeometry;activeGeometry=geometry.getInstanceID();
      try{return original.apply(this,args);}finally{activeGeometry=previous;}
    });
    for(const name of ['drawArrays','drawElements','drawArraysInstanced','drawElementsInstanced'])wrap(gl,name,original=>function(...args){
      beforeDraw(args[0],name.includes('Elements')?args[1]:args[2]);return original.apply(this,args);
    });
    const viewport=viewer.getCamera().getViewport();
    context._viewer.getScreenShot(viewport.width(),viewport.height());
  } finally {
    for(const undo of restore.reverse())undo();
  }
  const options=model.get('options')||{};
  const effects=Object.fromEntries(Object.entries(options).filter(([name])=>/post|tone|bloom|dof|ssao|vignette|grain|sharp|color|chromatic/i.test(name)));
  const postModel=context._features.postProcess?.getModel?.();
  if(postModel)effects.viewer_postprocessing=Object.fromEntries(Object.entries(postModel.attributes).map(([k,v])=>[k,v?.attributes||v]).filter(([k,v])=>typeof v!=='function'));
  const resources=[...new Set(w.performance.getEntriesByType('resource').map(r=>{
    try{const u=new URL(r.name);if(!/osgjs|\.bin|\.binz|\.gz|texture|environment/i.test(u.pathname))return null;return u.origin+u.pathname;}catch{return null;}
  }).filter(Boolean))];
  const report={uid:model.get('uid'),title:model.get('name'),source:location.href,
    viewer:{engine:'OSG.js',evidence:'_viewerOSGJS + geometria OSG observada',webgl:gl.getParameter(gl.VERSION),glsl:gl.getParameter(gl.SHADING_LANGUAGE_VERSION)},
    pipeline:['Página Sketchfab','iframe /embed','cena OSG em memória','geometria/instâncias/materiais/animação','shaders e estados WebGL','captura própria','reconstrução Blender'],
    shading:options.shading,environment:options.environment,background:options.background,effects,
    geometry_classes:[...new Set(geometries.map(g=>g.className()))],draw_calls:calls,programs,resource_paths:resources.slice(0,100)};
  w.__link3d_diagnostic=report;
  return report;
}
