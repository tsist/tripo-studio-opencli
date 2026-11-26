"""Postprocess forms use normal controls; page evaluation only observes state."""
import json,time
from pathlib import Path
from tripo_pricing import resolve_rig_version
from tripo_ui import StudioUI

class PostUI(StudioUI):
    def __init__(self,call,decode,operation,config):
        super().__init__(call,decode);self.operation=operation;self.config=config
        self.reference_uploaded=False;self.reference_skipped=False
    def edit_preview_state(self):
        # Hash URL paths and matrices inside the page. Signed image URLs never
        # leave the browser or enter the ledger. Query renewal preserves identity.
        return self.command('eval',r'''(async()=>{
          const p=window.useNuxtApp().$pinia,w=p._s.get('workspace-store'),s=p._s.get('workspace-magic-brush-store');
          const visible=e=>Boolean(e.getClientRects().length)&&!e.closest('[inert],[aria-hidden="true"]');
          const buttons=Array.from(document.querySelectorAll('button')).filter(visible);
          const save=buttons.filter(e=>e.innerText.trim()==='保存'&&e.classList.contains('btn-bg-purple-1'));
          const images=await Promise.all((s?.magicBrushImages||[]).map(async(im,index)=>{
            if(!im?.url||!im?.camera_matrix)return {index,fingerprint:null};
            const u=new URL(im.url,location.href);
            const bytes=new TextEncoder().encode(JSON.stringify([u.origin+u.pathname,im.camera_matrix]));
            const digest=await crypto.subtle.digest('SHA-256',bytes);
            return {index,fingerprint:Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('')};
          }));
          return {project_id:w?.project?.id,project_ready:w?.projectReady===true,readonly:w?.readonly,
            source_operator_id:w?.project?.operator?.operator_id,operator_status:w?.project?.operator?.status,
            images,selected_index:s?.magicBrushImageIndex,
            apply_submit:save.length===1?{text:save[0].innerText.trim(),disabled:save[0].disabled}:null};
        })()''')
    def wait_edit_state(self,predicate,description,timeout=30):
        deadline=time.monotonic()+timeout
        while True:
            s=self.edit_preview_state()
            if predicate(s):return s
            if time.monotonic()>=deadline:raise RuntimeError('Texture edit state was not verified: '+description)
            time.sleep(.5)
    def prepare_edit_apply(self,args,preview):
        s=self.wait_edit_state(lambda x:x.get('project_ready') and x.get('project_id')==args.project_id and x.get('apply_submit'), 'loaded viewer and save control')
        if s.get('readonly') or s['apply_submit']['disabled']:raise RuntimeError('Texture edit source is not editable or textured; no apply.')
        if s.get('source_operator_id')!=preview.get('source_operator_id'):raise RuntimeError('Texture edit source operator changed since preview; no apply.')
        matches=[im for im in s['images'] if im.get('fingerprint')==preview.get('fingerprint')]
        if len(matches)!=1:raise RuntimeError('The recorded preview is missing or ambiguous; do not regenerate or apply another image.')
        index=matches[0]['index']
        entries=self.find('button.flex-shrink-0.size-16.bg-cover.bg-center[style*="background-image"]')
        if len(entries)!=len(s['images']) or index>=len(entries):raise RuntimeError('Preview controls do not match the observed images.')
        self.click(entries[index])
        s=self.wait_edit_state(lambda x:x.get('selected_index')==index and index<len(x['images']) and x['images'][index].get('fingerprint')==preview['fingerprint'],'recorded preview selected')
        if s['readonly'] or not s.get('apply_submit') or s['apply_submit']['disabled']:raise RuntimeError('Apply lost readiness; no submit.')
        return s
    def edit_apply_entry(self):
        return self.one('button.btn-bg-purple-1','保存')
    def uv_state(self):
        return self.command('eval',r'''(()=>{
          const w=window.useNuxtApp().$pinia._s.get('workspace-store');
          const visible=e=>Boolean(e.getClientRects().length)&&!e.closest('[inert],[aria-hidden="true"]');
          const buttons=Array.from(document.querySelectorAll('button')).filter(visible);
          const save=buttons.filter(e=>e.innerText.trim()==='保存');
          const primary=buttons.filter(e=>/^(展开 UV|免费重试|重试)(\s|$)/.test(e.innerText.trim()));
          return {project_id:w?.project?.id,project_ready:w?.projectReady===true,source_operator_id:w?.project?.operator?.operator_id,readonly:w?.readonly,
            candidates:buttons.filter(e=>e.hasAttribute('data-candidate-id')).map(e=>({operator_id:e.getAttribute('data-candidate-id'),selected:e.getAttribute('aria-pressed')==='true'})),
            apply_submit:save.length===1?{text:save[0].innerText.trim(),disabled:save[0].disabled}:null,
            primary:primary.length===1?{text:primary[0].innerText.trim(),disabled:primary[0].disabled}:null};
        })()''')
    def wait_uv_state(self,predicate,description,timeout=30):
        deadline=time.monotonic()+timeout
        while True:
            s=self.uv_state()
            if predicate(s):return s
            if time.monotonic()>=deadline:raise RuntimeError('UV state was not verified: '+description)
            time.sleep(.5)
    def prepare_uv_apply(self,args,preview):
        s=self.wait_uv_state(lambda x:x.get('project_ready') and x.get('project_id')==args.project_id and any(c['operator_id']==preview['candidate_operator_id'] for c in x['candidates']),'recorded UV candidate loaded')
        if s.get('source_operator_id')!=preview['source_operator_id'] or s.get('readonly'):raise RuntimeError('UV source changed or is not editable; no apply.')
        self.click(self.one('button[data-candidate-id="'+preview['candidate_operator_id']+'"]'))
        return self.wait_uv_state(lambda x:any(c['operator_id']==preview['candidate_operator_id'] and c['selected'] for c in x['candidates']) and x.get('apply_submit') and not x['apply_submit']['disabled'],'recorded UV candidate selected and preview ready')
    def rig_state(self):
        return self.command('eval',r'''(()=>{const w=window.useNuxtApp().$pinia._s.get('workspace-store'),o=w?.project?.operator,t=o?.rigging?.type;return {project_id:w?.project?.id,source_operator_id:o?.operator_id,is_rigged:o?.is_rigged===true,service_rig_type:t||null,rig_type:t==='quadruped'?'animal':t||null};})()''')
    def animation_state(self):
        return self.command('eval',r'''(()=>{
          const w=window.useNuxtApp().$pinia._s.get('workspace-store'),o=w?.project?.operator;
          const cards=Array.from(document.querySelectorAll('button')).filter(e=>e.classList.contains('w-26.5')&&e.querySelector('img[alt]')&&e.getClientRects().length).map(e=>({label:e.querySelector('img').alt,text:e.innerText.trim().replace(/\s+/g,' '),disabled:e.disabled,cached:Boolean(e.querySelector('[class~="i-tripo:check-filled"]')),selected:e.classList.contains('border-purple-1!')}));
          const t=o?.rigging?.type;return {project_id:w?.project?.id,source_operator_id:o?.operator_id,readonly:w?.readonly,service_rig_type:t||null,rig_type:t==='quadruped'?'animal':t||null,cards,retarget_ops:(o?.retarget||[]).map(a=>({operator_id:a.operator_id||null,name:a.name||null,has_model:Boolean(a.model_url)}))};
        })()''')
    def animation_entry(self,label):
        return self.one('button:has(img[alt='+json.dumps(label,ensure_ascii=False)+'])')
    def wait_animation_state(self,predicate,description,timeout=30):
        deadline=time.monotonic()+timeout
        while True:
            s=self.animation_state()
            if predicate(s):return s
            if time.monotonic()>=deadline:raise RuntimeError('Animation state was not verified: '+description)
            time.sleep(.5)
    def observe(self):
        js=r'''(opts=>{
          const p=window.useNuxtApp().$pinia,w=p._s.get('workspace-store');
          const visible=e=>!e.closest('[role="dialog"][data-state="closed"],[role="listbox"][data-state="closed"],[inert],[aria-hidden="true"]')&&Boolean(e.getClientRects().length)&&getComputedStyle(e).visibility!=="hidden";
          const buttons=Array.from(document.querySelectorAll('button')).filter(visible);
          const prefixes={'smart-uv':'展开 UV','fill-parts':'开始补全'};
          const b=opts.trace?buttons.find(e=>e.getAttribute('data-trace')===opts.trace):buttons.find(e=>opts.operation==='smart-uv'?/^(展开 UV|免费重试|重试)(\s|$)/.test(e.innerText.trim()):(e.innerText||'').trim().startsWith(prefixes[opts.operation]||'__unmapped__'));
          let creditProp=null,comp=b?.__vueParentComponent;
          for(let i=0;comp&&i<6;i++,comp=comp.parent){if(Object.hasOwn(comp.props||{},'credits')&&Number.isInteger(comp.props.credits)){creditProp=comp.props.credits;break;}}
          let priceEvidence=null;
          const membership=p._s.get('user-store')?.benefits?.membership?.membership_level;
          // The verified immutable frontend component omits its badge when
          // premium texture preview costs zero. Pin this fallback to that build.
          const ruleBundle=performance.getEntriesByType('resource').some(e=>e.name==='https://studio.tripo3d.com/_nuxt/oTYZ5lRn.js');
          if(opts.operation==='texture-edit'&&membership==='premium'&&ruleBundle&&!/\d/.test(b?.innerText||'')){
            creditProp=0;priceEvidence={kind:'verified_frontend_rule',bundle:'oTYZ5lRn.js',membership:'premium',credits:0};
          }
          if(opts.operation==='fill-parts'&&b&&ruleBundle&&!/\d/.test(b.innerText||'')){
            creditProp=0;priceEvidence={kind:'verified_frontend_rule',bundle:'oTYZ5lRn.js',credits:0};
          }
          const fields={'workspace-segmentation-store':['completionMode','granularityLevel'],
            'workspace-retopology-store':['topology','faceLimit','smartPoly','smartPolyDisabled'],
            'workspace-texture-store':['textureQuality','upscaleTextureQuality','textureImageUploading','textureStyleUploading','textureStyleName','textureTab'],
            'workspace-rigging-store':['modelVersion']};
          const state={};for(const [id,keys]of Object.entries(fields)){const s=p._s.get(id);if(s)for(const k of keys)state[k]=s[k];}
          const magic=p._s.get('workspace-magic-brush-store');
          state.magicBrushType=magic?.magicBrushType;state.prompt=magic?.magicBrushPrompt?.text;
          const tex=p._s.get('workspace-texture-store');
          state.texture_prompt=tex?.textureText||'';state.texture_tag_count=tex?.textureTags?.length||0;
          const textureTab=tex?.textureTab;
          const styleName=String(tex?.textureStyleName||'无');
          const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
          const referenceReady=Boolean(tex?.textureStyle?.key)&&tex?.textureStyleUploading!==true;
          const baseReady=textureTab==='imageGenerate'?Boolean(tex?.textureImage?.key)
            :textureTab==='multiViewGenerate'?Boolean(Array.isArray(tex?.textureMultiViewImages)&&tex.textureMultiViewImages.some(e=>Boolean(e?.key)))
            :textureTab==='textGenerate'?Boolean(String(tex?.textureText||'').trim()):false;
          const active=v=>Array.isArray(v)?v.some(active):(v&&typeof v==='object')?Object.values(v).some(active):v===true;
          const uploading=active(tex?.textureImageUploading)||active(tex?.textureStyleUploading)||active(tex?.textureMultiViewUploading);
          const styleAvailable=Boolean(textureTab)&&textureTab!=='multiViewGenerate';
          const styleButton=buttons.find(e=>norm(e.innerText)===styleName);
          const styleRoot=Array.from(document.querySelectorAll('.w-163'))
            .find(e=>visible(e)&&(e.querySelector('input[type="file"]')||(e.innerText||'').includes('自定义你的贴图风格')))||null;
          const stylePanelOpen=Boolean(styleRoot)||Array.from(document.querySelectorAll('div.cursor-pointer.relative'))
            .some(e=>visible(e)&&['潮流机械','复古经典','机甲','木质'].includes(norm(e.innerText)));
          const fileInputs=Array.from(document.querySelectorAll('input[type="file"]'));
          let styleUploadIndex=null;
          if(styleRoot){
            const local=Array.from(styleRoot.querySelectorAll('input[type="file"]'));
            const imageLocal=local.filter(e=>/webp|jpe?g|png/i.test(String(e.accept||'')));
            const chosen=local.length===1?local[0]:(imageLocal.length===1?imageLocal[0]:null);
            if(chosen){const idx=fileInputs.indexOf(chosen);if(idx>=0)styleUploadIndex=idx;}
          }
          const styleControlVisible=Boolean(styleButton)||Boolean(tex?.textureStyleUploading)||stylePanelOpen||styleUploadIndex!==null;
          state.reference_ready=referenceReady;state.base_ready=baseReady;state.texture_style_name=styleName;
          const source=w?.project?.operator||{};
          const sourceProperties=Object.fromEntries(['is_quad','is_rigged','is_segmented','is_textured','is_ultra_textured'].map(k=>[k,source[k]===true]));
          sourceProperties.geometric_quad=Array.from(document.querySelectorAll('span,p,div')).some(e=>visible(e)&&e.innerText.trim()==='Quad');
          const controls=Array.from(document.querySelectorAll('button[role=switch],input[type=checkbox]')).map((e,nth)=>({nth,visible:visible(e),disabled:e.disabled===true||e.getAttribute('aria-disabled')==='true',checked:e.getAttribute('aria-checked')==='true'||e.checked===true,label:e.parentElement?.innerText?.trim()}));
          return {location_path:location.pathname,project_ready:w?.projectReady===true,project_id:w?.project?.id,readonly:w?.readonly,source_properties:sourceProperties,state,controls,dialogs:[],
            reference_ready:referenceReady,base_ready:baseReady,uploading,style_available:styleAvailable,
            style_control_visible:styleControlVisible,style_panel_open:stylePanelOpen,
            style_upload_available:styleUploadIndex!==null,style_upload_index:styleUploadIndex,
            texture_style_name:styleName,
            submit:b?{text:b.innerText.trim(),disabled:b.disabled,credit_prop:creditProp,price_evidence:priceEvidence}:null};
        })(OPTS)'''.replace('OPTS',json.dumps({'trace':self.config['trace'],'operation':self.operation}))
        return self.command('eval',js)
    def open_style_panel(self):
        s=self.observe()
        if not s.get('style_available'):
            raise RuntimeError('Texture style reference is not available in the current texture tab; multi-view mode hides the style control.')
        if s.get('style_panel_open') or s.get('style_upload_available'):return s
        if s.get('uploading'):
            self.expect(lambda x:not x.get('uploading'),'texture uploads idle',timeout=60)
            s=self.observe()
        if not s.get('style_control_visible'):
            raise RuntimeError('Texture style reference control is missing; refusing to ignore --ref-image.')
        name=s.get('texture_style_name') or '无'
        self.click(self.one('button',name))
        return self.expect(lambda x:x.get('style_panel_open'),'texture style panel open',timeout=10)
    def reveal_style_upload(self):
        s=self.open_style_panel()
        if s.get('style_upload_available') and s.get('style_upload_index') is not None:return s
        if s.get('texture_style_name')=='自定义':
            # The custom preview replaces the upload tile.  The source-backed
            # clear button calls clearTextureStyle; clear it instead of
            # selecting a paid preset or writing Pinia.
            clear=self.find('.w-163 button.bg-black-60')
            if len(clear)!=1:
                raise RuntimeError('Texture style clear control is missing; refusing to replace the existing style silently.')
            self.click(clear[0])
            s=self.expect(lambda x:not x.get('reference_ready') and x.get('style_upload_available'),'texture style cleared and upload tile ready',timeout=10)
            if s.get('style_upload_available') and s.get('style_upload_index') is not None:return s
        raise RuntimeError('Texture style upload control is missing; refusing to guess a file input.')
    def upload_reference(self,path):
        s=self.reveal_style_upload();index=s.get('style_upload_index')
        if index is None:raise RuntimeError('Texture style upload control is missing.')
        result=self.command('upload','input[type="file"]','--nth',str(index),str(Path(path).resolve()))
        if not result.get('uploaded'):raise RuntimeError('Texture style reference upload was not acknowledged.')
        self.expect(lambda x:x.get('reference_ready') and not x.get('uploading') and x.get('texture_style_name')=='自定义','texture style reference uploaded and audited',timeout=60)
        self.reference_uploaded=True
        return self.observe()
    def close_style_panel(self):
        s=self.observe()
        if not s.get('style_panel_open') and not s.get('style_upload_available'):return
        try:
            self.command('keys','Escape')
            self.expect(lambda x:not x.get('style_panel_open'),'texture style panel closed',timeout=3)
            return
        except Exception:
            pass
        name=s.get('texture_style_name') or '无'
        self.click(self.one('button',name))
        self.expect(lambda x:not x.get('style_panel_open'),'texture style panel closed',timeout=5)
    def prepare_texture(self,args):
        # public-workspace.js submits style_image from workspace-texture-store
        # textureStyle, independently of the image/multiview/text base input.
        if args.prompt:
            if self.observe()['state'].get('textureTab')!='textGenerate':
                self.click(self.one('button:has([class~="i-tripo:pen"])'))
            self.expect(lambda x:x['state'].get('textureTab')=='textGenerate','texture text mode')
            entry=self.one('textarea[placeholder="描述您希望在模型上生成的贴图"]')
            result=self.command('fill',str(entry['ref']),args.prompt)
            if not result.get('verified'):raise RuntimeError('Texture prompt input rejected the requested value.')
            self.expect(lambda x:x['state'].get('texture_prompt')==args.prompt,'texture text input retained')
        s=self.observe()
        if args.ref_image and not s.get('style_available'):
            raise RuntimeError('--ref-image is not available in the current texture tab; multi-view mode hides the style control.')
        if not args.ref_image and s.get('reference_ready'):
            raise RuntimeError('A texture style reference is already applied; pass --ref-image or clear it in Studio. Refusing to submit a different priced request.')
        if args.execute_spend and s.get('uploading'):
            s=self.expect(lambda x:not x.get('uploading'),'texture uploads idle',timeout=90)
        if args.execute_spend and not s.get('base_ready'):
            raise RuntimeError('Texture base input is not ready; upload the image, views or prompt required by the current texture tab.')
        if args.ref_image and not s.get('style_control_visible') and not s.get('style_upload_available') and not s.get('style_panel_open'):
            try:
                s=self.expect(lambda x:x.get('style_control_visible') or x.get('style_upload_available'),'texture style control',timeout=5)
            except RuntimeError:
                raise RuntimeError('Texture style reference control is missing; refusing to ignore --ref-image.') from None
        self.click(self.one('button[value="'+args.texture_quality+'"]'))
        if args.ref_image:
            try:
                if args.execute_spend or args.prepare_upload:
                    self.upload_reference(args.ref_image)
                else:
                    self.open_style_panel();self.reference_skipped=True
            finally:
                self.close_style_panel()
        s=self.observe()
        if args.execute_spend:
            if not s.get('base_ready'):raise RuntimeError('Texture base input is not ready; upload the image, views or prompt required by the current texture tab.')
            if args.ref_image and not s.get('reference_ready'):raise RuntimeError('Texture style reference is not ready after upload.')
            if s.get('uploading'):raise RuntimeError('Texture inputs are still uploading; no submit.')
        return self.verify(args)
    def submit_entry(self):
        if self.config['trace']:return self.one('button[data-trace="'+self.config['trace']+'"]')
        if self.operation=='smart-uv':
            text=(self.observe().get('submit') or {}).get('text','')
            labels=[v for v in ['展开 UV','免费重试','重试'] if text.startswith(v)]
            if len(labels)!=1:raise RuntimeError('UV submit action was not identified.')
            return self.one('button',labels[0],prefix=True)
        return self.one('button',{'smart-uv':'展开 UV','fill-parts':'开始补全'}[self.operation],prefix=True)
    def prepare(self,args):
        self.expect(lambda s:s['project_ready'] and s['project_id']==args.project_id,'requested project loaded',timeout=25)
        s=self.observe()
        if s['readonly']:raise RuntimeError('Project is read-only; no postprocess operation allowed.')
        op=self.operation
        src=s.get('source_properties',{})
        if s.get('location_path')!=self.config['route']+'/'+args.project_id:
            raise RuntimeError('Studio redirected away from the requested operation; no submit.')
        if op=='texture-upscale' and src.get('is_textured') is not True:
            raise RuntimeError('Texture upscale needs a genuinely textured source; generate texture first. No submit.')
        if op=='segmentation' and (src.get('is_quad') or src.get('geometric_quad') or src.get('is_rigged')):
            raise RuntimeError('Studio segmentation does not accept quad or rigged sources. Use an eligible triangle source; no submit.')
        if op=='texture-edit' and (src.get('is_ultra_textured') or src.get('is_segmented')):
            raise RuntimeError('Studio texture editing does not accept 8K or segmented sources; no submit.')
        if args.ref_image and op!='texture':raise RuntimeError('Reference/style upload is only mapped for texture generation.')
        if args.smart_poly and op!='retopology':raise ValueError('--smart-poly is only valid for retopology.')
        if args.triangle and op!='retopology':raise ValueError('--triangle is only valid for retopology.')
        if args.upscale_extreme and op!='texture-upscale':raise ValueError('--upscale-extreme is only valid for texture-upscale.')
        if op=='smart-uv':
            current=self.wait_uv_state(lambda x:bool(x.get('primary')),'UV context and primary action loaded');label=(current.get('primary') or {}).get('text','')
            if getattr(args,'uv_retry',False):
                if not current['candidates'] or not label.startswith(('免费重试','重试')):raise RuntimeError('No successful UV candidate is available for an explicit next-candidate request.')
            elif not label.startswith('展开 UV'):
                raise RuntimeError('UV candidates already exist. Select/apply a recorded candidate, or explicitly authorize --uv-retry; never retry automatically.')
        if op=='segmentation':
            label={'coarse':'粗略','medium':'标准','fine':'精细'}[args.granularity]
            self.click(self.one('button',label,prefix=True))
        elif op=='retopology':
            topology = 'triangle' if args.triangle else 'quad'
            if s['state'].get('topology') != topology:
                self.click(self.one(f'button[value="{topology}"]'))
            current = self.expect(
                lambda view: view['state'].get('topology') == topology
                and (not args.smart_poly or view['state'].get('smartPolyDisabled') is False),
                f'{topology} topology and requested Smart Mesh controls ready', timeout=25,
            )
            if current['state'].get('smartPoly') != args.smart_poly:
                self.set_switch('P1.0', args.smart_poly, 'smartPoly')
            entry=self.one('input[type="text"]');result=self.command('fill',str(entry['ref']),str(args.face_limit))
            if not result.get('verified'):raise RuntimeError('Retopology face input rejected.')
            self.command('keys','Tab')
        elif op=='texture':return self.prepare_texture(args)
        elif op=='texture-upscale':self.click(self.one('button','8K' if args.upscale_extreme else '4K',prefix=True))
        elif op=='rigging':
            wanted=resolve_rig_version(getattr(args,'rig_version',None),args.rig_type,getattr(args,'region','cn'))
            if self.observe()['state'].get('modelVersion')!=wanted:
                self.click(self.one('button[role="combobox"]'))
                self.click(self.one('[role="option"]',wanted.split('-')[0],prefix=True))
        elif op=='texture-edit':
            if not args.prompt.strip():raise ValueError('Prompt-only texture editing needs --prompt.')
            self.click(self.one('button[data-trace="studio_cn.workspace_texture-edit.brush_mode_tab"]','生成模式'))
            r=self.command('fill',str(self.one('textarea')['ref']),args.prompt)
            if not r.get('verified'):raise RuntimeError('Texture-edit prompt did not retain its value.')
            viewer=self.wait_edit_state(lambda x:x.get('project_id')==args.project_id and x.get('apply_submit'),'loaded viewer and save control')
            if viewer['apply_submit']['disabled']:raise RuntimeError('The loaded model is not eligible for texture editing; save/preview is disabled. Generate a textured eligible source first; no submit.')
        elif op=='fill-parts':
            # This panel only exists after real segmentation; never fake a multi-part state.
            self.click(self.one('button','AI 补全'))
        return self.verify(args)
    def verify(self,args):
        op=self.operation;s=self.observe();st=s['state'];expected={}
        if s.get('location_path')!=self.config['route']+'/'+args.project_id:raise RuntimeError('Operation route changed before submit; no submit.')
        if not s['project_ready'] or s['project_id']!=args.project_id or s['readonly']:raise RuntimeError('Postprocess source project changed or is not editable.')
        if op=='texture-upscale' and s.get('source_properties',{}).get('is_textured') is not True:
            raise RuntimeError('Texture upscale needs a genuinely textured source; generate texture first. No submit.')
        if op=='segmentation':expected={'granularityLevel':{'coarse':'low','medium':'medium','fine':'high'}[args.granularity]}
        if op=='retopology':expected={'topology':'triangle' if args.triangle else 'quad','smartPoly':args.smart_poly,'faceLimit':args.face_limit}
        if op=='texture':
            expected={'textureQuality':args.texture_quality}
            if args.prompt:
                expected.update(textureTab='textGenerate',texture_prompt=args.prompt,texture_tag_count=0)
            if args.ref_image:
                if not s.get('style_available'):
                    raise RuntimeError('Texture style reference is not available in the current texture tab.')
                if args.execute_spend or args.prepare_upload:
                    if not s.get('reference_ready'):raise RuntimeError('Texture style reference is not ready.')
                    if s.get('texture_style_name')!='自定义':raise RuntimeError('Texture style reference was not set by the custom upload path.')
                    expected['textureStyleUploading']=False
                elif not s.get('style_control_visible'):
                    raise RuntimeError('Texture style reference control is missing.')
            if args.execute_spend:
                if not s.get('base_ready'):raise RuntimeError('Texture base input is not ready.')
                if s.get('uploading'):raise RuntimeError('Texture inputs are still uploading.')
        if op=='texture-upscale':expected={'upscaleTextureQuality':'extreme' if args.upscale_extreme else 'detailed'}
        if op=='rigging':expected={'modelVersion':resolve_rig_version(getattr(args,'rig_version',None),args.rig_type,getattr(args,'region','cn'))}
        if op=='texture-edit':expected={'magicBrushType':'image','prompt':args.prompt}
        if op=='fill-parts':expected={'completionMode':'ai_completion'}
        wrong={k:{'wanted':v,'actual':st.get(k)} for k,v in expected.items() if st.get(k)!=v}
        if wrong:raise RuntimeError(f'Postprocess parameter mismatch: {wrong}')
        if not s['submit']:raise RuntimeError('Operation is unavailable for the current project; missing submit control.')
        return s
