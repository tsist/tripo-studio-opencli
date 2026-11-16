// Real isolated ledger + fake browser: never operates a live page or cloud.
import os from 'node:os';import assert from 'node:assert/strict';import fs from 'node:fs';import path from 'node:path';import {fileURLToPath} from 'node:url';
import {BridgeUI,requireReady,ledgerCommand,VERIFIED_EXPORT_CONTRACT,bindExistingBridgePage} from '../skills/tripo-studio-opencli/scripts/tripo_bridge_ui.mjs';
const root=fs.mkdtempSync(path.join(os.tmpdir(),'tripo-bridge-test-'));
process.on('exit',()=>fs.rmSync(root,{recursive:true,force:true}));const id='00000001-0000-4000-8000-000000000001';
const plan={project_id:id,model_format:'fbx',fbx_preset:'blender',texture_size:8192,texture_packaging:'embedded'};
const control=(text,extra={})=>({text,hit:true,disabled:false,x:20,y:30,...extra});
const state=()=>({url:`https://studio.tripo3d.com/workspace/generate/${id}`,visible:true,focused:true,drawer:true,export_contract:VERIFIED_EXPORT_CONTRACT,
 bridge:{dcc:'blender',status:'OPEN',connected:true,pip:true,busy:false},controls:[
 control('FBX',{inDrawer:true,role:'combobox'}),control('Blender',{inDrawer:true,state:'on'}),
 control('8k',{inDrawer:true,value:'8192',state:'on'}),control('发送至',{inDrawer:true}),control('发送至 Blender',{menuVisual:true,tag:'DIV',inDrawer:false})]});
let tests=0;
function setup(name,{source=true,downloads=1}={}){
 const ledger=path.join(root,name+'.json'),taskId='test-'+name,stepKey='send';
 ledgerCommand(ledger,'init-task','--task-id',taskId,'--budget-cap','0','--download-cap','1','--max-submissions','1','--region','cn');
 const metadata={bridge_plan:plan,...(source?{bridge_source:{project_id:id,owner_verified:true,net_credits:0,checked_at:new Date().toISOString()}}:{})};
 ledgerCommand(ledger,'reserve','--task-id',taskId,'--step-key',stepKey,'--operation','export','--credits','0','--downloads',String(downloads),'--metadata-json',JSON.stringify(metadata));
 return {ledger,taskId,stepKey};
}
class FakePage{
 constructor(s=state()){this.s=s;this.events=[];this.capture=0;this.reads=0;this.activePage='observed-model-page';this.contextId='test-profile';this.session='test-session';}
 getActivePage(){return this.activePage;}
 setActivePage(p){this.activePage=p;}
 async tabs(){return this.tabRows??[{page:'observed-model-page',url:this.s.url}];}
 async evaluate(){this.reads++;return structuredClone(this.s);}
 async startNetworkCapture(){this.capture++;return this.captureResult??true;}
 async cdp(method,p){this.events.push({method,...p});if(this.onInput)this.onInput(method,p);}
}
function status(a){return ledgerCommand(a.ledger,'status','--task-id',a.taskId);}
{
 const s=state();s.bridge.pip=false;assert.throws(()=>requireReady(s,id));tests++;
 const q=state();q.url=q.url.replace('.com','.ai');assert.throws(()=>requireReady(q,id));tests++;
}
{
 const p=new FakePage(),ui=new BridgeUI(p,id);p.s.controls=p.s.controls.filter(c=>c.text!=='发送至 Blender');
 ui.wait=async(predicate)=>{const s=await ui.snapshot();assert.equal(predicate(s),false);throw Error('menu absent');};
 await assert.rejects(()=>ui.prepare(),/menu absent/);assert.equal(p.events.length,1);assert.equal(p.events[0].type,'mouseMoved');tests++;
}
{
 const a=setup('once'),p=new FakePage(),ui=new BridgeUI(p,id);
 p.onInput=(_m,q)=>{if(q.type==='mousePressed')assert.equal(status(a).steps.send.status,'submitting');};
 const r=await ui.sendOnce(a);assert.equal(r.cloud_success,false);assert.equal(status(a).steps.send.status,'submitting');
 assert.equal(status(a).steps.send.evidence.browser_context.page,'observed-model-page');
 assert.equal(p.events.filter(e=>e.type==='mousePressed').length,1);
 await assert.rejects(()=>ui.sendOnce(a),/unused export reservation/);assert.equal(p.capture,1);assert.equal(p.events.length,3);tests++;
}
{
 const a=setup('capture-fail'),p=new FakePage(),ui=new BridgeUI(p,id);p.captureResult=false;
 await assert.rejects(()=>ui.sendOnce(a),/Capture not armed/);assert.equal(p.events.length,0);assert.equal(status(a).steps.send.status,'unknown');assert.equal(status(a).halted,true);tests++;
}
{
 const a=setup('input-unknown'),p=new FakePage(),ui=new BridgeUI(p,id);p.onInput=(_m,q)=>{if(q.type==='mousePressed')throw Error('input error');};
 await assert.rejects(()=>ui.sendOnce(a),/input error/);assert.equal(status(a).steps.send.status,'unknown');
 const count=p.events.length;await assert.rejects(()=>ui.sendOnce(a));assert.equal(p.events.length,count);tests++;
}
{
 const a=setup('no-preflight',{source:false}),p=new FakePage(),ui=new BridgeUI(p,id);
 await assert.rejects(()=>ui.sendOnce(a),/preflight required/);assert.equal(p.capture,0);assert.equal(p.events.length,0);tests++;
}
{
 const a=setup('no-download',{downloads:0}),p=new FakePage(),ui=new BridgeUI(p,id);
 await assert.rejects(()=>ui.sendOnce(a),/unused export reservation/);assert.equal(p.events.length,0);tests++;
}
{
 const a=setup('changed-build'),p=new FakePage(),ui=new BridgeUI(p,id);p.s.export_contract=null;
 await assert.rejects(()=>ui.sendOnce(a),/Unverified export caller build/);assert.equal(p.capture,0);assert.equal(p.events.length,0);tests++;
}
{
 const a=setup('changed-parameters'),p=new FakePage(),ui=new BridgeUI(p,id);
 p.evaluate=async()=>{p.reads++;const s=state();if(p.reads>1)s.controls[0].text='GLB';return s;};
 await assert.rejects(()=>ui.sendOnce(a),/parameters changed/);assert.equal(p.events.length,0);assert.equal(status(a).steps.send.status,'unknown');tests++;
}

{
 const p=new FakePage();p.activePage=undefined;
 assert.throws(()=>new BridgeUI(p,id),/explicitly pinned/);assert.equal(p.reads,0);tests++;
}
{
 const p=new FakePage();p.activePage=undefined;
 const context=await bindExistingBridgePage(p,id);assert.equal(context.page,'observed-model-page');
 assert.equal(p.events.length,0);assert.equal(p.reads,0);tests++;
}
{
 const p=new FakePage();p.activePage=undefined;p.tabRows=[{page:'a',url:state().url},{page:'b',url:state().url}];
 await assert.rejects(()=>bindExistingBridgePage(p,id),/ambiguous/);assert.equal(p.activePage,undefined);tests++;
}
{
 const p=new FakePage();await assert.rejects(()=>bindExistingBridgePage(p,id,{pageId:'missing'}),/missing/);
 assert.equal(p.events.length,0);tests++;
}
{
 const p=new FakePage(),ui=new BridgeUI(p,id);p.setActivePage('other-page');
 await assert.rejects(()=>ui.snapshot(),/identity changed/);assert.equal(p.reads,0);tests++;
}
{
 const p=new FakePage(),ui=new BridgeUI(p,id);
 p.evaluate=async()=>{p.setActivePage('other-page');return state();};
 await assert.rejects(()=>ui.snapshot(),/identity changed/);assert.equal(p.events.length,0);tests++;
}

fs.writeFileSync(path.join(root,'summary.json'),JSON.stringify({ok:true,tests,scope:'real isolated ledger + fake browser; no live send'},null,2),{flag:'wx'});
process.stdout.write(JSON.stringify({ok:true,tests,scope:'real isolated ledger + fake browser; no live send'})+'\n');
