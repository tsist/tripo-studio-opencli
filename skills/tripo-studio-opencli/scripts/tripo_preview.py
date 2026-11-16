"""Apply an exact recorded texture preview without generating it again."""
from pathlib import Path
import re,uuid
from tripo_pricing import CreditEstimate
from tripo_post_ui import PostUI


def attach_preview_page(d,args,preview,route):
    """Resume the recorded unsaved page without opening/navigating any tab."""
    ctx=preview.get('browser_context') or getattr(args,'_preview_browser_context',None)
    if not isinstance(ctx,dict) or ctx.get('profile')!=args.profile or any(not isinstance(ctx.get(k),str) or not ctx[k] for k in ['session','page']):
        raise RuntimeError('Preview has no verified owned browser context; no apply and no page guessing.')
    session=ctx['session'];page=ctx['page']
    call=lambda op,*a:d._opencli(args.profile,'browser',session,op,*a)
    tabs=d._extract_json(call('tab','list'))
    expected=d.REGION_ORIGINS[args.region]+'/workspace/'+route+'/'+args.project_id
    matches=[t for t in tabs if isinstance(t,dict) and t.get('page')==page and t.get('url','').rstrip('/')==expected]
    if len(matches)!=1:raise RuntimeError('Recorded preview page is missing or changed; no navigation or apply.')
    key=(args.profile,session)
    if key in d._BROWSER_PAGES and d._BROWSER_PAGES[key]!=page:raise RuntimeError('Preview session is already bound to a different page.')
    d._BROWSER_PAGES[key]=page
    args._keep_browser=True
    return session,call


def one_receipt_operator(d,args,receipt,context):
    operators=receipt.get('operator_ids',[]) if isinstance(receipt,dict) else []
    if not isinstance(operators,list) or len(operators)!=1 or not isinstance(operators[0],str) or not re.fullmatch(r'[A-Za-z0-9-]{8,64}',operators[0]):
        from tripo_actions import commit
        args._keep_browser=True
        commit(d,args,'unknown',{'reason':'apply receipt lacks exactly one valid operator','browser_context':context})
        raise RuntimeError('Apply receipt is incomplete; halt without closing its page or repeating save.')
    return operators[0]


def recorded_preview(d,args):
    from tripo_actions import require_identity
    require_identity(args)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',args.apply_preview_step):raise ValueError('Unsafe preview step key.')
    if args.apply_preview_step==args.step_key:raise ValueError('Preview and apply must have separate step keys.')
    task=d._ledger(Path(args.ledger),'status','--task-id',args.task_id)
    if task.get('region')!=args.region:raise ValueError('Preview task region mismatch.')
    step=task.get('steps',{}).get(args.apply_preview_step,{})
    evidence=step.get('evidence',{})
    preview=evidence.get('preview',{})
    if step.get('status')!='success' or evidence.get('result_kind')!='preview_ready' or evidence.get('asset_applied') is not False:
        raise RuntimeError('Apply needs a successfully recorded preview_ready step; no preview generation or retry.')
    identity_ok=re.fullmatch(r'[A-Za-z0-9-]{8,64}',str(preview.get('candidate_operator_id',''))) if args.operation=='smart-uv' else re.fullmatch(r'[0-9a-f]{64}',str(preview.get('fingerprint','')))
    if preview.get('project_id')!=args.project_id or not preview.get('source_operator_id') or not identity_ok:
        raise RuntimeError('Recorded preview identity/source is incomplete or belongs to another project.')
    for item in task.get('steps',{}).values():
        if item.get('metadata',{}).get('preview_step')==args.apply_preview_step and item.get('status')!='aborted_before_submit':
            raise RuntimeError('This preview already has an apply intent; inspect its receipt, never submit it again.')
    return preview


def apply_texture_preview(d,args):
    from tripo_actions import require_spend_contract
    if args.operation!='texture-edit':raise ValueError('--apply-preview-step currently requires texture-edit.')
    if args.prompt or args.ref_image or args.brush_mode or args.prepare_upload or args.smart_poly or args.triangle or args.upscale_extreme or args.rig_type!='biped' or args.face_limit!=10000 or args.granularity!='medium' or args.texture_quality!='standard' or getattr(args,'uv_retry',False) or getattr(args,'animation','') or getattr(args,'retarget_credit_cap',None) is not None:
        raise ValueError('Apply only consumes the exact recorded preview; generation/input parameters do not apply.')
    if not re.fullmatch(r'[A-Za-z0-9-]{8,64}',args.project_id):raise ValueError('Invalid project ID.')
    cap=args.unpriced_apply_cap
    if cap is not None and (type(cap) is not int or cap<0):raise ValueError('Apply exposure cap must be a nonnegative integer.')
    if args.execute_spend:require_spend_contract('postprocess_texture_edit_apply')
    preview=recorded_preview(d,args)
    if not args.prepare_only and not args.execute_spend:
        return {'ok':True,'dry_run':True,'result_kind':'apply_plan','ready':False,'execution_blocked':True,'requires_verified_price_contract':True,'preview':preview,'quoted_credits':None,'authorized_exposure_cap':cap,'blocked_reason':'Texture save has no verified price or upper bound; internal cap cannot unlock submission.'}
    d._ensure_daemon(args.profile)
    pf=d._fetch_preflight(args.profile,args.region)
    if cap is not None and pf['balance']['credit_balance']<cap:raise RuntimeError('Insufficient balance for the authorized exposure cap.')
    session,call=attach_preview_page(d,args,preview,'texture-edit')
    try:
        ui=PostUI(call,d._extract_json,'texture-edit',d.POSTPROCESS_CONFIG['texture-edit'])
        observed=ui.prepare_edit_apply(args,preview)
        price=d._parse_submit_credits(observed['apply_submit']['text'])
        # A future visible price is a new contract, not silently accepted under
        # today's unpriced-save authorization.
        if price is not None:raise RuntimeError('Apply now displays a price; recheck the pricing contract before submitting.')
        return {'ok':True,'prepared':True,'ui_ready':True,'ready':False,'submitted':False,'execution_blocked':True,'requires_verified_price_contract':True,'price_verified':False,'quoted_credits':None,'authorized_exposure_cap':cap,'preview':preview,'observed':observed,'actions':ui.actions,'blocked_reason':'Texture save price/upper bound remains unverified.'}
    finally:
        if not getattr(args,'_keep_browser',False):d._opencli(args.profile,'browser',session,'close',check=False)


def apply_uv_preview(d,args):
    from tripo_actions import execute_ui,finish_operators
    if args.operation!='smart-uv':raise ValueError('UV apply requires smart-uv.')
    if args.prompt or args.ref_image or args.brush_mode or args.prepare_upload or args.smart_poly or args.triangle or args.upscale_extreme or args.rig_type!='biped' or args.face_limit!=10000 or args.granularity!='medium' or args.texture_quality!='standard' or getattr(args,'uv_retry',False) or getattr(args,'animation','') or getattr(args,'retarget_credit_cap',None) is not None:
        raise ValueError('UV apply only consumes the recorded candidate; generation parameters do not apply.')
    if not re.fullmatch(r'[A-Za-z0-9-]{8,64}',args.project_id):raise ValueError('Invalid project ID.')
    cap=args.unpriced_apply_cap
    if cap is not None and (type(cap) is not int or cap<0):raise ValueError('Apply exposure cap must be a nonnegative integer.')
    preview=recorded_preview(d,args)
    if preview['source_operator_id']==preview['candidate_operator_id']:raise RuntimeError('UV candidate is already the source operator; no repeat apply.')
    if not args.prepare_only and not args.execute_spend:return {'ok':True,'dry_run':True,'result_kind':'apply_plan','preview':preview,'quoted_credits':None,'authorized_exposure_cap':cap,'removes_current_textures':True}
    if args.execute_spend and cap is None:raise RuntimeError('UV save has no verified price. An explicitly authorized --unpriced-apply-cap is required; no submit.')
    d._ensure_daemon(args.profile);pf=d._fetch_preflight(args.profile,args.region)
    if cap is not None and pf['balance']['credit_balance']<cap:raise RuntimeError('Insufficient balance for the authorized exposure cap.')
    session,call=attach_preview_page(d,args,preview,'smart-uv')
    try:
        ui=PostUI(call,d._extract_json,'smart-uv',d.POSTPROCESS_CONFIG['smart-uv'])
        observed=ui.prepare_uv_apply(args,preview)
        if d._parse_submit_credits(observed['apply_submit']['text']) is not None:raise RuntimeError('UV save now displays a price; recheck its contract.')
        if args.prepare_only:return {'ok':True,'prepared':True,'ready':True,'submitted':False,'price_verified':False,'quoted_credits':None,'preview':preview,'observed':observed,'removes_current_textures':True}
        estimate=CreditEstimate('postprocess_smart_uv_apply',cap,0,cap,None,['Local reserved exposure for unpriced UV save; not a provider charge bound or price quote'])
        args._before_preflight=pf;args._estimated_credits=cap
        args._submission_metadata={'preview_step':args.apply_preview_step,'candidate_operator_id':preview['candidate_operator_id'],'price_evidence':{'kind':'unpriced_explicit_cap','quoted_credits':None,'authorized_exposure_cap':cap}}
        args._uv_apply_identity={'uv_apply_source_id':preview['source_operator_id'],'uv_apply_candidate_id':preview['candidate_operator_id']}
        context={'session':session,'page':d._BROWSER_PAGES.get((args.profile,session)),'profile':args.profile};args._browser_context=context
        ui.command('state');target=ui.one('button','保存')
        current=ui.uv_state()
        if current.get('project_id')!=args.project_id or current.get('source_operator_id')!=preview['source_operator_id'] or current.get('readonly') or not current.get('apply_submit') or current['apply_submit']['disabled'] or not any(c['operator_id']==preview['candidate_operator_id'] and c['selected'] for c in current['candidates']):
            raise RuntimeError('UV source, candidate or readiness changed; no submit.')
        if d._parse_submit_credits(current['apply_submit']['text']) is not None:raise RuntimeError('UV apply price appeared before submit; recheck its contract.')
        receipt=execute_ui(d,args,call,estimate,{'state':{'candidate_operator_id':preview['candidate_operator_id']}},target['ref'],'/v2/studio/operation/uv_edit/apply',1,context)
        if one_receipt_operator(d,args,receipt,context)!=preview['candidate_operator_id']:
            from tripo_actions import commit
            args._keep_browser=True
            commit(d,args,'unknown',{'reason':'UV apply operator does not match recorded candidate','browser_context':context})
            raise RuntimeError('UV apply receipt identity mismatch; halt.')
        def verify():
            ui.wait_uv_state(lambda x:x.get('project_id')==args.project_id and x.get('source_operator_id')==preview['candidate_operator_id'],'UV candidate became current project operator')
            return {'result_kind':'asset_applied','asset_applied':True,'applied_project_id':args.project_id,'applied_operator_id':preview['candidate_operator_id'],'preview_step':args.apply_preview_step,'removes_previous_textures':True,'quoted_credits':None,'authorized_exposure_cap':cap}
        result=finish_operators(d,args,receipt,verify)
        args._keep_browser=False
        return result
    finally:
        if not getattr(args,'_keep_browser',False):d._opencli(args.profile,'browser',session,'close',check=False)


def retarget_animation(d,args):
    from tripo_actions import execute_ui,finish_operators,require_identity
    if args.operation!='rigging' or not args.animation.strip() or len(args.animation)>120:raise ValueError('Animation needs rigging and an exact card label of 1..120 characters.')
    if args.prompt or args.ref_image or args.brush_mode or args.prepare_upload or args.smart_poly or args.triangle or args.upscale_extreme or args.face_limit!=10000 or args.granularity!='medium' or args.texture_quality!='standard' or args.apply_preview_step or args.unpriced_apply_cap is not None or args.uv_retry:
        raise ValueError('Animation consumes a rigged source and a card label; generation/apply parameters do not apply.')
    if not re.fullmatch(r'[A-Za-z0-9-]{8,64}',args.project_id):raise ValueError('Invalid project ID.')
    cap=args.retarget_credit_cap
    if cap is not None and (type(cap) is not int or cap<0):raise ValueError('Retarget exposure cap must be a nonnegative integer.')
    if args.execute_spend:
        require_identity(args)
        if cap is None:raise RuntimeError('Animation card has no verified price. An explicitly authorized --retarget-credit-cap is required; no submit.')
    if not args.prepare_only and not args.execute_spend:return {'ok':True,'dry_run':True,'operation':'retarget','animation':args.animation,'quoted_credits':None,'authorized_exposure_cap':cap}
    d._ensure_daemon(args.profile);pf=d._fetch_preflight(args.profile,args.region)
    if cap is not None and pf['balance']['credit_balance']<cap:raise RuntimeError('Insufficient balance for the retarget exposure cap.')
    session='tripo_retarget_'+uuid.uuid4().hex[:12]
    call=lambda op,*a:d._opencli(args.profile,'browser',session,op,*a)
    try:
        call('open',d.REGION_ORIGINS[args.region]+'/workspace/rigging/'+args.project_id)
        ui=PostUI(call,d._extract_json,'rigging',d.POSTPROCESS_CONFIG['rigging'])
        s=ui.wait_animation_state(lambda x:x.get('project_id')==args.project_id and bool(x.get('cards')),'rigged source and animation cards loaded')
        if s.get('rig_type')!=args.rig_type:raise RuntimeError('Source rig type does not match requested animation type; no submit.')
        cards=[c for c in s['cards'] if c['label']==args.animation]
        if len(cards)!=1 or cards[0]['disabled']:raise RuntimeError('Requested animation card is missing, ambiguous or disabled.')
        if cards[0].get('text')!=args.animation:raise RuntimeError('Animation card contains unexpected text/price; recheck its contract.')
        if args.prepare_only:return {'ok':True,'prepared':True,'ready':True,'submitted':False,'price_verified':False,'quoted_credits':None,'authorized_exposure_cap':cap,'observed':s,'cached':cards[0]['cached']}
        source=s['source_operator_id']
        if s.get('readonly'):raise RuntimeError('Rigged source is not editable; no retarget submit.')
        ui.command('state');target=ui.animation_entry(args.animation)
        current=ui.animation_state();matches=[c for c in current['cards'] if c['label']==args.animation]
        if current.get('project_id')!=args.project_id or current.get('source_operator_id')!=source or current.get('rig_type')!=args.rig_type or current.get('readonly') or len(matches)!=1 or matches[0]['disabled'] or matches[0].get('text')!=args.animation:raise RuntimeError('Animation source/card changed before submit.')
        if matches[0]['cached']:
            if not matches[0]['selected']:ui.click(target)
            selected=ui.wait_animation_state(lambda x:any(c['label']==args.animation and c['cached'] and c['selected'] for c in x['cards']),'cached animation selected')
            return {'ok':True,'submitted':False,'cached':True,'result_kind':'animation_preview_selected','animation':args.animation,'observed':selected}
        estimate=CreditEstimate('postprocess_retarget',cap,0,cap,None,['Local reserved exposure for an unpriced animation card; not a provider charge bound or price quote'])
        args._before_preflight=pf;args._estimated_credits=cap
        args._submission_metadata={'animation_label':args.animation,'price_evidence':{'kind':'unpriced_explicit_cap','quoted_credits':None,'authorized_exposure_cap':cap}}
        context={'session':session,'page':d._BROWSER_PAGES.get((args.profile,session)),'profile':args.profile};args._browser_context=context
        receipt=execute_ui(d,args,call,estimate,{'state':{'animation_label':args.animation,'rig_type':args.rig_type}},target['ref'],'/v2/studio/operation/retarget_model',1,context)
        operator=one_receipt_operator(d,args,receipt,context)
        def verify():
            ready=ui.wait_animation_state(lambda x:x.get('project_id')==args.project_id and any(o['operator_id']==operator and o['has_model'] for o in x['retarget_ops']) and any(c['label']==args.animation and c['cached'] for c in x['cards']),'retarget output persisted in project')
            op=next(o for o in ready['retarget_ops'] if o['operator_id']==operator and o['has_model'])
            if not any(c['label']==args.animation and c['selected'] for c in ready['cards']):ui.click(ui.animation_entry(args.animation))
            ui.wait_animation_state(lambda x:any(c['label']==args.animation and c['cached'] and c['selected'] for c in x['cards']),'generated animation selected in viewer')
            return {'result_kind':'animation_ready','asset_applied':True,'animation_label':args.animation,'animation_name':op['name'],'retarget_operator_id':operator,'quoted_credits':None,'authorized_exposure_cap':cap}
        return finish_operators(d,args,receipt,verify)
    finally:
        if not getattr(args,'_keep_browser',False):d._opencli(args.profile,'browser',session,'close',check=False)
