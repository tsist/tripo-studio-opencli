"""Studio form preparation through structured OpenCLI actions only.

All browser eval expressions in this module are read-only observations. Settings
are changed by fresh refs and verified against the live UI and reactive store.
"""
from __future__ import annotations
import json
import re
import time
from typing import Any, Callable

MODE_VALUES={'single':'imageToModel','multiview':'multiView','batch':'batchImages','text':'textToModel'}
MODE_ICONS={'single':'add-image','multiview':'multi-view','batch':'images','text':'pen'}
VISIBILITY={'shareable':'仅分享','private':'私有','public':'公开'}
FIELDS=['tab','mode','lpVersion','hdVersion','useSmartMeshV2','quad','faceLimit','modelFaceLimits','privateMode',
        'generateParts','smartPoly','texture','pbr','textureQuality','geometryQuality','delight','enableImageAutofix','modelCount','canUseMultiModel','supportsMultiModel','useSameFaceLimit']

class StudioUI:
    def __init__(self, call: Callable[...,str], decode: Callable[[str],Any]):
        self.call=call;self.decode=decode;self.actions=[]

    def command(self, operation: str, *args: str) -> Any:
        raw=self.call(operation,*args)
        if operation in {'keys','state','get'}:
            try:return self.decode(raw)
            except ValueError:return raw
        return self.decode(raw)

    def observe(self) -> dict[str,Any]:
        js=r'''(() => {
          const nuxt=window.useNuxtApp();const st=nuxt.$pinia._s.get("workspace-generate-store");
          const fields=FIELDS_PLACEHOLDER;const state=st?Object.fromEntries(fields.map(k=>[k,st[k]])):{};
          const visible=e=>!e.closest('[role="dialog"][data-state="closed"],[role="listbox"][data-state="closed"],[inert],[aria-hidden="true"]')&&Boolean(e.getClientRects().length)&&getComputedStyle(e).visibility!=="hidden";
          const label=e=>{let p=e.parentElement;for(let i=0;p&&i<5;i++,p=p.parentElement){
            const t=(p.innerText||"").trim();if(t&&t.length<150)return t;}return "";};
          const controls=Array.from(document.querySelectorAll("button[role=switch],input[type=checkbox]"))
            .map((e,nth)=>({nth,visible:visible(e),disabled:e.disabled===true||e.getAttribute('aria-disabled')==='true',label:label(e),checked:e.getAttribute("aria-checked")==="true"||e.checked===true}));
          const active=v=>Array.isArray(v)?v.some(active):(v&&typeof v==="object")?Object.values(v).some(active):v===true;
          const buttons=Array.from(document.querySelectorAll("button")).filter(visible);
          const submit=buttons.find(e=>e.getAttribute("data-trace")?.includes("generate_model_button"))||buttons.find(e=>/^生成模型/.test(e.innerText.trim()));
          return {url:location.origin+location.pathname,state,controls,
            prompt:st?.textToModel?.prompt||"",t_pose:st?.textToModel?.t_pose,
            image_ready:Boolean(st?.imageToModel?.key),
            multiview_ready:Array.isArray(st?.multiViewImages)?[0,1,3,2].map(i=>Boolean(st.multiViewImages[i]?.key||st.multiViewImages[i]?.image?.key)):[],
            batch_ready:Array.isArray(st?.batchImages)?st.batchImages.filter(e=>Boolean(e?.key)).length:0,
            uploading:active(st?.imageToModelUploading)||active(st?.multiViewUploading)||active(st?.batchUploading)||active(st?.batchImagesUploading),
            submit:submit?{text:submit.innerText.trim(),disabled:submit.disabled,trace:submit.getAttribute("data-trace")}:null,
            dialogs:Array.from(document.querySelectorAll('[role="dialog"][data-state="open"]')).filter(visible).map(e=>e.innerText.slice(0,2500))};
        })()'''.replace('FIELDS_PLACEHOLDER',json.dumps(FIELDS))
        return self.command('eval',js)

    def expect(self, predicate: Callable[[dict],bool], description: str, timeout: float=7) -> dict:
        deadline=time.monotonic()+timeout
        while True:
            state=self.observe()
            if predicate(state):return state
            if time.monotonic()>=deadline:
                raise RuntimeError(f'UI verification failed: {description}; observed={state["state"]}')
            time.sleep(.25)

    def find(self, css: str) -> list[dict]:
        found=self.command('find','--css',css,'--limit','200','--text-max','250')
        return [e for e in found.get('entries',[]) if e.get('visible')]

    def one(self, css: str, text: str|None=None, prefix: bool=False) -> dict:
        entries=self.find(css)
        if text is not None:
            norm=lambda s:re.sub(r'\s+',' ',s).strip()
            entries=[e for e in entries if (norm(e.get('text','')).startswith(norm(text)) if prefix else norm(e.get('text',''))==norm(text))]
        if len(entries)!=1:
            raise RuntimeError(f'Expected one live control: css={css!r}, text={text!r}; matches={len(entries)}')
        return entries[0]

    def click(self, entry: dict) -> dict:
        if re.match(r'^(生成模型|开始拆分|生成贴图|生成PBR|自动绑定|生成 UV|开始补全)',entry.get('text','').strip()):
            raise RuntimeError('Form preparation cannot click a submit control.')
        result=self.command('click',str(entry['ref']))
        if not result.get('clicked'):
            raise RuntimeError('Structured click was not acknowledged.')
        self.actions.append({'action':'click','text':entry.get('text',''),'match_level':result.get('match_level'),'method':result.get('click_method'),'hit':result.get('hit')})
        return result

    def close_dialog(self) -> None:
        entries=self.find('[role="dialog"][data-state="open"] button:has([class~="i-tripo:close"])') if self.observe()['dialogs'] else []
        if entries:
            if len(entries)!=1:raise RuntimeError('Multiple open settings dialogs; refuse ambiguous close.')
            self.click(entries[0])
            self.expect(lambda s:not s['dialogs'],'settings dialog closed')

    def set_engine(self, tier: str) -> None:
        self.close_dialog();want='low_poly' if tier=='smartmesh' else 'high_detail'
        if self.observe()['state'].get('tab')!=want:
            self.click(self.one('button','智能网格' if tier=='smartmesh' else '高精度模型'))
        self.expect(lambda s:s['state'].get('tab')==want,f'engine {tier}')

    def set_mode(self, mode: str) -> None:
        self.close_dialog();want=MODE_VALUES[mode]
        if self.observe()['state'].get('mode')!=want:
            self.click(self.one(f'div.mb-3 button:has([class~="i-tripo:{MODE_ICONS[mode]}"])'))
        self.expect(lambda s:s['state'].get('mode')==want,f'input mode {mode}')

    def set_visibility(self, visibility: str) -> None:
        self.close_dialog()
        if self.observe()['state'].get('privateMode')!=visibility:
            options=set(VISIBILITY.values())
            boxes=[e for e in self.find('button[role="combobox"]') if e.get('text','').strip() in options]
            if len(boxes)!=1:raise RuntimeError('Privacy combobox not uniquely identified.')
            self.click(boxes[0]);self.click(self.one('[role="option"]',VISIBILITY[visibility],prefix=True))
        self.expect(lambda s:s['state'].get('privateMode')==visibility,f'privacy {visibility}')

    def set_version(self, tier: str, version: str) -> None:
        field='lpVersion' if tier=='smartmesh' else 'hdVersion'
        if self.observe()['state'].get(field)==version:return
        boxes=[e for e in self.find('button[role="combobox"]') if re.match(r'^(P[12]\.0|v[0-9])',e.get('text','').strip())]
        if len(boxes)!=1:raise RuntimeError('Model-version combobox not uniquely identified.')
        self.click(boxes[0])
        label=('P2.0' if '2.0' in version else 'P1.0') if tier=='smartmesh' else version.split('-')[0]
        self.click(self.one('[role="option"]',label,prefix=True))
        self.expect(lambda s:s['state'].get(field)==version,f'model version {version}')

    def open_general(self, tier: str) -> None:
        self.close_dialog();label='拓扑' if tier=='smartmesh' else '几何与贴图'
        self.click(self.one('[aria-haspopup="dialog"]',label))
        self.expect(lambda s:bool(s['dialogs']),f'{label} dialog')

    def set_switch(self, label: str, wanted: bool, field: str|None=None) -> None:
        observed=self.observe()
        matches=[c for c in observed['controls'] if c['visible'] and label in c['label']]
        if len(matches)!=1:raise RuntimeError(f'Switch label not unique: {label}; visible={[c["label"] for c in observed["controls"] if c["visible"]]}')
        c=matches[0]
        if c['checked']!=wanted:
            if c.get('disabled'):raise ValueError(f'Studio disabled this option for the current source/mode: {label}; no click.')
            entries=[e for e in self.find('button[role=switch],input[type=checkbox]') if e['nth']==c['nth']]
            if len(entries)!=1:raise RuntimeError(f'Switch identity changed: {label}')
            self.click(entries[0])
        if field:self.expect(lambda s:s['state'].get(field)==wanted,f'{label}={wanted}')
        else:self.expect(lambda s:any(x['visible'] and label in x['label'] and x['checked']==wanted for x in s['controls']),f'{label}={wanted}')

    def set_hd_smart_poly(self, wanted: bool) -> None:
        """Set HD Smart Poly only through the live structured switch.

        The frozen public-workspace.js contains the switch label ``智能网格``
        (with a ``P1.0`` badge) for retopology, and no ``智能布线`` label
        anywhere.  The HD generation panel in that bundle does not render a
        Smart Poly item, so a missing live control is a hard evidence failure,
        not a reason to write Pinia or guess a selector.
        """
        observed=self.observe()
        matches=[c for c in observed['controls'] if c['visible'] and '智能网格' in c['label']]
        if len(matches)!=1:
            labels=[c['label'] for c in observed['controls'] if c['visible']]
            raise RuntimeError(
                "HD generate Smart Poly switch was not found. "
                "public-workspace.js has no '智能布线' control; its only "
                "'智能网格 P1.0' switch is in the retopology panel, and the "
                "frozen HD generation panel shows no Smart Poly item. "
                f"No Pinia write or guessed selector is used. visible={labels}"
            )
        self.set_switch('智能网格',wanted,'smartPoly')


    def set_face_limit(self, limit: int, variations: int=1) -> None:
        if limit<=0:return
        sliders=self.find('[role="dialog"][data-state="open"] [role="slider"]')
        # Read the actual range, do not silently clamp a requested face budget.
        if len(sliders)!=1:raise RuntimeError('Face-count slider not unique.')
        attrs=self.command('get','attributes',str(sliders[0]['ref']))['value']
        if not int(attrs['aria-valuemin'])<=limit<=int(attrs['aria-valuemax']):
            raise ValueError(f'Face limit {limit} outside live UI range {attrs["aria-valuemin"]}..{attrs["aria-valuemax"]}')
        inputs=self.find('[role="dialog"][data-state="open"] input[type="text"]')
        if len(inputs)!=1:raise RuntimeError('Face-count input not unique.')
        r=self.command('fill',str(inputs[0]['ref']),str(limit))
        if not r.get('verified'):raise RuntimeError('Face-count input rejected the requested value.')
        self.command('keys','Tab')
        self.expect(lambda s:s['state'].get('faceLimit')==limit,f'face count {limit}')
        if self.observe()['state'].get('tab')=='low_poly':
            self.expect(lambda s:s['state'].get('modelFaceLimits')==[limit]*variations,'all variation face counts')

    def prepare_generate(self, args) -> dict:
        self.set_engine(args.tier);self.set_mode(args.mode)
        self.set_version(args.tier,args.lp_version if args.tier=='smartmesh' else args.hd_version)
        self.set_visibility(args.visibility)
        # Parts changes reset geometry/topology/face defaults. Apply it before
        # the general panel and the final requested face limit.
        if args.tier=='hd':self.set_switch('分部件生成',args.generate_parts,'generateParts')
        if args.tier=='smartmesh':
            if self.observe()['state'].get('supportsMultiModel'):
                self.click(self.one('button[value="'+str(args.variations)+'"]',str(args.variations)))
            elif args.variations!=1:raise ValueError('This mode/version has no multiple-model control.')
            self.expect(lambda s:len(s['state'].get('modelFaceLimits',[]))==args.variations,'variation count')
        elif args.variations!=1:
            raise ValueError('High Detail has no verified variation-count control; use batch inputs or variations=1.')
        self.open_general(args.tier)
        if args.tier=='smartmesh' and args.variations>1:self.set_switch('使用相同面数',True,'useSameFaceLimit')
        if args.tier=='hd':self.prepare_hd_options(args)
        topology=self.one('[role="dialog"][data-state="open"] button[value="'+('quad' if args.quad else 'triangle')+'"]')
        topology_attrs=self.command('get','attributes',str(topology['ref']))['value']
        if 'disabled' in topology_attrs or topology_attrs.get('aria-disabled')=='true':
            raise ValueError('Requested topology is disabled by the current Studio mode/version; no click or submit.')
        self.click(topology)
        self.expect(lambda s:s['state'].get('quad')==('quad' if args.quad else 'triangle'),'topology selection')
        self.set_face_limit(args.face_limit,args.variations)
        # HD-only settings are configured after live control mapping, never Pinia writes.
        self.close_dialog()
        if args.mode=='text':
            entry=self.one('textarea')
            r=self.command('fill',str(entry['ref']),args.prompt)
            if not r.get('verified'):raise RuntimeError('Prompt input did not retain its value.')
            self.expect(lambda s:s['prompt']==args.prompt,'text prompt')
            if self.observe().get('t_pose')!=args.t_pose:
                self.click(self.one('button','T-姿势'))
            self.expect(lambda s:s.get('t_pose')==args.t_pose,'T-pose selection')
        return {'observed':self.observe(),'actions':self.actions}

    def upload_inputs(self,args) -> dict:
        from pathlib import Path
        selector='input[type="file"][accept*="image"]'
        if args.mode in {'single','batch'}:
            result=self.command('upload',selector,*[str(Path(p).resolve()) for p in args.images])
            if not result.get('uploaded'):raise RuntimeError('Image upload not acknowledged.')
        elif args.mode=='multiview':
            for nth,path in enumerate([args.front,args.left,args.right,args.back]):
                if path:
                    # A completed view replaces its file input. Re-identify the named
                    # slot before every upload instead of reusing a shrinking nth list.
                    inputs=self.command('eval',r'''Array.from(document.querySelectorAll('input[type="file"][accept*="image"]')).map((e,nth)=>({nth,label:(e.parentElement?.innerText||'').trim().split('\n')[0]}))''')
                    label=['前','左侧','右侧','后'][nth]
                    matches=[e for e in inputs if e.get('label')==label]
                    if len(matches)!=1:raise RuntimeError(f'View input {label} was not uniquely identified.')
                    result=self.command('upload',selector,'--nth',str(matches[0]['nth']),str(Path(path).resolve()))
                    if not result.get('uploaded'):raise RuntimeError(f'View slot {nth} upload not acknowledged.')
                    self.expect(lambda s:len(s.get('multiview_ready',[]))>nth and s['multiview_ready'][nth] and not s['uploading'],f'view {label} audited',timeout=45)
        return self.expect(lambda s:self.inputs_ready(s,args),'every requested input audited and ready',timeout=45)

    @staticmethod
    def inputs_ready(s,args) -> bool:
        if s.get('uploading'):return False
        if args.mode=='single':return s.get('image_ready') is True
        if args.mode=='batch':return s.get('batch_ready')==len(args.images)
        if args.mode=='text':return s.get('prompt')==args.prompt and bool(args.prompt.strip())
        slots=[args.front,args.left,args.right,args.back]
        ready=s.get('multiview_ready',[])
        return all(i<len(ready) and ready[i] for i,p in enumerate(slots) if p)

    def verify_generate(self,args,require_inputs=False) -> dict:
        s=self.observe();st=s['state']
        expected={'tab':'low_poly' if args.tier=='smartmesh' else 'high_detail','mode':MODE_VALUES[args.mode],
                  'quad':'quad' if args.quad else 'triangle','privateMode':args.visibility}
        expected['lpVersion' if args.tier=='smartmesh' else 'hdVersion']=args.lp_version if args.tier=='smartmesh' else args.hd_version
        if args.face_limit>0:expected['faceLimit']=args.face_limit
        if args.tier=='smartmesh':
            expected['modelFaceLimits']=[st['faceLimit']]*args.variations
            expected['modelCount']=args.variations
        else:
            expected.update(generateParts=args.generate_parts,smartPoly=args.smart_poly,texture=not args.no_texture)
            if args.hd_version.startswith('v3.1'):expected['geometryQuality']=args.geometry_quality
            if not args.no_texture:expected.update(textureQuality=args.texture_quality,pbr=args.pbr)
        mismatches={k:{'wanted':v,'actual':st.get(k)} for k,v in expected.items() if st.get(k)!=v}
        if args.mode=='text' and (s['prompt']!=args.prompt or s.get('t_pose')!=args.t_pose):mismatches['text']='prompt or T-pose differs'
        if mismatches:raise RuntimeError(f'Generate parameter mismatch: {mismatches}')
        if require_inputs and (not self.inputs_ready(s,args) or not s.get('submit') or s['submit']['disabled']):
            raise RuntimeError('Inputs or submit control are not ready.')
        return s

    def prepare_hd_options(self,args) -> None:
        if args.hd_version.startswith('v3.1'):
            self.set_switch('超清几何精度',args.geometry_quality=='detailed')
            self.expect(lambda s:s['state'].get('geometryQuality')==args.geometry_quality,'geometry quality')
        elif args.geometry_quality!='detailed':
            raise ValueError('This HD version has no geometry-quality control; only the default/not-applicable setting is accepted.')
        self.set_switch('贴图生成',not args.no_texture,'texture')
        if not args.no_texture:
            label={'standard':'2K','detailed':'4K','extreme':'8K'}[args.texture_quality]
            self.click(self.one('[role="dialog"][data-state="open"] button',label,prefix=True))
            self.expect(lambda s:s['state'].get('textureQuality')==args.texture_quality,'texture quality')
            self.set_switch('PBR',args.pbr,'pbr')
        elif args.pbr:
            raise ValueError('PBR requires texture generation.')
        if args.smart_poly or self.observe()['state'].get('smartPoly'):
            self.set_hd_smart_poly(args.smart_poly)
