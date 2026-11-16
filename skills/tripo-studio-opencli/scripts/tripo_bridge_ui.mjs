/** Normal UI preparation and budget-gated one-shot official Bridge click.
 * No fetch, DOM writes or synthetic DOM events.
 * Page.cdp input deliberately has no DOM-click/hover fallback.
 */
import {execFileSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {isAbsolute} from 'node:path';
import {fileURLToPath} from 'node:url';

const ledgerScript=fileURLToPath(new URL('./tripo_ledger.py',import.meta.url));
const pythonBin=process.env.TRIPO_LEDGER_PYTHON || 'python';
export const VERIFIED_EXPORT_CONTRACT='CdtAW7IG:mzvvSiEk:blender-embedded';
export function classifyBridgeExportCaptures(rows,projectId){
 const requests=new Map();let preflights=0;
 for(const row of rows){
  if(!['/v2/studio/operation/export','/v2/studio/operation/export-public-model'].includes(row.endpoint))continue;
  if(row.method==='OPTIONS'){preflights++;continue;}
  if(row.method!=='POST'||!row.request_id)throw Error('Export capture method/identity missing; no inferred receipt');
  requests.set(row.request_id,row);
 }
 if(requests.size>1)throw Error('Multiple actual POST exports observed; halt');
 const receipt=[...requests.values()][0];
 if(!receipt||!receipt.capture_complete)return {status:'pending',post_requests:requests.size,preflights};
 if(receipt.endpoint!=='/v2/studio/operation/export'||receipt.http!==200||receipt.response?.code!==0)
  throw Error('Owned export response rejected; no retry');
 if(!/^[0-9a-f-]{36}$/.test(receipt.response?.operator_id??''))throw Error('Export UUID missing');
 const expected={project_id:projectId,format:'fbx',fbx_preset:'blender',texture_size:8192,texture_packaging:'embedded',with_animation:false,enable_bake_animation:false};
 if(Object.entries(expected).some(([k,v])=>receipt.request?.[k]!==v))throw Error('Export receipt parameters differ');
 return {status:'receipt',post_requests:1,preflights,receipt};
}
export async function bindExistingBridgePage(page,projectId,{pageId}={}){
 if(!/^[0-9a-f-]{36}$/.test(projectId))throw Error('Invalid project identity');
 if(typeof page.tabs!=='function'||typeof page.setActivePage!=='function')throw Error('Explicit existing-page binding is required');
 const url=`https://studio.tripo3d.com/workspace/generate/${projectId}`;
 const rows=await page.tabs();
 const matches=rows.filter(r=>r.url===url&&(pageId===undefined||r.page===pageId));
 if(matches.length!==1||typeof matches[0].page!=='string'||!matches[0].page)
  throw Error('Existing model tab missing/ambiguous; no navigation or fallback');
 page.setActivePage(matches[0].page);
 return {profile:page.contextId??null,session:page.session??null,page:matches[0].page,project_id:projectId};
}
export function ledgerCommand(ledger,...args){
 if(!pythonBin||!isAbsolute(ledger))throw Error('Explicit absolute ledger and project Python required');
 const raw=execFileSync(pythonBin,[ledgerScript,'--ledger',ledger,...args],{encoding:'utf8',windowsHide:true,maxBuffer:4*1024*1024});
 const r=JSON.parse(raw);if(r.ok!==true)throw Error('Ledger action failed; no UI submission');return r;
}
export const BRIDGE_SNAPSHOT = String.raw`(()=>{
 const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[data-state="closed"][role="dialog"],[inert],[aria-hidden="true"]');
 const text=e=>(e.innerText||'').replace(/\s+/g,' ').trim();
 const popovers=[...document.querySelectorAll('[id^="reka-popover-content"][data-state="open"]')].filter(visible);
 const drawer=popovers.find(e=>e.querySelector('input:not([type="file"])')&&text(e).startsWith('导出'));
 const top=[...document.querySelectorAll('button')].find(e=>visible(e)&&text(e)==='发送至 3D 软件');
 // Reka renders the visual delayed-open list in an unlabelled div. Its
 // role=tooltip node is a separate accessibility copy, not the clickable list.
 const elements=[...document.querySelectorAll('button,input:not([type="file"]),[role="option"],[role="menuitem"],div.cursor-pointer')].filter(visible);
 const controls=elements.map(e=>{const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);
  const inDrawer=!!drawer?.contains(e);let label='';
  if(e.getAttribute('role')==='switch'){let p=e.parentElement;for(let i=0;p&&i<3;i++,p=p.parentElement){if(text(p).includes('Blender')){label='Blender';break}}}
  return {text:text(e),tag:e.tagName,role:e.getAttribute('role'),value:e.value??null,
   state:e.getAttribute('data-state'),checked:e.getAttribute('aria-checked'),label,
   disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',inDrawer,
   opener:!inDrawer&&text(e)==='导出'&&!!e.closest('[id^="reka-popover-trigger"]'),
   menuVisual:e.matches('div.cursor-pointer')&&!!e.closest('[data-state="delayed-open"],[data-state="instant-open"]'),
   top:e===top,x,y,hit:!!h&&(e===h||e.contains(h))};});
 const b=window.useNuxtApp().$pinia._s.get('dcc-bridge-store');
 const scripts=performance.getEntriesByType('resource').map(e=>new URL(e.name).pathname);
 return {url:location.origin+location.pathname,visible:document.visibilityState==='visible',focused:document.hasFocus(),
  export_contract:scripts.includes('/_nuxt/CdtAW7IG.js')&&scripts.includes('/_nuxt/mzvvSiEk.js')?'CdtAW7IG:mzvvSiEk:blender-embedded':null,
  bridge:{dcc:b?.currentDCC??null,status:b?.status??null,connected:b?.connected===true,
   pip:!!window.documentPictureInPicture?.window&&!window.documentPictureInPicture.window.closed,busy:b?.currentTask!=null},
  drawer:!!drawer,controls};
})()`;

export function requireReady(s, projectId, {connection=true}={}) {
 if(!/^[0-9a-f-]{36}$/.test(projectId)||s.url!==`https://studio.tripo3d.com/workspace/generate/${projectId}`)
  throw Error('Wrong project/region; no UI action');
 if(!s.visible||!s.focused)throw Error('Studio window not focused; activate its native window before preparation');
 if(s.bridge.busy)throw Error('Bridge transfer already in progress; never resend');
 if(connection&&(s.bridge.dcc!=='blender'||s.bridge.status!=='OPEN'||!s.bridge.connected||!s.bridge.pip))
  throw Error('Blender Bridge/PiP not ready; no send');
}

export class BridgeUI {
 constructor(page,projectId){
  const id=page.getActivePage?.();
  if(typeof id!=='string'||!id)throw Error('BridgeUI requires an explicitly pinned existing Page');
  this.page=page;this.projectId=projectId;this.actions=[];
  this.browserContext=Object.freeze({profile:page.contextId??null,session:page.session??null,page:id});
 }
 assertPage(){if(this.page.getActivePage?.()!==this.browserContext.page)throw Error('Pinned page identity changed; no UI action');}
 async snapshot(){this.assertPage();const r=await this.page.evaluate(BRIDGE_SNAPSHOT);this.assertPage();return typeof r==='string'?JSON.parse(r):r;}
 async wait(predicate,label,timeout=5000){const end=Date.now()+timeout;do{const s=await this.snapshot();
  if(predicate(s))return s;if(Date.now()>=end)throw Error(`UI state not reached: ${label}`);
  await new Promise(r=>setTimeout(r,100));}while(true);}
 one(s,predicate,label){const a=s.controls.filter(predicate);if(a.length!==1)throw Error(`Ambiguous/missing ${label}: ${a.length}`);return a[0];}
 async point(target,type){this.assertPage();if(target.disabled||!target.hit||!Number.isFinite(target.x)||!Number.isFinite(target.y))throw Error('Control disabled/occluded; no input');
  await this.page.cdp('Input.dispatchMouseEvent',{type,x:target.x,y:target.y,...(type==='mouseMoved'?{}:{button:'left',clickCount:1})});}
 async click(predicate,label,{connection=true}={}){
  const s=await this.snapshot();requireReady(s,this.projectId,{connection});const t=this.one(s,predicate,label);
  // This helper must never submit an export or a Bridge transfer.
  if(t.text==='发送至'||t.text.startsWith('发送至 Blender')||(t.text==='导出'&&!t.opener))throw Error('Submission is forbidden in preparation');
  await this.point(t,'mouseMoved');await this.point(t,'mousePressed');await this.point(t,'mouseReleased');
  this.actions.push({action:'physical_click',label});
 }
 async ensureConnection(){
  let s=await this.snapshot();requireReady(s,this.projectId,{connection:false});
  if(s.bridge.dcc==='blender'&&s.bridge.status==='OPEN'&&s.bridge.connected&&s.bridge.pip)return;
  if(s.bridge.dcc||s.bridge.status==='CONNECTING')throw Error('Different/incomplete DCC session; do not replace it');
  await this.click(c=>c.top,'top DCC menu',{connection:false});
  await this.wait(q=>q.controls.some(c=>c.role==='switch'&&c.label==='Blender'),'Blender connection switch');
  s=await this.snapshot();const t=this.one(s,c=>c.role==='switch'&&c.label==='Blender','Blender switch');
  if(t.checked!=='false')throw Error('Connection switch state is ambiguous; no toggle');
  await this.click(c=>c.role==='switch'&&c.label==='Blender','connect Blender',{connection:false});
  await this.wait(q=>q.bridge.dcc==='blender'&&q.bridge.status==='OPEN'&&q.bridge.connected&&q.bridge.pip,'Blender handshake/PiP');
  await this.click(c=>c.top,'close DCC menu');
 }
 async prepare(){
  // OpenCLI's permitted CDP surface excludes Page.bringToFront. The caller
  // activates the known native Chrome window; requireReady verifies focus.
  await this.ensureConnection();
  let s=await this.snapshot();requireReady(s,this.projectId);
  if(!s.drawer){await this.click(c=>c.opener,'open export drawer');await this.wait(q=>q.drawer,'export drawer');}
  s=await this.snapshot();const format=this.one(s,c=>c.inDrawer&&c.role==='combobox','format');
  if(format.text!=='FBX'){
   await this.click(c=>c.inDrawer&&c.role==='combobox','format menu');
   await this.wait(q=>q.controls.some(c=>c.role==='option'&&c.text==='FBX'),'FBX option');
   await this.click(c=>c.role==='option'&&c.text==='FBX','select FBX');
   await this.wait(q=>q.controls.some(c=>c.inDrawer&&c.role==='combobox'&&c.text==='FBX'),'FBX selected');
  }
  s=await this.snapshot();let preset=this.one(s,c=>c.inDrawer&&c.text==='Blender','Blender preset');
  if(preset.state!=='on')await this.click(c=>c.inDrawer&&c.text==='Blender','select Blender preset');
  s=await this.snapshot();let size=this.one(s,c=>c.inDrawer&&c.value==='8192','8K option');
  if(size.state!=='on')await this.click(c=>c.inDrawer&&c.value==='8192','select 8K');
  s=await this.snapshot();requireReady(s,this.projectId);
  if(s.export_contract!==VERIFIED_EXPORT_CONTRACT)throw Error('Unverified export caller build; no send');
  if(!s.controls.some(c=>c.inDrawer&&c.role==='combobox'&&c.text==='FBX')||
     !s.controls.some(c=>c.inDrawer&&c.text==='Blender'&&c.state==='on')||
     !s.controls.some(c=>c.inDrawer&&c.value==='8192'&&c.state==='on'))throw Error('Export parameters changed; no send');
  const target=this.one(s,c=>c.inDrawer&&c.text==='发送至','send-menu trigger');
  await this.point(target,'mouseMoved'); // Real CDP pointer, no synthetic-event fallback.
  this.actions.push({action:'physical_hover',label:'send-menu trigger'});
  // The tooltip is delayed-open. Input returning is not a visibility receipt.
  s=await this.wait(q=>q.controls.filter(c=>c.menuVisual&&c.text==='发送至 Blender').length===1,'visible Blender send item');
  requireReady(s,this.projectId);
  const send=this.one(s,c=>c.menuVisual&&c.text==='发送至 Blender','Blender send item');
  if(send.disabled||!send.hit)throw Error('Blender send item not actionable');
  return {submitted:false,menu_ready:true,menu_target:{text:send.text,x:send.x,y:send.y},format:'fbx',fbx_preset:'blender',texture_size:8192,
   texture_packaging:'embedded',export_contract:VERIFIED_EXPORT_CONTRACT,
   packaging_basis:'Verified loaded DCC-bridge caller overrides params with target descriptor',actions:this.actions};
 }
 async sendOnce({ledger,taskId,stepKey}){
  const plan={project_id:this.projectId,model_format:'fbx',fbx_preset:'blender',texture_size:8192,texture_packaging:'embedded'};
  const task=ledgerCommand(ledger,'status','--task-id',taskId),step=task.steps?.[stepKey];
  const stored=step?.metadata?.bridge_plan,source=step?.metadata?.bridge_source;
  if(task.schema_version<2||task.region!=='cn'||task.halted||step?.status!=='reserved'||
     step.operation!=='export'||step.credits!==0||step.downloads!==1||step.was_submitted||
     !stored||Object.entries(plan).some(([k,v])=>stored[k]!==v))throw Error('No matching unused export reservation; no click');
  const checked=Date.parse(source?.checked_at??'');
  if(source?.project_id!==this.projectId||source?.owner_verified!==true||source?.net_credits!==0||
     !Number.isFinite(checked)||Date.now()-checked>300000||checked>Date.now()+1000)
   throw Error('Fresh owned-source/zero-credit preflight required; no click');
  // Prepare is deliberately separate: it is never implicitly repeated here.
  const checkedTarget=s=>{requireReady(s,this.projectId);
   if(s.export_contract!==VERIFIED_EXPORT_CONTRACT)throw Error('Unverified export caller build; no send');
   const t=this.one(s,c=>c.menuVisual&&c.tag==='DIV'&&!c.inDrawer&&c.text==='发送至 Blender','Blender send item');
   if(!s.drawer||!s.controls.some(c=>c.inDrawer&&c.role==='combobox'&&c.text==='FBX')||
     !s.controls.some(c=>c.inDrawer&&c.text==='Blender'&&c.state==='on')||
     !s.controls.some(c=>c.inDrawer&&c.value==='8192'&&c.state==='on')||!t.hit||t.disabled)
    throw Error('Send menu/parameters changed; no click');return t;};
  let s=await this.snapshot();const t=checkedTarget(s);
  const commit=(status,evidence)=>ledgerCommand(ledger,'commit','--task-id',taskId,'--step-key',stepKey,
   '--status',status,'--evidence-json',JSON.stringify({...evidence,browser_context:this.browserContext}));
  // Atomic ledger transition claims this attempt before changing capture.
  commit('submitting',{bridge_plan:plan,browser_context:this.browserContext,ui_target:t.text,capture_required:true});
  let inputAttempted=false;
  try{
   if(await this.page.startNetworkCapture()!==true)throw Error('Capture not armed; no click');
   s=await this.snapshot();const fresh=checkedTarget(s);
   await this.point(fresh,'mouseMoved');
   const pointed=checkedTarget(await this.snapshot());
   if(Math.abs(pointed.x-fresh.x)>1||Math.abs(pointed.y-fresh.y)>1)throw Error('Send target moved after pointer arrival; no press');
   inputAttempted=true;await this.point(pointed,'mousePressed');await this.point(pointed,'mouseReleased');
  }catch(e){commit('unknown',{reason:'UI submission outcome uncertain; no retry',input_attempted:inputAttempted,bridge_plan:plan});throw e;}
  return {clicked_once:true,status:'submitting',browser_context:this.browserContext,cloud_success:false,requires_same_attempt_receipt_and_receiver:true,
   note:'Read capture with retainIncomplete:true; persist the matching export UUID, then commit submitted. Do not mark success until receiver/file verification.'};
 }
}
