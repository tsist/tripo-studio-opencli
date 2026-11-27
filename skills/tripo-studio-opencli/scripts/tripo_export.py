"""One budgeted export; later queries only resume the recorded download job."""
from __future__ import annotations
import json,re,os,uuid
from contextlib import nullcontext
from pathlib import Path
from tripo_actions import require_identity,reserve,commit
import tripo_ledger
from tripo_pricing import estimate_postprocess
from tripo_download import safe_destination,validate_artifact,SmartDownloadBridge

def _new_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:
        json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())

def _update_json(path,data):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    _new_json(temp,data);os.replace(temp,path)

def _params(args):
    params = {k:getattr(args,k) for k in ('project_id','model_format','fbx_preset','texture_packaging','texture_size','name','with_animation','enable_bake_animation','animation_names','pivot_to_center_bottom','pack_uv')}
    return {**params, 'source_operator_id': getattr(args, 'source_operator_id', '')}

def _recorded_params(step):
    stored=step.get('metadata',{}).get('export_params')
    if not isinstance(stored,dict):return None
    # Added selectors default to the former request body. Old receipts remain
    # resumable; no new export is allowed through this normalization.
    return {'animation_names':[],'pivot_to_center_bottom':False,'pack_uv':False,
            'source_operator_id':'',**stored}


def _artifact_destination(args, filename):
    root = Path(args.runtime_dir).resolve()
    if getattr(args, 'source_operator_id', ''):
        # A deferred sibling export may not have created its output yet.
        # Isolate each step even when the cloud gives both assets the same name.
        task_root = (root / args.task_id).resolve()
        if task_root == root or not task_root.is_relative_to(root):
            raise ValueError('Export task directory escapes the runtime directory.')
        return safe_destination(task_root, args.step_key, filename)
    return safe_destination(root, args.task_id, filename)


def _verify_source_record(args, rec, step):
    metadata = step.get('metadata', {})
    profile = metadata.get('export_profile')
    if profile is not None and profile != args.profile:
        raise RuntimeError('Export receipt belongs to another Studio profile.')
    source = _recorded_params(step)['source_operator_id']
    if rec.get('source_operator_id', '') != source:
        raise RuntimeError('Download receipt source operator differs from the recorded export.')
    if (step.get('status') == 'unknown'
            and (step.get('evidence') or {}).get('reason') == 'source_unverified_after_export_submission'):
        raise RuntimeError('Export source is unresolved. Verify the original operator before releasing any artifact.')


def _verify_project_available(d, args):
    ledger = d._ledger(Path(args.ledger), 'status', '--task-id', args.task_id)
    if getattr(args, 'submit_only', False) and ledger.get('studio_profile') != args.profile:
        raise RuntimeError('Use the Studio profile bound at init-task for this batch.')
    for key, step in ledger['steps'].items():
        if key == args.step_key or step.get('status') not in {'reserved', 'submitting', 'submitted', 'unknown'}:
            continue
        metadata = step.get('metadata') or {}
        evidence = step.get('evidence') or {}
        if (args.project_id == step.get('project_id')
                or args.project_id == metadata.get('project_id')
                or args.project_id in (evidence.get('project_ids') or [])):
            raise RuntimeError(f'Project still has active step {key!r}; resolve it before a new export.')


def _check_deferred_progress(d, args, operator):
    rows = d._extract_json(d._opencli(
        args.profile, 'tripo-studio', 'progress', '--ids', operator,
        '--region', args.region, '-f', 'json'))
    if (not isinstance(rows, list) or len(rows) != 1
            or not isinstance(rows[0], dict) or rows[0].get('operator_id') != operator):
        raise RuntimeError('Progress response did not match the recorded export operator.')
    status = rows[0].get('status')
    if not isinstance(status, str) or not status:
        raise RuntimeError('Recorded export state is uncertain; inspect the original receipt.')
    return status.lower()


def _current_project(d, args):
    rows = d._extract_json(d._opencli(args.profile, 'tripo-studio', 'project', args.project_id,
                                     '--region', args.region, '-f', 'json'))
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or type(rows[0].get('is_owner')) is not bool):
        raise RuntimeError('Ownership could not be established.')
    return rows[0]


def _matches_requested_source(row, args):
    return (row.get('project_id') == args.project_id
            and row.get('operator_id') == args.source_operator_id
            and row.get('status') == 'success')

_MANIFEST_ARTIFACT_FIELDS=('path','format','packaging','kind','bytes','sha256','sha256_scope','publisher_verified','validation_scope','asset_usability_verified','errors','details')

def _confined(path,root):
    try:return Path(path).resolve().is_relative_to(root)
    except (OSError,RuntimeError,ValueError):return False

def _verify_download_record(args, rec, ledger, step, root):
    if ledger.get('region') != args.region:
        raise RuntimeError('Export ledger belongs to another Studio region.')
    if not isinstance(step, dict) or step.get('status') not in {'submitted', 'unknown', 'success'}:
        raise RuntimeError('Download/ledger state conflict; inspect without re-exporting.')
    if step.get('project_id') != args.project_id:
        raise RuntimeError('Download project does not match the recorded step; no success committed.')
    operator = rec.get('operator_id')
    if not isinstance(operator, str) or not operator or step.get('operator_id') != operator:
        raise RuntimeError('Download operator does not match the recorded step; no success committed.')
    if _recorded_params(step)!=_params(args):
        raise RuntimeError('Export parameters differ from the recorded step; no success committed.')
    _verify_source_record(args, rec, step)
    if rec.get('format')!=args.model_format or rec.get('packaging')!=args.texture_packaging:
        raise RuntimeError('Download record format/packaging differs from the recorded export; no success committed.')
    destination=rec.get('destination')
    if not isinstance(destination,str) or not destination:raise RuntimeError('Download record has no artifact destination; no success committed.')
    path=Path(destination)
    if not path.is_absolute() or not _confined(path,root):
        raise RuntimeError('Recorded artifact path is outside the runtime directory; no success committed.')
    if rec.get('source_operator_id') and path.parent.resolve() != (root / args.task_id / args.step_key).resolve():
        raise RuntimeError('Recorded artifact path is outside its export step directory.')
    return path


def _verify_existing_manifest(manifest, rec, path):
    if not isinstance(manifest, dict):
        raise RuntimeError('Export manifest is corrupt; no success committed.')
    artifact = manifest.get('artifact')
    if not isinstance(artifact, dict):
        raise RuntimeError('Export manifest has no artifact receipt; no success committed.')
    if any(manifest.get(key) != rec[key] for key in ('project_id', 'operator_id')):
        raise RuntimeError('Export manifest project or operator does not match the recorded step; no success committed.')
    if manifest.get('source_operator_id', '') != rec.get('source_operator_id', ''):
        raise RuntimeError('Export manifest source operator does not match the recorded step; no success committed.')
    if rec.get('source_operator_id') and manifest.get('source_check') != 'studio_current_before_submit':
        raise RuntimeError('Export manifest source-check evidence is missing.')
    if manifest.get('download_job') != rec['job_id']:
        raise RuntimeError('Export manifest download job does not match the recorded step; no success committed.')
    if artifact.get('path') != rec['destination']:
        raise RuntimeError('Manifest artifact path does not match the recorded destination; no success committed.')
    if artifact.get('ok') is not True or artifact.get('sha256_scope')!='local_fingerprint_only' or artifact.get('publisher_verified') is not False:
        raise RuntimeError('Manifest artifact does not describe a local, validated fingerprint; no success committed.')
    if manifest.get('publisher_verified',False) is not False:
        raise RuntimeError('Manifest claims publisher verification for a local fingerprint; no success committed.')
    if not path.is_file():raise RuntimeError('Manifest artifact is missing; no success committed.')
    try:current=validate_artifact(path,rec['format'],rec['packaging'])
    except Exception as exc:raise RuntimeError('Manifest artifact could not be revalidated; no success committed.') from exc
    if current.get('ok') is not True:raise RuntimeError('Manifest artifact failed local revalidation; no success committed.')
    for field in _MANIFEST_ARTIFACT_FIELDS:
        if artifact.get(field)!=current.get(field):
            raise RuntimeError(f'Manifest artifact {field} does not match the file; no success committed.')

def _finish_commit(d,args,step,manifest):
    # CLI switch has no value; use the exact argument list for reconciliation.
    if step.get('status')=='unknown':
        return d._ledger(Path(args.ledger),'commit','--task-id',args.task_id,'--step-key',args.step_key,'--status','success','--reconcile-unknown','--evidence-json',json.dumps(manifest))
    return commit(d,args,'success',manifest)


def _partial_backend(destination, previous_job):
    partial = destination.with_name(destination.name + '.partial')
    control = partial.with_name(partial.name + '.aria2')
    has_partial = os.path.lexists(partial)
    has_control = os.path.lexists(control)
    if not has_partial and not has_control:
        return None
    if (not partial.is_file() or partial.resolve() != partial
            or has_control and (not control.is_file() or control.resolve() != control)):
        raise RuntimeError('Download partial or control file is incomplete or redirected; retained as-is.')
    backend = previous_job.get('backend') if previous_job else None
    if backend not in {'aria2', 'curl'} or has_control and backend != 'aria2':
        raise RuntimeError('Partial download lacks a matching original backend; retained as-is.')
    return backend


def _resolve_recorded(d,args,record_path,bridge,rec):
    state=d._ledger(Path(args.ledger),'status','--task-id',args.task_id)
    step=state.get('steps',{}).get(args.step_key,{})
    if state.get('region') != args.region or step.get('project_id') != args.project_id:
        raise RuntimeError('Recovery ledger belongs to another project or region.')
    if _recorded_params(step)!=_params(args):raise RuntimeError('Recovery parameters differ from the recorded export; no operation performed.')
    if step.get('status') not in {'submitted','unknown'}:raise RuntimeError('Only a submitted/unknown recorded export can be resolved.')
    if not rec or rec.get('operator_id')!=step.get('operator_id'):raise RuntimeError('No matching durable export receipt to recover.')
    _verify_source_record(args, rec, step)
    destination = Path(rec['destination']) if rec.get('destination') else None
    root = Path(args.runtime_dir).resolve()
    if destination is not None and (not destination.is_absolute() or not _confined(destination, root)):
        raise RuntimeError('Recorded download destination is outside the runtime directory.')
    if destination is not None and rec.get('source_operator_id') and destination.parent.resolve() != (root / args.task_id / args.step_key).resolve():
        raise RuntimeError('Recorded download destination is outside its export step directory.')
    job = None
    if rec.get('job_id'):
        job=bridge.status(rec['job_id'])
        if (job.get('id') != rec['job_id'] or not rec.get('destination')
                or str(job.get('destination', '')).casefold() != str(rec['destination']).casefold()):
            raise RuntimeError('Download job identity or destination differs from the recorded export.')
        if job.get('status') not in {'failed','interrupted','cancelled'}:
            return {'ok': True, 'job': job, 'download_job': rec['job_id'],
                    'message': 'Existing download is active or complete; use --download-job, not another export.'}
    if destination is not None:
        _partial_backend(destination, job)
    # New exports already hold the browser lock and have checked the bridge.
    browser_lock = (tripo_ledger._ledger_lock(Path(args.runtime_dir) / 'studio-browser.lock')
                    if args.resume_export else nullcontext())
    with browser_lock:
        if args.resume_export:
            d._ensure_daemon(args.profile)
            if not record_path.is_file():
                _new_json(record_path, rec)
            if step.get('metadata', {}).get('deferred_export'):
                status = _check_deferred_progress(d, args, rec['operator_id'])
                if status == 'unknown':
                    if step['status'] == 'submitted':
                        commit(d, args, 'unknown', {'operator_ids': [rec['operator_id']],
                                                    'project_ids': [args.project_id],
                                                    'source_operator_id': rec['source_operator_id'],
                                                    'reason': 'Studio reported unknown for the original export'})
                    raise RuntimeError('Recorded export state is uncertain; inspect the original receipt.')
                if status in tripo_ledger.TERMINAL_HALT_STATUSES:
                    if step['status'] == 'submitted':
                        commit(d, args, status, {'operator_ids': [rec['operator_id']],
                                                 'project_ids': [args.project_id],
                                                 'source_operator_id': rec['source_operator_id'],
                                                 'reason': 'matching Studio progress receipt'})
                    raise RuntimeError(f'Recorded export ended with status {status!r}; inspect the original receipt.')
                if status != 'success':
                    return {'ok': True, 'export_pending': True, 'operator_id': rec['operator_id'],
                            'status': status, 'record_path': str(record_path)}
        return _retrieve_recorded_export(d, args, record_path, bridge, rec, job)


def _retrieve_recorded_export(d, args, record_path, bridge, rec, previous_job):
    named=['--name',Path(rec['destination']).stem] if rec.get('destination') else []
    rows=d._extract_json(d._opencli(args.profile,*_adapter_args(args),*named,'--resume-operator-id',rec['operator_id'],timeout=220))
    if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('operator_id')!=rec['operator_id'] or rows[0].get('project_id')!=args.project_id:raise RuntimeError('Export resolution did not match its durable receipt.')
    row=rows[0]
    if not rec.get('destination'):
        rec['destination']=str(_artifact_destination(args, row.get('file_name','')))
        _update_json(record_path,rec)
    dest=Path(rec['destination'])
    if row.get('file_name')!=dest.name:raise RuntimeError('Resolved export filename changed; no download started.')
    backend = _partial_backend(dest, previous_job)
    transfer_options = {'backend': backend} if backend else {}
    job=bridge.start(row['model_url'], dest, **transfer_options)
    if rec.get('job_id'):rec.setdefault('previous_jobs',[]).append(rec['job_id'])
    rec['job_id']=job['id'];_update_json(record_path,rec)
    return {'ok':True,'download_running':True,'job':job,'operator_id':rec['operator_id'],'record_path':str(record_path),'message':'Recorded export resolved; no new export submitted. Check this download job once.'}

def _adapter_args(args):
    result=['tripo-studio','export-url',args.project_id,'--model-format',args.model_format,'--fbx-preset',args.fbx_preset,
        '--texture-packaging',args.texture_packaging,'--texture-size',str(args.texture_size),'--region',args.region,'-f','json']
    if args.name:result+=['--name',args.name]
    if args.with_animation:result+=['--with-animation']
    if args.enable_bake_animation:result+=['--enable-bake-animation']
    if args.animation_names:result+=['--animation-names',','.join(args.animation_names)]
    if args.pivot_to_center_bottom:result+=['--pivot-to-center-bottom']
    if args.pack_uv:result+=['--pack-uv']
    if args.allow_nonowner_spend:result+=['--allow-nonowner-spend']
    return result

def _status(d,args,record_path,bridge):
    if not record_path.is_file():raise RuntimeError('No matching export download record; never re-export to recover a download.')
    rec=json.loads(record_path.read_text(encoding='utf-8'))
    if rec.get('job_id')!=args.download_job or rec.get('project_id')!=args.project_id:raise RuntimeError('Download job/source does not match this step.')
    if rec.get('region')!=args.region:raise RuntimeError('Download record belongs to another Studio region.')
    manifest_path=record_path.with_suffix('.manifest.json')
    ledger=d._ledger(Path(args.ledger),'status','--task-id',args.task_id)
    step=ledger.get('steps',{}).get(args.step_key,{})
    path = _verify_download_record(args, rec, ledger, step, Path(args.runtime_dir).resolve())
    if not manifest_path.exists() and step.get('status') == 'success':
        # A settled export already has its verified artifact receipt. Rebuild
        # the missing file from that receipt without repeating the ledger commit.
        settled = step.get('evidence')
        if not isinstance(settled, dict):
            raise RuntimeError('The settled export has no artifact receipt to restore its manifest.')
        _verify_existing_manifest(settled, rec, path)
        _new_json(manifest_path, settled)
        return {'ok': True, 'already_verified': True, 'manifest_restored': True,
                'manifest': settled, 'manifest_path': str(manifest_path)}
    if manifest_path.exists():
        try:manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:raise RuntimeError('Export manifest is unreadable or corrupt; no success committed.') from exc
        _verify_existing_manifest(manifest, rec, path)
        if step.get('status') in {'submitted','unknown'}:_finish_commit(d,args,step,manifest)
        return {'ok':True,'already_verified':True,'manifest':manifest}
    job=bridge.status(args.download_job)
    if job.get('id') != args.download_job:
        raise RuntimeError('Downloader returned a different job; no success committed.')
    if str(job.get('destination','')).casefold()!=str(rec['destination']).casefold():raise RuntimeError('Download job destination changed.')
    if job.get('status') in {'failed','interrupted','cancelled'}:
        if step.get('status')=='submitted':commit(d,args,'unknown',{'download_job':args.download_job,'download_state':job.get('status'),'operator_id':rec['operator_id']})
        return {'ok':False,'job':job,'message':'Cloud export is already recorded. Use --resume-export to reuse its operator and partial; never create another export.'}
    if job.get('status')!='complete':return {'ok':True,'download_running':True,'job':job,'message':'One status snapshot; export was not repeated.'}
    if not path.is_file():raise RuntimeError('Downloader reported complete without a final artifact.')
    artifact=validate_artifact(path,rec['format'],rec['packaging'])
    if not artifact.get('ok'):
        if step.get('status') == 'submitted':
            commit(d,args,'failed',{'download_job':args.download_job,'validation':artifact})
        raise RuntimeError('Downloaded file failed format validation; retained for inspection, not delivered.')
    manifest={'project_id':args.project_id,'operator_id':rec['operator_id'],'download_job':args.download_job,'artifact':artifact,
        'actual_credit_debit':'not_attributed','publisher_verified':False}
    if rec.get('source_operator_id'):
        manifest['source_operator_id'] = rec['source_operator_id']
        manifest['source_check'] = 'studio_current_before_submit'
    _new_json(manifest_path,manifest)
    _finish_commit(d,args,step,manifest)
    return {'ok':True,'manifest':manifest,'manifest_path':str(manifest_path)}

def export(d,args):
    require_identity(args) if args.execute_spend or args.download_job or args.resume_export else None
    deferred = getattr(args, 'submit_only', False)
    source = getattr(args, 'source_operator_id', '')
    if source and not re.fullmatch(r'[A-Za-z0-9-]{8,64}', source):
        raise ValueError('Invalid source operator ID.')
    if deferred and (args.region != 'cn' or not args.execute_spend or args.download_job
                     or args.resume_export or args.allow_nonowner_spend or not source):
        raise ValueError('Deferred export requires CN, --execute-spend, --source-operator-id and owner-only access.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',args.task_id) or '..' in args.task_id:raise ValueError('Unsafe task-id.')
    if not re.fullmatch(r'[A-Za-z0-9-]{8,64}',args.project_id):raise ValueError('Invalid project-id.')
    if args.texture_size not in {512,1024,2048,4096,8192}:raise ValueError('Unsupported export texture size.')
    if any(not isinstance(n,str) or not n or ',' in n or len(n)>120 for n in args.animation_names):raise ValueError('Invalid retarget animation name.')
    if not (args.resume_export or args.download_job):
        if args.enable_bake_animation:raise ValueError('--enable-bake-animation is current-pose STATIC baking in Studio, not Action baking. Its pose/frame mapping is unverified; no new export. Omit it for a rigged animation export.')
        if args.animation_names and not args.with_animation:raise ValueError('Animation selection requires --with-animation.')
        if args.with_animation and not args.animation_names:raise ValueError('Animation export requires explicit --animation-names from completed project retargets.')
    root=Path(args.runtime_dir).resolve()
    record_path=safe_destination(root,args.task_id,args.step_key+'.download.json') if args.step_key and not args.download_job and not args.resume_export else root/args.task_id/(args.step_key+'.download.json')
    if not record_path.resolve().is_relative_to(root):raise ValueError('Download record escapes the runtime directory.')
    bridge=SmartDownloadBridge(state_root=root/'download-jobs')
    if args.resume_export:
        if args.execute_spend or args.download_job:raise ValueError('--resume-export cannot create another export or combine with --download-job.')
        if record_path.is_file():rec=json.loads(record_path.read_text(encoding='utf-8'))
        else:
            state=d._ledger(Path(args.ledger),'status','--task-id',args.task_id);step=state.get('steps',{}).get(args.step_key,{})
            if _recorded_params(step)!=_params(args) or step.get('status') not in {'submitted','unknown'} or not step.get('operator_id') or state.get('region')!=args.region:
                raise RuntimeError('No matching durable export operator; inspect the ledger, never re-export.')
            rec={'job_id':None,'operator_id':step['operator_id'],'project_id':args.project_id,'region':args.region,'destination':None,'format':args.model_format,'packaging':args.texture_packaging}
            if source:
                rec['source_operator_id'] = source
                filename = (step.get('evidence') or {}).get('file_name')
                if filename:
                    rec['destination'] = str(_artifact_destination(args, filename))
        if rec.get('region')!=args.region or rec.get('project_id')!=args.project_id:raise RuntimeError('Recovery receipt source/region mismatch.')
        return _resolve_recorded(d,args,record_path,bridge,rec)
    if args.download_job:
        if args.execute_spend:raise ValueError('--download-job cannot create another export.')
        return _status(d,args,record_path,bridge)
    d._ensure_daemon(args.profile);pf=d._fetch_preflight(args.profile,args.region)
    source_checked_submission = bool(source and args.execute_spend)
    if source_checked_submission:
        bridge.doctor()
        _verify_project_available(d, args)
    # Do the current-variant read after the local preparation checks, just
    # before reservation/submission. The adapter exports Studio's current
    # default, so this does not select an arbitrary historical variant.
    detail = _current_project(d, args)
    owner=detail['is_owner'];est=estimate_postprocess('export',is_owner=owner)
    if deferred and not owner:
        raise RuntimeError('Only owner exports may be submitted for deferred retrieval.')
    if source and not _matches_requested_source(detail, args):
        raise RuntimeError('The successful current Studio operator does not match the requested source; no export submitted.')
    if not args.execute_spend:return {'ok':True,'dry_run':True,'estimate':est.to_dict(),'downloads_required':1,'is_owner':owner,'preflight':pf}
    if not owner and not args.allow_nonowner_spend:raise RuntimeError('Nonowner export needs explicit approved spending scope.')
    if int(pf['quota'].get('download_quota_remaining',0))<1 and not pf['quota'].get('download_unlimited'):raise RuntimeError('No download allowance available.')
    if detail.get('status')!='success':raise RuntimeError('Source project has not completed successfully.')
    # Verify download backend before reserving a cloud operation.
    if not source_checked_submission:
        bridge.doctor()
    metadata = {'export_params': _params(args)}
    if source:
        metadata.update(export_profile=args.profile, project_id=args.project_id)
    if deferred:
        metadata['deferred_export'] = True
    reserve(d,args,est,metadata,downloads=1)
    stage='reserved';op=''
    try:
        commit(d,args,'submitting',{'reason':'one export adapter invocation about to begin'});stage='submitting'
        rows=d._extract_json(d._opencli(args.profile,*_adapter_args(args),'--confirm-export','--submit-only',timeout=90))
        if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):raise RuntimeError('Unclear export receipt.')
        row=rows[0];candidate=str(row.get('operator_id',''))
        if not re.fullmatch(r'[A-Za-z0-9-]{8,64}',candidate) or row.get('project_id')!=args.project_id or candidate==source:raise RuntimeError('Export receipt source/ID mismatch.')
        op = candidate
        if deferred and (row.get('is_owner') is not True or type(row.get('estimated_credits')) is not int
                         or row['estimated_credits'] != 0 or row.get('state') != 'submitted'
                         or row.get('format') != args.model_format):
            raise RuntimeError('Deferred export receipt did not confirm the approved zero-credit owner export.')
        evidence = {'operator_ids':[op],'project_ids':[args.project_id],'format':args.model_format}
        if source:
            evidence.update(source_operator_id=source, source_check='studio_current_before_submit')
        dest = _artifact_destination(args, row.get('file_name','')) if deferred else None
        if dest is not None:
            extension = ('.glb' if args.model_format == 'gltf' else
                         '.zip' if args.texture_packaging == 'zip' else f'.{args.model_format}')
            if dest.suffix.lower() != extension:
                raise RuntimeError('Deferred export filename extension does not match the approved format and packaging.')
            evidence['file_name'] = dest.name
        commit(d,args,'submitted',evidence,operator_id=op,project_id=args.project_id);stage='submitted'
        if deferred:
            try:
                current = _current_project(d, args)
                if current.get('is_owner') is not True or not _matches_requested_source(current, args):
                    raise RuntimeError('The Studio current source changed during export submission.')
            except Exception as exc:
                commit(d, args, 'unknown', {**evidence, 'reason': 'source_unverified_after_export_submission'})
                raise RuntimeError('Export was submitted, but the current source could not be confirmed again. Inspect its original receipt.') from exc
        if dest is None:
            dest = _artifact_destination(args, row.get('file_name',''))
        rec={'job_id':None,'operator_id':op,'project_id':args.project_id,'region':args.region,'destination':str(dest),'format':args.model_format,'packaging':args.texture_packaging}
        if source:
            rec['source_operator_id'] = source
        _new_json(record_path,rec)
        if deferred:
            return {'ok': True, 'submitted': True, 'operator_id': op, 'project_id': args.project_id,
                    'source_operator_id': source, 'record_path': str(record_path), 'destination': str(dest)}
        return _resolve_recorded(d,args,record_path,bridge,rec)
    except Exception:
        if stage=='reserved':commit(d,args,'aborted_before_submit',{'reason':'adapter not attempted'})
        elif stage != 'submitted' or not deferred:
            commit(d,args,'unknown',{'operator_ids':[op] if op else [],'reason':'export/download result uncertain; no automatic re-export'},
                   operator_id=op or None, project_id=args.project_id if op else None)
        raise
