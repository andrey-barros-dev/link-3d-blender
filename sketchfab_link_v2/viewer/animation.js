// Executar no Liberty depois de 01_capturar_viewer.js, na mesma página.
// Captura ossos, pesos, morph targets e amostras da animação, sem scripts do add-on.
async () => {
  const w = document.querySelector('iframe[src*="/embed"]')?.contentWindow || window;
  const context = w.__link3d_context, data = w.__link3d_capture;
  if (!context || !data) throw Error('Execute a captura estática primeiro');
  const feature = context._features.animation;
  const model = feature.getModel(), view = feature._views[0];
  const collection = model.get('animations');
  const clips = (collection.models || []).filter(a => a.get('isEnabled'));
  if (!clips.length) throw Error('Nenhum clipe habilitado');
  const geometries = Object.values(context._features.material.getModel()._materialByStateSetID).flatMap(m => m.get('geometryList'));
  const byId = new Map(geometries.map(g => [g.getInstanceID(), g]));
  const rigSources = [], skeletonMap = new Map();
  data.rigs = [];
  const meshSources = data.meshes.map((mesh, index) => {
    const geometry = byId.get(mesh.geometryId);
    if (!geometry) throw Error('Geometria ausente: ' + index);
    if (geometry.className() === 'RigGeometry') {
      const skeleton = geometry.getSkeleton(), id = skeleton.getInstanceID();
      if (!skeletonMap.has(id)) {
        const bones = [], seen = new Set();
        function visit(n) {
          if (typeof n.getInvBindMatrixInSkeletonSpace === 'function' && !seen.has(n.getInstanceID())) {
            seen.add(n.getInstanceID()); bones.push(n);
          }
          for (const child of n.getChildren?.() || []) visit(child);
        }
        visit(skeleton);
        const rig = data.rigs.length;
        skeletonMap.set(id, rig);
        rigSources.push({skeleton, bones});
        data.rigs.push({name:skeleton.getName() || 'Skeleton_' + id,
          world:Array.from(skeleton.getWorldMatrices()[0]),
          bones:bones.map(b => ({id:b.getInstanceID(), name:b.getName() || 'Bone_' + b.getInstanceID(),
            parent:b.getBoneParent?.()?.getInstanceID() ?? null,
            inverseBind:Array.from(b.getInvBindMatrixInSkeletonSpace())}))});
      }
      const attrs = geometry.getVertexAttributeList();
      mesh.skin = {rig:skeletonMap.get(id),
        palette:geometry._rigTransformImplementation._bones.map(b => b.getInstanceID()),
        joints:Array.from(attrs.Bones.getElements()), weights:Array.from(attrs.Weights.getElements())};
    }
    const source = geometry.getSourceGeometry?.() || geometry;
    let morphCallback = null;
    if (source.className() === 'MorphGeometry') {
      morphCallback = view._manager._animationsUpdateCallbackArray.find(cb => cb._morphs?.includes(source));
      if (!morphCallback) throw Error('Controlador de morph não encontrado');
      const targets = source.getMorphTargets();
      mesh.morphs = targets.map((target, i) => ({name:morphCallback.getTargetName(i) || target.getName() || 'Morph_' + i,
        positions:Array.from(target.getVertexAttributeList().Vertex.getElements())}));
    }
    return {geometry, morph:source.className() === 'MorphGeometry' ? source : null, morphCallback, index};
  });
  const saved = {uid:model.get('currentUID'), playing:model.get('playing'), progress:model.get('progress') || 0};
  // frame() também atualiza o viewer quando a aba está em segundo plano.
  // Esperar apenas requestAnimationFrame pode suspender a captura indefinidamente.
  const tick = async () => {
    context._viewerOSGJS.frame();
    await new Promise(resolve => setTimeout(resolve, 0));
  };
  const minimum = [Infinity,Infinity,Infinity], maximum = [-Infinity,-Infinity,-Infinity];
  const fps = 30;
  data.animations = [];
  const point = (p,m) => [m[0]*p[0]+m[4]*p[1]+m[8]*p[2]+m[12],m[1]*p[0]+m[5]*p[1]+m[9]*p[2]+m[13],m[2]*p[0]+m[6]*p[1]+m[10]*p[2]+m[14]];
  try {
    if (model.get('playing')) model.set('playing',false);
    for (const clip of clips) {
      model.set('currentUID',clip.get('uid'));
      await Promise.all(view._animationPromises || []);
      await tick();
      const duration = clip.get('duration'), count = Math.ceil(duration*fps)+1;
      const output = {uid:clip.get('uid'),name:clip.get('name'),duration,fps,loop:clip.get('isLoop'),frames:[],samples:[]};
      for (let frame=0;frame<count;frame++) {
        const time = Math.min(frame/fps,duration);
        view.onSeek(time/duration*100);
        await tick();
        const meshMatrices = meshSources.map(({geometry,index}) => Array.from(geometry.getWorldMatrices()[data.meshes[index].instanceIndex || 0]));
        output.frames.push({time,
          rigWorlds:rigSources.map(({skeleton})=>Array.from(skeleton.getWorldMatrices()[0])),
          bones:rigSources.map(({bones})=>bones.flatMap(b=>Array.from(b.getMatrixInSkeletonSpace()))),
          meshes:meshMatrices,
          morphs:meshSources.map(({morphCallback})=>morphCallback?Array.from({length:morphCallback.getNumTarget()},(_,i)=>morphCallback.getTarget(i).value):null)});
        const validate = frame===0 || frame===Math.floor(count/2) || frame===count-1;
        if (validate || frame%10===0) {
          for (const {geometry,index} of meshSources) {
            geometry._rigTransformImplementation?.update(geometry);
            const positions = geometry.computeTransformedVertices?.() || geometry.getVertexAttributeList().Vertex.getElements();
            const matrix = meshMatrices[index], samples=[];
            for(let i=0;i<positions.length;i+=3){
              const p=point(positions.subarray(i,i+3),matrix);
              if(!p.every(Number.isFinite))throw Error('Posição animada inválida');
              for(let axis=0;axis<3;axis++){minimum[axis]=Math.min(minimum[axis],p[axis]);maximum[axis]=Math.max(maximum[axis],p[axis]);}
            }
            if(validate){
              const n=positions.length/3;
              for(let k=0;k<8;k++){const vertex=Math.floor((n-1)*k/7);samples.push({index:vertex,position:point(positions.subarray(vertex*3,vertex*3+3),matrix)});}
              output.samples.push({frame:frame+1,mesh:index,vertices:samples});
            }
          }
        }
      }
      data.animations.push(output);
    }
    data.animation_bounds = [...minimum,...maximum];
  } finally {
    model.set('currentUID',saved.uid);
    view.onSeek(saved.progress);
    model.set('playing',saved.playing);
  }
  return {rigs:data.rigs.length,bones:data.rigs.reduce((n,r)=>n+r.bones.length,0),
    clips:data.animations.map(a=>({name:a.name,duration:a.duration,frames:a.frames.length})),
    morphMeshes:data.meshes.filter(m=>m.morphs?.length).length};
}
