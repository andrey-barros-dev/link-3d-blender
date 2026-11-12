// Assinatura técnica do viewer; leitura de metadados, sem baixar mapas.
async (expectedUid) => {
  for (let attempt=0;attempt<90;attempt++) {
    try {
      const frame=[...document.querySelectorAll('iframe')].find(f=>f.src.includes('/embed'));
      const w=frame?.contentWindow || (location.pathname.includes('/embed')?window:null);
      if(!w?.webpackChunksketchfab)throw Error('Viewer carregando');
      let req;w.webpackChunksketchfab.push([['link3d_inspect_'+Date.now()],{},r=>req=r]);
      let context;
      for(const [id,fn] of Object.entries(req.m)) {
        const text=String(fn);
        if(!text.includes('getScenePositionFromScreenPoint')||!text.includes('getMaterialFromHit'))continue;
        context=Object.values(req(id)).find(v=>v?._rootModel&&v?._features);
        if(context)break;
      }
      if(!context)throw Error('Viewer carregando');
      const model=context._context.getModel();
      if(expectedUid&&model.get('uid')!==expectedUid)throw Error('Modelo carregando');
      if(!model.get('downloadType'))throw Error('Modelo carregando');
      w.__link3d_context=context;
      const materials=Object.values(context._features.material.getModel()._materialByStateSetID).filter(m=>m.get('geometryList').length);
      const geometries=[...new Set(materials.flatMap(m=>m.get('geometryList')))];
      const animations=context._features.animation?.getModel()?.get('animations')?.models || [];
      return {
        uid:model.get('uid'),title:model.get('name'),source:location.href,download_type:model.get('downloadType'),
        shading:model.get('options')?.shading,animation_count:model.get('animationCount')||0,
        clips:animations.map(c=>({name:c.get('name'),uid:c.get('uid'),duration:c.get('duration'),loop:c.get('isLoop')})),
        geometries:geometries.length,primitive_modes:[...new Set(geometries.flatMap(g=>g.getPrimitiveSetList().map(p=>p.getMode())))],
        attributes:[...new Set(geometries.flatMap(g=>Object.keys(g.getVertexAttributeList())))],
        materials:materials.map(m=>({name:m.get('name'),channels:Object.fromEntries(Object.entries(m.get('channels')).filter(([k,v])=>v?.get('enable')).map(([k,c])=>{
          const t=c.get('textureModel');
          return [k,{factor:c.get('factor'),type:c.get('type'),texture:!!t,uv:t?.get('texCoordUnit'),wrapS:t?.get('wrapS'),wrapT:t?.get('wrapT'),format:t?.get('multiresTexture')?._internalFormat}];
        }))}))
      };
    } catch(error) {
      if(attempt===89)throw error;
      await new Promise(resolve=>setTimeout(resolve,500));
    }
  }
}
