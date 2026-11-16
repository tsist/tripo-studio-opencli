import sys,json,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skills/tripo-studio-opencli/scripts'))
import tripo_studio_driver as d
import tripo_preview as p
from test_uv_current import UVApply,PROJECT,SOURCE,CANDIDATE,PF

class Animation(UVApply):
    # Reuse isolated ledger/transport fixtures; only these animation tests are
    # loaded by the suite below, not the inherited UV cases.
    def setUp(self):
        super().setUp()
        self.args=d.build_parser().parse_args(['--profile','offline','--ledger',str(self.ledger),'postprocess','--operation','rigging','--project-id',PROJECT,'--task-id','uvtest','--step-key','animation','--animation','Walking','--retarget-credit-cap','0','--execute-spend'])
        self.cached=False;self.quoted=False;self.no_readback=False;self.ui_clicks=0
    def transport(self,profile,*parts,**kw):
        op=parts[2];self.calls.append(op)
        if op=='wait':return json.dumps({'error':{'code':'xhr_not_seen'}})
        if op=='eval':return json.dumps({'complete':True,'count':1})
        if op=='open':d._BROWSER_PAGES[(profile,parts[1])]='owned-animation-test'
        if op=='click':self.clicks+=1;return json.dumps({'clicked':True,'matches_n':1})
        if op=='network':
            endpoint='/v2/studio/operation/retarget_model';url='https://api-cn.tripo3d.com'+endpoint
            if '--detail' in parts:return json.dumps({'key':'animation-new','url':url,'method':'POST','status':200,'body':{'code':0,'data':{'project_id':PROJECT,'operator_id':CANDIDATE}}})
            self.networks+=1
            return json.dumps({'captured_at':'2026-11-15T12:00:01Z' if self.networks>1 else '2026-11-15T12:00:00Z','entries':[] if self.networks==1 else [{'key':'animation-new','url':url,'method':'POST','status':200,'timestamp':'2026-11-15T12:00:01Z'}]})
        return '{}'
    def ui_class(self):
        parent=self
        class UI:
            actions=[]
            def __init__(self,*args):self.waits=0
            def command(self,*args):return {}
            def animation_entry(self,*args):return {'ref':'4'}
            def click(self,*args):parent.ui_clicks+=1
            def animation_state(self):
                return {'project_id':PROJECT,'source_operator_id':SOURCE,'readonly':False,'rig_type':'biped','cards':[{'label':'Walking','text':'Walking 5' if parent.quoted else 'Walking','cached':parent.cached,'disabled':False,'selected':False}],'retarget_ops':[]}
            def wait_animation_state(self,predicate,*args,**kwargs):
                self.waits+=1
                value=self.animation_state()
                if self.waits>1:
                    if parent.no_readback:raise OSError('retarget output unavailable')
                    value['cards'][0].update(cached=True,selected=True)
                    value['retarget_ops']=[{'operator_id':CANDIDATE,'name':'walk','has_model':True}]
                if not predicate(value):raise RuntimeError('unexpected animation predicate')
                return value
        return UI
    def run_animation(self):
        with patch.object(d,'_ensure_daemon'),patch.object(d,'_fetch_preflight',return_value=PF),patch.object(d,'_opencli',side_effect=self.transport),patch.object(d,'_poll_operator',return_value={'status':'success','operator_id':CANDIDATE}),patch.object(p,'PostUI',self.ui_class()):return p.retarget_animation(d,self.args)
    def test_animation_success_verifies_project_output_and_selection(self):
        r=self.run_animation();self.assertEqual(r['result_kind'],'animation_ready');self.assertEqual(r['animation_name'],'walk');self.assertEqual(self.clicks,1)
    def test_cached_animation_selects_without_service_operation(self):
        self.cached=True;r=self.run_animation();self.assertFalse(r['submitted']);self.assertEqual(self.clicks,0);self.assertEqual(self.ui_clicks,1)
        self.assertNotIn('animation',self.state()['steps'])
    def test_unpriced_card_needs_explicit_cap_before_ui(self):
        self.args.retarget_credit_cap=None
        with self.assertRaises(RuntimeError):self.run_animation()
        self.assertEqual(self.calls,[])
    def test_unexpected_card_price_is_rejected_before_click(self):
        self.quoted=True
        with self.assertRaises(RuntimeError):self.run_animation()
        self.assertEqual(self.clicks,0)
    def test_retarget_readback_failure_halts_and_preserves_page(self):
        self.no_readback=True
        with self.assertRaises(OSError):self.run_animation()
        self.assertEqual(self.state()['steps']['animation']['status'],'unknown');self.assertNotIn('close',self.calls)
    def test_prepare_is_not_a_server_submission(self):
        self.args.execute_spend=False;self.args.prepare_only=True
        r=self.run_animation();self.assertTrue(r['prepared']);self.assertFalse(r['submitted']);self.assertEqual(self.clicks,0)

def load_tests(loader,tests,pattern):
    names=[n for n in Animation.__dict__ if n.startswith('test_')]
    return unittest.TestSuite(Animation(n) for n in names)

if __name__=='__main__':unittest.main()
