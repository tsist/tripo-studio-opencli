"""One budgeted export; later queries only resume the recorded download job."""
from __future__ import annotations
import json,re,os,uuid
from pathlib import Path
from tripo_actions import require_identity,reserve,commit
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
    return {k:getattr(args,k) for k in ('project_id','model_format','fbx_preset','texture_packaging','texture_size','name','with_animation','enable_bake_animation','animation_names','pivot_to_center_bottom','pack_uv')}

def _recorded_params(step):
    stored=step.get('metadata',{}).get('export_params')
    if not isinstance(stored,dict):return None
    # Added selectors default to the former request body. Old receipts remain
    # resumable; no new export is allowed through this normalization.
    return {'animation_names':[],'pivot_to_center_bottom':False,'pack_uv':False,**stored}

_MANIFEST_ARTIFACT_FIELDS=('path','format','packaging','kind','bytes','sha256','sha256_scope','publisher_verified','validation_scope','asset_usability_verified','errors','details')

def _confined(path,root):
    try:return Path(path).resolve().is_relative_to(root)
    except (OSError,RuntimeError,ValueError):return False

def _verify_existing_manifest(manifest,args,rec,step,root):
    if not isinstance(manifest,dict) or not isinstance(step,dict):raise RuntimeError('Export manifest/ledger state is corrupt; no success committed.')
    artifact=manifest.get('artifact')
    if not isinstance(artifact,dict):raise RuntimeError('Export manifest has no artifact receipt; no success committed.')
    project=args.project_id
    if manifest.get('project_id')!=project or rec.get('project_id')!=project or step.get('project_id')!=project:
        raise RuntimeError('Export manifest project does not match the recorded step; no success committed.')
    operator=rec.get('operator_id')
    if not isinstance(operator,str) or not operator or manifest.get('operator_id')!=operator or step.get('operator_id')!=operator:
        raise RuntimeError('Export manifest operator does not match the recorded step; no success committed.')
    if rec.get('job_id')!=args.download_job or manifest.get('download_job')!=args.download_job:
        raise RuntimeError('Export manifest download job does not match the recorded step; no success committed.')
    if _recorded_params(step)!=_params(args):
        raise RuntimeError('Export parameters differ from the recorded step; no success committed.')
    if rec.get('format')!=args.model_format or rec.get('packaging')!=args.texture_packaging:
        raise RuntimeError('Download record format/packaging differs from the recorded export; no success committed.')
    destination=rec.get('destination')
    if not isinstance(destination,str) or not destination:raise RuntimeError('Download record has no artifact destination; no success committed.')
    path=Path(destination)
    if not path.is_absolute() or not _confined(path,root):
        raise RuntimeError('Recorded artifact path is outside the runtime directory; no success committed.')
    if artifact.get('path')!=destination:
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

def _resolve_recorded(d,args,record_path,bridge,rec):
    state=d._ledger(Path(args.ledger),'status','--task-id',args.task_id)
    step=state.get('steps',{}).get(args.step_key,{})
    if _recorded_params(step)!=_params(args):raise RuntimeError('Recovery parameters differ from the recorded export; no operation performed.')
    if step.get('status') not in {'submitted','unknown'}:raise RuntimeError('Only a submitted/unknown recorded export can be resolved.')
    if not rec or rec.get('operator_id')!=step.get('operator_id'):raise RuntimeError('No matching durable export receipt to recover.')
    if rec.get('job_id'):
        job=bridge.status(rec['job_id'])
        if job.get('status') not in {'failed','interrupted','cancelled'}:
            return {'ok':True,'job':job,'message':'Existing download is active or complete; use --download-job, not another export.'}
    named=['--name',Path(rec['destination']).stem] if rec.get('destination') else []
    rows=d._extract_json(d._opencli(args.profile,*_adapter_args(args),*named,'--resume-operator-id',rec['operator_id'],timeout=220))
    if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('operator_id')!=rec['operator_id'] or rows[0].get('project_id')!=args.project_id:raise RuntimeError('Export resolution did not match its durable receipt.')
    row=rows[0]
    if not rec.get('destination'):
        rec['destination']=str(safe_destination(Path(args.runtime_dir).resolve(),args.task_id,str(row.get('file_name',''))))
        _update_json(record_path,rec)
    dest=Path(rec['destination'])
    if row.get('file_name')!=dest.name:raise RuntimeError('Resolved export filename changed; no download started.')
    job=bridge.start(row['model_url'],dest)
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
    if manifest_path.exists():
        try:manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:raise RuntimeError('Export manifest is unreadable or corrupt; no success committed.') from exc
        if not isinstance(step,dict) or step.get('status') not in {'submitted','unknown','success'}:
            raise RuntimeError('Manifest/ledger state conflict; inspect without re-exporting.')
        _verify_existing_manifest(manifest,args,rec,step,Path(args.runtime_dir).resolve())
        if step.get('status') in {'submitted','unknown'}:_finish_commit(d,args,step,manifest)
        return {'ok':True,'already_verified':True,'manifest':manifest}
    job=bridge.status(args.download_job)
    if str(job.get('destination','')).casefold()!=str(rec['destination']).casefold():raise RuntimeError('Download job destination changed.')
    if job.get('status') in {'failed','interrupted','cancelled'}:
        if step.get('status')=='submitted':commit(d,args,'unknown',{'download_job':args.download_job,'download_state':job.get('status'),'operator_id':rec['operator_id']})
        return {'ok':False,'job':job,'message':'Cloud export is already recorded. Use --resume-export to reuse its operator and partial; never create another export.'}
    if job.get('status')!='complete':return {'ok':True,'download_running':True,'job':job,'message':'One status snapshot; export was not repeated.'}
    path=Path(rec['destination'])
    if not path.is_file():raise RuntimeError('Downloader reported complete without a final artifact.')
    artifact=validate_artifact(path,rec['format'],rec['packaging'])
    if not artifact.get('ok'):
        commit(d,args,'failed',{'download_job':args.download_job,'validation':artifact})
        raise RuntimeError('Downloaded file failed format validation; retained for inspection, not delivered.')
    manifest={'project_id':args.project_id,'operator_id':rec['operator_id'],'download_job':args.download_job,'artifact':artifact,
        'actual_credit_debit':'not_attributed','publisher_verified':False}
    _new_json(manifest_path,manifest)
    _finish_commit(d,args,step,manifest)
    return {'ok':True,'manifest':manifest,'manifest_path':str(manifest_path)}

def export(d,args):
    require_identity(args) if args.execute_spend or args.download_job or args.resume_export else None
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
        d._ensure_daemon(args.profile)
        if record_path.is_file():rec=json.loads(record_path.read_text(encoding='utf-8'))
        else:
            state=d._ledger(Path(args.ledger),'status','--task-id',args.task_id);step=state.get('steps',{}).get(args.step_key,{})
            if _recorded_params(step)!=_params(args) or step.get('status') not in {'submitted','unknown'} or not step.get('operator_id') or state.get('region')!=args.region:
                raise RuntimeError('No matching durable export operator; inspect the ledger, never re-export.')
            rec={'job_id':None,'operator_id':step['operator_id'],'project_id':args.project_id,'region':args.region,'destination':None,'format':args.model_format,'packaging':args.texture_packaging}
            _new_json(record_path,rec)
        if rec.get('region')!=args.region or rec.get('project_id')!=args.project_id:raise RuntimeError('Recovery receipt source/region mismatch.')
        return _resolve_recorded(d,args,record_path,bridge,rec)
    if args.download_job:
        if args.execute_spend:raise ValueError('--download-job cannot create another export.')
        return _status(d,args,record_path,bridge)
    d._ensure_daemon(args.profile);pf=d._fetch_preflight(args.profile,args.region)
    detail=d._extract_json(d._opencli(args.profile,'tripo-studio','project',args.project_id,'--region',args.region,'-f','json'))
    if not isinstance(detail,list) or len(detail)!=1 or type(detail[0].get('is_owner')) is not bool:raise RuntimeError('Ownership could not be established.')
    owner=detail[0]['is_owner'];est=estimate_postprocess('export',is_owner=owner)
    if not args.execute_spend:return {'ok':True,'dry_run':True,'estimate':est.to_dict(),'downloads_required':1,'is_owner':owner,'preflight':pf}
    if not owner and not args.allow_nonowner_spend:raise RuntimeError('Nonowner export needs explicit approved spending scope.')
    if int(pf['quota'].get('download_quota_remaining',0))<1 and not pf['quota'].get('download_unlimited'):raise RuntimeError('No download allowance available.')
    if detail[0].get('status')!='success':raise RuntimeError('Source project has not completed successfully.')
    # Verify download backend before reserving a cloud operation.
    bridge.doctor()
    reserve(d,args,est,{'export_params':_params(args)},downloads=1)
    stage='reserved';op=''
    try:
        commit(d,args,'submitting',{'reason':'one export adapter invocation about to begin'});stage='submitting'
        rows=d._extract_json(d._opencli(args.profile,*_adapter_args(args),'--confirm-export','--submit-only',timeout=90))
        if not isinstance(rows,list) or len(rows)!=1:raise RuntimeError('Unclear export receipt.')
        row=rows[0];op=str(row.get('operator_id',''))
        if not re.fullmatch(r'[A-Za-z0-9-]{8,64}',op) or row.get('project_id')!=args.project_id:raise RuntimeError('Export receipt source/ID mismatch.')
        commit(d,args,'submitted',{'operator_ids':[op],'project_ids':[args.project_id],'format':args.model_format},operator_id=op,project_id=args.project_id);stage='submitted'
        dest=safe_destination(root,args.task_id,str(row.get('file_name','')))
        rec={'job_id':None,'operator_id':op,'project_id':args.project_id,'region':args.region,'destination':str(dest),'format':args.model_format,'packaging':args.texture_packaging}
        _new_json(record_path,rec)
        return _resolve_recorded(d,args,record_path,bridge,rec)
    except Exception:
        if stage=='reserved':commit(d,args,'aborted_before_submit',{'reason':'adapter not attempted'})
        else:commit(d,args,'unknown',{'operator_ids':[op] if op else [],'reason':'export/download result uncertain; no automatic re-export'})
        raise
