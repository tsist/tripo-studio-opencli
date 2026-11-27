"""Existing Studio commands: verified form preparation and guarded execution."""
from __future__ import annotations
import json,re,time,uuid
from pathlib import Path
from typing import Any
from tripo_pricing import (
    estimate_generate,
    estimate_postprocess,
    is_h35_version,
    resolve_hd_version,
    resolve_lp_version,
)
from tripo_ui import StudioUI

REFERENCE_IMAGE_SUFFIXES={'.jpg','.jpeg','.png','.webp'}
REFERENCE_IMAGE_MAX_BYTES=20*1024*1024
DEFERRED_POSTPROCESS = {'segmentation', 'fill-parts', 'retopology', 'texture', 'texture-pbr', 'texture-upscale'}
ACTIVE_STEP_STATUSES = {'reserved', 'submitting', 'submitted', 'unknown'}

def validate_reference_image(path):
    """Validate the local style reference before any UI/network action.

    public-workspace.js accepts JPG/PNG/WEBP up to 20 MB for the texture-style
    uploader.  Keep the same local constraints and never echo file metadata.
    """
    p=Path(path).expanduser().resolve()
    if not p.is_file():raise FileNotFoundError('Reference/style image is missing.')
    if p.suffix.lower() not in REFERENCE_IMAGE_SUFFIXES:raise ValueError('Reference/style image must be JPG, PNG, or WEBP.')
    if p.stat().st_size>REFERENCE_IMAGE_MAX_BYTES:raise ValueError('Reference/style image exceeds the 20 MB Studio limit.')
    return p


def texture_ready(observed,reference_required,reference_uploaded):
    """A texture request is ready only when its real base/style inputs are set.

    A style that merely exists in the page is not proof that the requested
    --ref-image was uploaded.  For a reference request, readiness therefore
    requires this invocation's upload acknowledgement plus audited style state.
    """
    submit=observed.get('submit') or {}
    return bool(
        not observed.get('uploading')
        and observed.get('base_ready') is True
        and submit
        and submit.get('disabled') is False
        and (not reference_required or (reference_uploaded and observed.get('reference_ready') is True))
    )


def require_identity(args):
    if not args.task_id or not args.step_key:raise ValueError('--task-id and --step-key are required for execution.')
    for name,value in [('task-id',args.task_id),('step-key',args.step_key)]:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value) or value in {'.','..'}:
            raise ValueError(f'Unsafe {name}.')

def validate_generate(args,require_inputs=True):
    if args.mode=='single' and (len(args.images)!=1 if require_inputs else len(args.images)>1):raise ValueError('Single mode requires exactly one image.')
    if args.mode=='batch' and require_inputs and not 1<=len(args.images)<=20:raise ValueError('Batch mode requires 1..20 images.')
    if args.mode=='multiview' and require_inputs and not any([args.front,args.left,args.right,args.back]):raise ValueError('Multiview requires at least one named view.')
    if args.mode=='multiview' and require_inputs and (not args.front or sum(bool(x) for x in [args.front,args.left,args.right,args.back])<2):raise ValueError('Multiview requires the front image and at least one other view.')
    if args.mode=='text' and not args.prompt.strip():raise ValueError('Text mode requires a nonempty prompt.')
    if args.t_pose and args.mode!='text':raise ValueError('T-pose is only available for text mode.')
    if args.face_limit<0:raise ValueError('Face limit must be nonnegative.')
    if args.tier=='smartmesh' and (args.generate_parts or args.smart_poly or args.pbr or args.no_texture):
        raise ValueError('These HD options have no mapped Smart Mesh controls; do not silently ignore them.')
    if args.tier=='hd' and is_h35_version(args.hd_version) and args.quad:
        raise ValueError('H3.5 Preview (v3.5-20261108) only supports triangle topology; use --tier smartmesh --quad or --hd-version v3.1-20260211 for quad.')
    if args.tier=='hd' and args.generate_parts:
        # public-workspace.js forces triangle topology and turns texture off when
        # 分部件生成 is enabled.  Reject conflicting explicit requests rather
        # than silently changing them.
        if args.quad:raise ValueError('HD generate-parts only supports triangle topology; the Studio UI auto-switches quad to triangle. Refusing to change --quad silently.')
        if not args.no_texture:raise ValueError('HD generate-parts requires --no-texture; the Studio UI automatically disables texture for parts generation.')
        if 0<args.face_limit<10000:raise ValueError('HD generate-parts requires a face limit of at least 10000.')
    if args.tier=='hd' and args.smart_poly:
        # The same source disables Smart Poly when detailed geometry is chosen;
        # smart-poly face ceilings are 10k (quad) / 20k (triangle).
        if args.geometry_quality=='detailed':raise ValueError('HD smart-poly is incompatible with --geometry-quality detailed; the Studio UI auto-disables Smart Poly.')
        ceiling=10000 if args.quad else 20000
        if args.face_limit>ceiling:raise ValueError(f'HD smart-poly face limit is capped at {ceiling} for {"quad" if args.quad else "triangle"} topology.')
    if args.no_texture and args.pbr:raise ValueError('PBR requires texture generation.')
    if args.mode not in {'single','batch'} and args.images:raise ValueError('--images is only valid in single/batch mode.')
    if args.mode!='multiview' and any([args.front,args.left,args.right,args.back]):raise ValueError('Named views require multiview mode.')
    for p in [*args.images,args.front,args.left,args.right,args.back]:
        if p and not Path(p).is_file():raise FileNotFoundError(f'Input image missing: {p}')

def exact_price(text,expected,parse,credit_prop=None):
    observed=parse(text)
    if observed is None and type(credit_prop) is int and credit_prop>=0:observed=credit_prop
    if observed is not None and credit_prop is not None and observed!=credit_prop:raise RuntimeError('Visible price and live button credit property differ.')
    if observed is None or observed!=expected:
        raise RuntimeError(f'Live price is unknown or differs: observed={observed}, estimate={expected}. No submit.')
    return observed

def reserve(d,args,estimate,metadata,downloads=0):
    require_identity(args)
    state=d._ledger(Path(args.ledger),'status','--task-id',args.task_id)
    if state.get('schema_version',1)<2:raise RuntimeError('Execution requires a new schema-v2 task with explicit resource caps; old task history is retained.')
    if state.get('region')!=args.region:raise RuntimeError('Task ledger region differs from the requested Studio region.')
    metadata = {**metadata, 'studio_profile': args.profile}
    cmd=['reserve','--task-id',args.task_id,'--step-key',args.step_key,'--operation',estimate.action,'--credits',str(estimate.net_credits),'--downloads',str(downloads),'--metadata-json',json.dumps(metadata,ensure_ascii=False)]
    if estimate.trial_used:cmd+=['--trial',estimate.trial_used,'--trial-count','1']
    return d._ledger(Path(args.ledger),*cmd)

def commit(d,args,status,evidence=None,**extra):
    cmd=['commit','--task-id',args.task_id,'--step-key',args.step_key,'--status',status]
    if evidence is not None:cmd+=['--evidence-json',json.dumps(evidence,ensure_ascii=False)]
    for k,v in extra.items():
        if v is not None:cmd+=['--'+k.replace('_','-'),str(v)]
    return d._ledger(Path(args.ledger),*cmd)

def require_spend_contract(action):
    if action=='postprocess_texture_edit_apply':
        raise RuntimeError('Unverified texture-preview save charge: no verified price or service-side upper bound. Automatic save is blocked; --unpriced-apply-cap is only an internal reservation and cannot unlock it. Keep the recorded preview; no submit.')


def deferred_wallet(steps, profile, balance):
    """Compare this production batch's wallet exposure without billing individual steps."""
    active_history = list(steps.values())
    first = next((i for i, step in enumerate(active_history)
                  if step.get('was_submitted') and (step.get('metadata') or {}).get('deferred_profile')), None)
    if first is None:
        return None
    history = [step for step in active_history[first:] if step.get('was_submitted')]
    for step in history:
        metadata = step.get('metadata') or {}
        if any(metadata.get(key, profile) != profile for key in ('deferred_profile', 'studio_profile', 'export_profile')):
            raise RuntimeError('Steps in the same batch must use the original browser profile.')
    baseline = history[0]['metadata'].get('preflight', {}).get('balance', {}).get('credit_balance')
    if type(baseline) is not int or type(balance) is not int or min(baseline, balance) < 0:
        raise RuntimeError('Cannot compare the recorded and current batch wallet balances.')
    approved = sum(step['credits'] for step in history)
    return {'wallet_before': baseline, 'wallet_now': balance,
            'observed_net_decrease': baseline - balance, 'submitted_credit_ceiling': approved,
            'actual_credit_debit': 'not_attributed'}


def halt_batch_wallet(d, args, wallet, progress):
    reason = 'Batch wallet decrease exceeds its recorded submissions.'
    evidence = {'wallet': wallet, 'progress': progress}
    d._ledger(Path(args.ledger), 'halt', '--task-id', args.task_id, '--reason', reason,
              '--evidence-json', json.dumps(evidence, ensure_ascii=False))


def check_queued_submission(d, args, balance):
    state = d._ledger(Path(args.ledger), 'status', '--task-id', args.task_id)
    if state.get('region') != args.region or state.get('schema_version', 1) < 2:
        raise RuntimeError('Execution requires a schema-v2 task in the requested Studio region.')
    bound_profile = state.get('studio_profile')
    if (bound_profile and bound_profile != args.profile) or (getattr(args, 'submit_only', False) and not bound_profile):
        raise RuntimeError('Use the Studio profile bound at init-task for this batch.')
    if state.get('halted') or state.get('submitting') or state.get('unresolved_unknown'):
        raise RuntimeError('Inspect the halted, submitting, or unknown ledger step before a new submission.')
    steps = state['steps']
    if getattr(args, 'step_key', None) in steps:
        raise RuntimeError('This step key is already recorded. Inspect the existing receipt before proceeding.')
    for step in steps.values():
        metadata = step.get('metadata') or {}
        evidence = step.get('evidence') or {}
        if step.get('status') not in ACTIVE_STEP_STATUSES:
            continue
        project = getattr(args, 'project_id', None)
        export_params = metadata.get('export_params') or {}
        if project and (project in (evidence.get('project_ids') or []) or
                        project in {step.get('project_id'), metadata.get('project_id'),
                                    export_params.get('project_id')}):
            raise RuntimeError('The requested project already has an unsettled operation in this batch.')
    summary = deferred_wallet(steps, args.profile, balance)
    if summary and summary['observed_net_decrease'] > summary['submitted_credit_ceiling']:
        halt_batch_wallet(d, args, summary, {})
        raise RuntimeError('Batch wallet decrease exceeds its recorded submissions. Reconcile the batch before spending.')
    return summary


def execute_ui(d,args,call,estimate,observed,submit_ref,endpoint,count,browser_context=None):
    require_spend_contract(estimate.action)
    from tripo_submit import SingleSubmission
    preflight = getattr(args, '_before_preflight', None) or {}
    args._batch_wallet = check_queued_submission(d, args, preflight.get('balance', {}).get('credit_balance'))
    barrier=getattr(d,'USE_RESOURCE_BARRIER',False)
    if barrier:
        from tripo_submit import arm_capture
        arm_capture(call,d._extract_json)
    metadata={'live_price':estimate.net_credits,'parameters':observed.get('state',{}),'expected_results':count,'preflight':getattr(args,'_before_preflight',None),**getattr(args,'_submission_metadata',{})}
    if getattr(args, 'submit_only', False):
        metadata.update(deferred_profile=args.profile, project_id=getattr(args, 'project_id', None),
                        source_operator_id=getattr(args, 'source_operator_id', None))
    if metadata.get('price_evidence',{}).get('kind')=='unpriced_explicit_cap':metadata['live_price']=None
    reserve(d,args,estimate,metadata)
    last=['reserved']
    def record(status,receipt):
        if status=='unknown':args._keep_browser=True
        if browser_context:receipt={**receipt,'browser_context':browser_context}
        extra={}
        if receipt.get('operator_ids'):extra['operator_id']=receipt['operator_ids'][0]
        if receipt.get('project_ids'):extra['project_id']=receipt['project_ids'][0]
        result=commit(d,args,status,receipt,**extra)
        last[0]=status
        return result
    try:
        receipt=SingleSubmission(call,d._extract_json,record).run(endpoint,str(submit_ref),expected_count=count,api_origin='https://api-cn.tripo3d.com' if args.region=='cn' else 'https://api.tripo3d.ai',expected_project_id=getattr(args,'project_id',None),resource_barrier=barrier,**getattr(args,'_uv_apply_identity',{}))
    except Exception:
        if last[0]=='reserved':commit(d,args,'aborted_before_submit',{'reason':'submit was not attempted'})
        elif last[0] in {'submitting','submitted'}:record('unknown',{'reason':'submission result was not established; no retry'})
        raise
    return receipt

def finish_operators(d,args,receipt,verify=None):
    # Receipt IDs are persisted before polling. Transport failure cannot prove a failed task.
    operators=receipt.get('operator_ids',[])
    if not operators:raise RuntimeError('Receipt has no operator_ids; inspect ledger before any further action.')
    results=[]
    over_cap_evidence=None
    wallet_halt = False
    try:
        for op in operators:results.append(d._poll_operator(args.profile,args.region,op,args.timeout_sec))
        verified=verify() if verify else {}
        after=d._fetch_preflight(args.profile,args.region)
        before=getattr(args,'_before_preflight',None)
        wallet = None
        if getattr(args, '_batch_wallet', None):
            batch = d._ledger(Path(args.ledger), 'status', '--task-id', args.task_id)
            wallet = deferred_wallet(batch['steps'], args.profile, after['balance']['credit_balance'])
            wallet_halt = wallet['observed_net_decrease'] > wallet['submitted_credit_ceiling']
            if wallet_halt:
                halt_batch_wallet(d, args, wallet, dict(zip(operators, results)))
        elif before and before['balance']['credit_balance']-after['balance']['credit_balance']>getattr(args,'_estimated_credits',0):
            over_cap_evidence={'operators':operators,'reason':'wallet_decrease_exceeds_reserved_credits',
                'wallet_before':before['balance']['credit_balance'],'wallet_after':after['balance']['credit_balance'],
                'observed_wallet_decrease':before['balance']['credit_balance']-after['balance']['credit_balance'],
                'reserved_credits':getattr(args,'_estimated_credits',0),'actual_credit_debit':'not_attributed',
                'browser_context':getattr(args,'_browser_context',None)}
            raise RuntimeError('Observed wallet decrease exceeds this reserved estimate; attribution is unclear, halt for review.')
        evidence = {'operators':operators,'observed_after':after,'actual_credit_debit':'not_attributed',**verified}
        if wallet:
            evidence['wallet'] = wallet
        commit(d,args,'success',evidence)
    except d.OperatorFailed as exc:
        commit(d,args,'failed',{'operators':operators,'reason':str(exc)})
        raise
    except Exception:
        args._keep_browser=True
        commit(d,args,'unknown',over_cap_evidence or {'operators':operators,'reason':'poll/readback uncertain; no retry','browser_context':getattr(args,'_browser_context',None)})
        raise
    result = {'ok':True,'receipt':receipt,'progress':results,'credits_used':None,'actual_credit_debit':'not_attributed',**verified}
    if wallet_halt:
        result['halted'] = True
    return result

def generate(d,args):
    args.hd_version=resolve_hd_version(getattr(args,'hd_version',None),getattr(args,'region','cn'))
    args.lp_version=resolve_lp_version(getattr(args,'lp_version',None))
    validate_generate(args,require_inputs=args.execute_spend or args.prepare_upload or not args.prepare_only)
    d._ensure_daemon(args.profile)
    pf=d._fetch_preflight(args.profile,args.region);bal=pf['balance']
    est=estimate_generate(tier=args.tier,hd_version=args.hd_version,lp_version=args.lp_version,mode=args.mode,
        batch_count=max(1,len(args.images)) if args.mode=='batch' else 1,variations=args.variations,quad=args.quad,texture=not args.no_texture,
        pbr=args.pbr,texture_quality=args.texture_quality,geometry_quality=args.geometry_quality,generate_parts=args.generate_parts,
        smart_poly=args.smart_poly,smart_mesh_p2_trial=int(bal.get('smart_mesh_p2_trial',0)),ultra_texture_trial=int(pf['quota'].get('ultra_texture_trial',0)))
    resolved_models={'region':args.region,'tier':args.tier,'hd_version':args.hd_version,'lp_version':args.lp_version}
    if not args.prepare_only and not args.execute_spend:
        return {'ok':True,'dry_run':True,'resolved_models':resolved_models,'preflight':pf,'estimate':est.to_dict()}
    if args.execute_spend:
        require_identity(args)
        if int(bal.get('credit_balance',0))<est.net_credits:raise RuntimeError('Insufficient Studio credits.')
        check_queued_submission(d, args, bal.get('credit_balance'))
        args._before_preflight=pf;args._estimated_credits=est.net_credits
    session='tripo_generate_'+uuid.uuid4().hex[:12]
    call=lambda op,*a:d._opencli(args.profile,'browser',session,op,*a)
    receipt=None
    try:
        call('open',d.REGION_ORIGINS[args.region]+'/workspace/generate')
        ui=StudioUI(call,d._extract_json)
        ui.expect(lambda s:bool(s['state']),'generation workspace loaded',timeout=15)
        ui.prepare_generate(args)
        if args.execute_spend or args.prepare_upload:
            ui.upload_inputs(args)
            ui.prepare_generate(args)
        observed=ui.verify_generate(args,require_inputs=args.execute_spend or args.prepare_upload)
        price=exact_price(observed['submit']['text'],est.net_credits,d._parse_submit_credits,observed['submit'].get('credit_prop'))
        if args.prepare_only:
            return {'ok':True,'prepared':True,'submitted':False,'uploaded':args.prepare_upload,'resolved_models':resolved_models,'estimate':est.to_dict(),'live_price':price,'observed':observed,'actions':ui.actions}
        # OpenCLI find can reuse an old fingerprint after the price changes.
        # A fresh state snapshot rebuilds identities before locating the CTA.
        ui.command('state')
        target=ui.one_any('button',('生成模型','Generate'),prefix=True)
        # Recheck immediately before the only submission click.
        observed=ui.verify_generate(args,require_inputs=True)
        exact_price(observed['submit']['text'],est.net_credits,d._parse_submit_credits,observed['submit'].get('credit_prop'))
        endpoints={'single':'image_to_model','multiview':'multiview_to_model','batch':'batch_image_to_model','text':'text_to_model'}
        count=(len(args.images) if args.mode=='batch' else 1)*args.variations
        context={'session':session,'page':d._BROWSER_PAGES.get((args.profile,session)),'profile':args.profile}
        receipt=execute_ui(d,args,call,est,observed,target['ref'],'/v2/studio/operation/'+endpoints[args.mode],count,context)
    finally:
        if not getattr(args,'_keep_browser',False):d._opencli(args.profile,'browser',session,'close',check=False)
    if getattr(args, 'submit_only', False):
        return {'ok': True, 'state': 'submitted', 'step_key': args.step_key, 'receipt': receipt,
                'estimate': est.to_dict()}
    result=finish_operators(d,args,receipt);result['estimate']=est.to_dict();return result

def postprocess(d,args):
    from tripo_post_ui import PostUI
    if getattr(args,'animation',''):
        from tripo_preview import retarget_animation
        return retarget_animation(d,args)
    if getattr(args,'retarget_credit_cap',None) is not None:raise ValueError('--retarget-credit-cap requires --animation.')
    if getattr(args,'apply_preview_step',''):
        from tripo_preview import apply_texture_preview,apply_uv_preview
        return (apply_uv_preview if args.operation=='smart-uv' else apply_texture_preview)(d,args)
    if getattr(args,'unpriced_apply_cap',None) is not None:raise ValueError('--unpriced-apply-cap requires --apply-preview-step.')
    if getattr(args,'uv_retry',False) and args.operation!='smart-uv':raise ValueError('--uv-retry is only applicable to smart-uv.')
    if args.brush_mode:raise RuntimeError('3D brush strokes require manual interaction; prompt generation is the automated mode.')
    if args.prompt and args.operation not in {'texture','texture-edit'}:raise ValueError('--prompt is mapped only for texture or texture-edit; refusing to ignore it for another operation.')
    if args.operation=='texture' and args.prompt and (not args.prompt.strip() or len(args.prompt)>1000):raise ValueError('Texture text prompt must contain 1..1000 characters.')
    if args.ref_image and args.operation!='texture':raise ValueError('--ref-image is only applicable to texture generation.')
    if args.prepare_upload and not args.prepare_only:raise ValueError('--prepare-upload requires --prepare-only.')
    if args.prepare_upload and not args.ref_image:raise ValueError('--prepare-upload requires --ref-image for texture generation.')
    if args.rig_type!='biped' and args.operation!='rigging':raise ValueError('--rig-type is only applicable to rigging.')
    if args.texture_quality!='standard' and args.operation!='texture':raise ValueError('--texture-quality is only applicable to texture generation.')
    if getattr(args,'skip_upscaler_trial',False) and (args.operation!='texture-upscale' or args.upscale_extreme):
        raise ValueError('--skip-upscaler-trial is only applicable to paid 4K texture-upscale.')
    if args.face_limit!=10000 and args.operation!='retopology':raise ValueError('--face-limit is only applicable to retopology.')
    if args.granularity!='medium' and args.operation!='segmentation':raise ValueError('--granularity is only applicable to segmentation.')
    if not re.fullmatch(r'[A-Za-z0-9-]{8,64}',args.project_id):raise ValueError('Invalid project ID.')
    if args.ref_image:args.ref_image=str(validate_reference_image(args.ref_image))
    d._ensure_daemon(args.profile)
    pf=d._fetch_preflight(args.profile,args.region);bal=pf['balance']
    est=estimate_postprocess(args.operation,quad=not args.triangle,smart_poly=args.smart_poly,texture_quality=args.texture_quality,
        use_style=bool(args.ref_image),upscale_extreme=args.upscale_extreme,uv_generate_trial=int(bal.get('uv_generate_trial',0)),upscaler_trial=0 if getattr(args,'skip_upscaler_trial',False) else int(bal.get('upscaler_trial',0)),premium_texture_edit=str(bal.get('membership_level','')).lower()=='premium')
    if not args.prepare_only and not args.execute_spend:return {'ok':True,'dry_run':True,'estimate':est.to_dict(),'preflight':pf}
    if args.execute_spend:
        require_identity(args)
        check_queued_submission(d, args, bal.get('credit_balance'))
        if getattr(args, 'submit_only', False) and (type(bal.get('credit_balance')) is not int or bal['credit_balance'] < est.net_credits):
            raise RuntimeError('Insufficient Studio credits.')
        args._before_preflight=pf;args._estimated_credits=est.net_credits
    session='tripo_post_'+uuid.uuid4().hex[:12];call=lambda op,*a:d._opencli(args.profile,'browser',session,op,*a)
    try:
        call('open',d.REGION_ORIGINS[args.region]+d.POSTPROCESS_CONFIG[args.operation]['route']+'/'+args.project_id)
        ui=PostUI(call,d._extract_json,args.operation,d.POSTPROCESS_CONFIG[args.operation])
        prepared=ui.prepare(args)
        if args.operation=='smart-uv' and getattr(args,'uv_retry',False):
            state=ui.uv_state();label=(state.get('primary') or {}).get('text','')
            if label.startswith('免费重试') and 0<len(state['candidates'])<4:
                from tripo_pricing import CreditEstimate
                est=CreditEstimate('postprocess_smart-uv_retry',0,0,0,None,['Explicit next UV candidate; source-confirmed free retry window'])
            else:est=estimate_postprocess('smart-uv',is_uv_retry=True)
            args._estimated_credits=est.net_credits
        observed=ui.observe();reference_required=bool(args.ref_image);reference_uploaded=bool(getattr(ui,'reference_uploaded',False))
        if args.operation=='texture':
            ready=texture_ready(observed,reference_required,reference_uploaded)
        else:
            ready=bool(observed.get('submit')) and observed['submit'].get('disabled') is False
        # In prepare-only, a requested reference that was not uploaded cannot
        # be accepted as a style request.  Report the current live price for
        # diagnostics, but never mark it as verified/ready.
        price=None;price_verified=False
        if not (args.prepare_only and reference_required and not reference_uploaded):
            price=exact_price(observed['submit']['text'],est.net_credits,d._parse_submit_credits,observed['submit'].get('credit_prop'))
            price_verified=True
        else:
            price=d._parse_submit_credits(observed['submit']['text'])
        if args.prepare_only:
            result={'ok':True,'prepared':True,'submitted':False,'ready':ready,'price_verified':price_verified,
                    'estimate':est.to_dict(),'live_price':price,'observed':observed,'actions':ui.actions}
            if args.operation=='texture':
                result.update(reference_uploaded=reference_uploaded,
                              reference_ready=bool(observed.get('reference_ready')),
                              base_ready=bool(observed.get('base_ready')))
            return result
        if not ready:raise RuntimeError('Inputs or submit control are not ready; no submit.')
        if observed['submit']['disabled']:raise RuntimeError('Postprocess is ineligible or not ready; no submit.')
        ui.command('state')
        target=ui.submit_entry()
        observed=ui.verify(args)
        reference_uploaded=bool(getattr(ui,'reference_uploaded',False))
        if args.operation=='texture':
            if not texture_ready(observed,reference_required,reference_uploaded):
                raise RuntimeError('Texture inputs lost readiness before submit; no submit.')
        elif not observed.get('submit') or observed['submit'].get('disabled') is not False:
            raise RuntimeError('Inputs or submit control lost readiness before submit; no submit.')
        exact_price(observed['submit']['text'],est.net_credits,d._parse_submit_credits,observed['submit'].get('credit_prop'))
        if getattr(args, 'submit_only', False):
            d._verify_current_project(args.profile, args.region, args.project_id, args.source_operator_id)
        context={'session':session,'page':d._BROWSER_PAGES.get((args.profile,session)),'profile':args.profile}
        args._browser_context=context
        preview_before=ui.edit_preview_state() if args.operation=='texture-edit' else None
        uv_before=ui.uv_state() if args.operation=='smart-uv' else None
        receipt=execute_ui(d,args,call,est,observed,target['ref'],d.POSTPROCESS_CONFIG[args.operation]['endpoint_hint'],1,context)
        if getattr(args, 'submit_only', False):
            return {'ok': True, 'state': 'submitted', 'step_key': args.step_key, 'receipt': receipt,
                    'estimate': est.to_dict()}
        def verify_preview():
            s=ui.wait_edit_state(lambda x:x.get('project_id')==args.project_id and x.get('source_operator_id')==preview_before.get('source_operator_id') and len(x['images'])>len(preview_before['images']) and x.get('selected_index')==0 and bool(x['images'][0].get('fingerprint')),'new preview rendered and selected',timeout=30)
            return {'result_kind':'preview_ready','asset_applied':False,'preview':{'project_id':args.project_id,'source_operator_id':s['source_operator_id'],'fingerprint':s['images'][0]['fingerprint'],'browser_context':context}}
        def verify_uv():
            candidate=receipt['operator_ids'][0]
            s=ui.wait_uv_state(lambda x:x.get('project_id')==args.project_id and x.get('source_operator_id')==uv_before.get('source_operator_id') and any(c['operator_id']==candidate for c in x['candidates']),'generated UV candidate persisted in context')
            return {'result_kind':'preview_ready','asset_applied':False,'preview':{'project_id':args.project_id,'source_operator_id':s['source_operator_id'],'candidate_operator_id':candidate,'browser_context':context}}
        def verify_rig():
            deadline=time.monotonic()+30
            while True:
                s=ui.rig_state()
                if s.get('project_id')==args.project_id and s.get('is_rigged') is True and s.get('source_operator_id')==receipt['operator_ids'][0] and s.get('rig_type')==args.rig_type:return {'result_kind':'asset_rigged','asset_applied':True,'rig_readback':s}
                if time.monotonic()>=deadline:raise RuntimeError('Rigging success was not reflected by the requested project/rig type.')
                time.sleep(.5)
        verify=verify_preview if preview_before is not None else verify_uv if uv_before is not None else verify_rig if args.operation=='rigging' else None
        result=finish_operators(d,args,receipt,verify)
        if result.get('result_kind')=='preview_ready':args._keep_browser=True
        return result
    finally:
        if not getattr(args,'_keep_browser',False):d._opencli(args.profile,'browser',session,'close',check=False)
