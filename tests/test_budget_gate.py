"""Offline behavior checks: unpriced texture save must stop before side effects."""
import sys,types,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skills/tripo-studio-opencli/scripts'))
import tripo_preview as preview
import tripo_actions as actions
from tripo_pricing import CreditEstimate

def args(**overrides):
 values=dict(operation='texture-edit',project_id='00000001-0000-4000-8000-000000000001',
  task_id='offline-test',step_key='apply',apply_preview_step='preview',ledger='not-used.json',
  profile='offline',region='cn',prompt='',ref_image='',brush_mode=False,prepare_upload=False,
  smart_poly=False,triangle=False,upscale_extreme=False,rig_type='biped',face_limit=10000,
  granularity='medium',texture_quality='standard',uv_retry=False,animation='',retarget_credit_cap=None,
  unpriced_apply_cap=5,prepare_only=False,execute_spend=True)
 values.update(overrides);return types.SimpleNamespace(**values)

class NoSideEffects:
 USE_RESOURCE_BARRIER=True
 POSTPROCESS_CONFIG={'texture-edit':{}}
 def __init__(self):self.events=[]
 def _ensure_daemon(self,*a):self.events.append('daemon');raise AssertionError('Unexpected browser/daemon action')
 def _ledger(self,*a):self.events.append('ledger');raise AssertionError('Unexpected ledger action')
 def _fetch_preflight(self,*a):self.events.append('preflight');raise AssertionError('Unexpected cloud action')
 def _extract_json(self,x):return x
 def _parse_submit_credits(self,x):return None

class BudgetGate(unittest.TestCase):
 def test_execute_save_rejected_for_every_user_cap(self):
  for cap in (None,0,5,10,875,100000):
   with self.subTest(cap=cap):
    d=NoSideEffects()
    with patch.object(preview,'recorded_preview',return_value={'fingerprint':'a'*64}):
     with self.assertRaisesRegex(RuntimeError,'[Nn]o verified|[Uu]nverified'):
      preview.apply_texture_preview(d,args(unpriced_apply_cap=cap))
    self.assertEqual(d.events,[])

 def test_direct_submission_cannot_bypass_missing_price_contract(self):
  for evidence in ({},{'price_evidence':{'kind':'unpriced_explicit_cap'}},
                   {'price_evidence':{'kind':'user_supplied_quote','quoted_credits':0}}):
   with self.subTest(evidence=evidence):
    d=NoSideEffects();a=args(_submission_metadata=evidence)
    estimate=CreditEstimate('postprocess_texture_edit_apply',5,0,5,None,['internal reservation only'])
    call=lambda *unused:(_ for _ in ()).throw(AssertionError('Unexpected browser call'))
    with self.assertRaisesRegex(RuntimeError,'[Nn]o verified|[Uu]nverified'):
     actions.execute_ui(d,a,call,estimate,{},'1','/v2/studio/operation/apply_retexture',1)
    self.assertEqual(d.events,[])

 def test_dry_plan_does_not_promise_execution_from_cap(self):
  d=NoSideEffects()
  with patch.object(preview,'recorded_preview',return_value={'fingerprint':'a'*64}):
   result=preview.apply_texture_preview(d,args(execute_spend=False))
  self.assertTrue(result['dry_run']);self.assertFalse(result['ready'])
  self.assertTrue(result['execution_blocked']);self.assertTrue(result['requires_verified_price_contract'])
  self.assertIsNone(result['quoted_credits']);self.assertEqual(d.events,[])

 def test_prepare_preserves_candidate_without_claiming_spend_ready(self):
  d=NoSideEffects();d._ensure_daemon=lambda *a:None
  d._fetch_preflight=lambda *a:{'balance':{'credit_balance':100}}
  class FakeUI:
   actions=[]
   def __init__(self,*a):pass
   def prepare_edit_apply(self,*a):return {'apply_submit':{'text':'保存','disabled':False}}
  a=args(execute_spend=False,prepare_only=True)
  with patch.object(preview,'recorded_preview',return_value={'fingerprint':'a'*64}),\
       patch.object(preview,'attach_preview_page',return_value=('offline',lambda *a:None)),\
       patch.object(preview,'PostUI',FakeUI):
   a._keep_browser=True
   result=preview.apply_texture_preview(d,a)
  self.assertTrue(result['ui_ready']);self.assertFalse(result['ready']);self.assertFalse(result['submitted'])
  self.assertTrue(result['execution_blocked']);self.assertEqual(d.events,[])

 def test_invalid_caps_remain_invalid(self):
  for cap in (-1,True,1.5):
   with self.subTest(cap=cap):
    with self.assertRaises(ValueError):preview.apply_texture_preview(NoSideEffects(),args(unpriced_apply_cap=cap))

 def test_wallet_overage_retains_precise_unattributed_evidence(self):
  for operation in ('smart-uv','rigging','texture'):
   with self.subTest(operation=operation):
    d=NoSideEffects();d.OperatorFailed=type('OperatorFailed',(RuntimeError,),{})
    d._poll_operator=lambda *a:{'status':'success'}
    d._fetch_preflight=lambda *a:{'balance':{'credit_balance':90}}
    a=args(operation=operation,_before_preflight={'balance':{'credit_balance':100}},
           _estimated_credits=5,timeout_sec=1,_browser_context={'page':'existing-page'})
    commits=[]
    with patch.object(actions,'commit',side_effect=lambda *v,**kw:commits.append((v[2],v[3]))):
     with self.assertRaises(RuntimeError):actions.finish_operators(d,a,{'operator_ids':['existing-operator']})
    self.assertEqual(len(commits),1);status,evidence=commits[0]
    self.assertEqual(status,'unknown');self.assertEqual(evidence['reason'],'wallet_decrease_exceeds_reserved_credits')
    self.assertEqual((evidence['wallet_before'],evidence['wallet_after'],evidence['observed_wallet_decrease'],evidence['reserved_credits']),(100,90,10,5))
    self.assertEqual(evidence['actual_credit_debit'],'not_attributed');self.assertTrue(a._keep_browser)

 def test_wallet_net_change_is_not_reported_as_attributed_cost(self):
  for after in (96,95,105):
   with self.subTest(after=after):
    d=NoSideEffects();d.OperatorFailed=type('OperatorFailed',(RuntimeError,),{})
    d._poll_operator=lambda *a:{'status':'success'}
    d._fetch_preflight=lambda *a:{'balance':{'credit_balance':after}}
    a=args(_before_preflight={'balance':{'credit_balance':100}},_estimated_credits=5,timeout_sec=1)
    commits=[]
    with patch.object(actions,'commit',side_effect=lambda *v,**kw:commits.append((v[2],v[3]))):
     result=actions.finish_operators(d,a,{'operator_ids':['existing-operator']})
    self.assertEqual(commits[0][0],'success');self.assertIsNone(result['credits_used'])
    self.assertEqual(result['actual_credit_debit'],'not_attributed')

 def test_uncertain_readback_is_distinct_from_observed_overage(self):
  d=NoSideEffects();d.OperatorFailed=type('OperatorFailed',(RuntimeError,),{})
  d._poll_operator=lambda *a:{'status':'success'}
  d._fetch_preflight=lambda *a:(_ for _ in ()).throw(RuntimeError('readback unavailable'))
  a=args(_before_preflight={'balance':{'credit_balance':100}},_estimated_credits=5,timeout_sec=1)
  commits=[]
  with patch.object(actions,'commit',side_effect=lambda *v,**kw:commits.append((v[2],v[3]))):
   with self.assertRaises(RuntimeError):actions.finish_operators(d,a,{'operator_ids':['existing-operator']})
  self.assertEqual(commits[0][1]['reason'],'poll/readback uncertain; no retry')
  self.assertNotIn('observed_wallet_decrease',commits[0][1]);self.assertTrue(a._keep_browser)

if __name__=='__main__':unittest.main(verbosity=2)
