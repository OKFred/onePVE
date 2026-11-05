"""Optional host-side SLS export. Local caps are not an Alibaba account spending cap."""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid
try:
    import fcntl
except ImportError:
    fcntl = None

if __name__ == '__main__':
    sys.dont_write_bytecode = True
import onepve_audit as audit
import onepve_sls_budget as budget
from onepve_common import STATE_ROOT, atomic_json, require_root, run
from onepve_sls_wire import SlsClient, SlsError, MAX_RAW_BYTES, MAX_ROWS, encode_logs

ROOT = STATE_ROOT / 'sls'
AGENT_STATE = STATE_ROOT / 'agent'
UNIT_ROOT = Path('/etc/systemd/system')
SOURCES = ['onepve_sls.py','onepve_sls_wire.py','onepve_sls_budget.py','onepve_audit.py','onepve_common.py']


def need(value,message):
    if not value:raise RuntimeError(message)


def _private(path,directory=False):
    audit._private(Path(path),directory)


@contextlib.contextmanager
def _lock():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);_private(ROOT,True)
    descriptor=os.open(ROOT/'operation.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        _private(ROOT/'operation.lock')
        if fcntl:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        else:need(os.name=='nt','Linux locking is unavailable')
        yield
    finally:os.close(descriptor)


def _config(allow_initializing=False):
    path=ROOT/'config.json';_private(path)
    cfg=json.loads(path.read_text())
    need(cfg.get('schema_version')==1 and isinstance(cfg.get('owner'),str)
         and re.fullmatch('[0-9a-f]{32}',cfg['owner']) and type(cfg.get('enabled')) is bool,'Invalid SLS configuration')
    need(re.fullmatch('[0-9]{16}',cfg.get('account_id','')),'Invalid SLS account identity')
    need(allow_initializing or not cfg.get('initializing'),'SLS initial configuration is incomplete; explicitly configure again')
    return cfg


def _state(cfg):
    path=ROOT/'upload-state.json';_private(path)
    state=json.loads(path.read_text())
    need(state.get('schema_version')==1 and state.get('owner')==cfg['owner']
         and state.get('destination')==_destination(cfg) and isinstance(state.get('cursors'),dict),
         'SLS upload state differs or is damaged; no automatic replay is allowed')
    return state


def _destination(cfg):
    return hashlib.sha256(json.dumps([cfg[key] for key in ['account_id','endpoint','project','logstore']]).encode()).hexdigest()


def _credentials(cfg):
    path=Path(cfg['credential_file']);need(path.is_absolute(),'SLS credential path must be absolute');_private(path)
    need(path.stat().st_size<=16384,'SLS credential file is too large')
    value=json.loads(path.read_text())
    need(isinstance(value,dict),'Invalid SLS credential file')
    return value


def _client(cfg,month=None):
    approved=budget.status(ROOT/'budget',cfg['account_id'])
    need(approved['approved'],'Verify and approve this month\'s remaining account free quota before enabling SLS')
    need(approved.get('raw_bytes_remaining',0)>0 and approved.get('requests_remaining',0)>0,'SLS host upload budget is exhausted')
    month=month or approved['month']
    return SlsClient(cfg['endpoint'],cfg['project'],cfg['logstore'],_credentials(cfg),
        lambda raw_bytes=0,request_count=1:budget.reserve(ROOT/'budget',cfg['account_id'],month,raw_bytes,request_count))


def _runtime():
    sources={name:(Path(__file__).parent/name).read_bytes() for name in SOURCES}
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in sources.items()}
    digest=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    target=ROOT/('runtime-'+digest)
    if not target.exists():
        staging=Path(tempfile.mkdtemp(prefix='.runtime-',dir=ROOT))
        try:
            for name,data in sources.items():
                descriptor=os.open(staging/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            atomic_json(staging/'manifest.json',hashes)
            staging.rename(target)
        finally:
            if staging.exists():
                need(staging.parent==ROOT and staging.resolve()==staging.absolute(),'Redirected SLS runtime staging; retained')
                shutil.rmtree(staging)
    _private(target,True)
    need({path.name for path in target.iterdir()}==set(SOURCES)|{'manifest.json'},'SLS runtime contains unowned files')
    for name,digest in hashes.items():
        _private(target/name);need(hashlib.sha256((target/name).read_bytes()).hexdigest()==digest,'SLS runtime changed')
    _private(target/'manifest.json');need(json.loads((target/'manifest.json').read_text())==hashes,'SLS runtime manifest changed')
    return target


def _units(cfg,runtime):
    marker='# onePVE SLS owner='+cfg['owner']+'\n'
    service=(marker+'[Unit]\nDescription=onePVE bounded SLS audit export\nAfter=network-online.target\n'
        '[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=yes\nProtectHome=read-only\nProtectSystem=strict\n'
        'PrivateTmp=yes\nCapabilityBoundingSet=\nRestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n'
        'ReadWritePaths='+str(ROOT)+'\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nMemoryMax=128M\nTasksMax=16\n'
        'ExecStart=/usr/bin/python3 '+str(runtime/'onepve_sls.py')+' upload\nTimeoutStartSec=120\nStandardOutput=null\n')
    timer=(marker+'[Unit]\nDescription=Poll onePVE audit for SLS\n[Timer]\nOnBootSec=2min\n'
        'OnUnitActiveSec=5min\nUnit=onepve-sls-upload.service\n[Install]\nWantedBy=timers.target\n')
    return {'onepve-sls-upload.service':service,'onepve-sls-upload.timer':timer}


def _check_units(cfg,desired=None):
    desired=desired or {};known=cfg.get('units',{});pending=cfg.get('units_pending',{})
    fixed={'onepve-sls-upload.service','onepve-sls-upload.timer'}
    for name in set(known)|set(pending)|set(desired)|fixed:
        need(name in {'onepve-sls-upload.service','onepve-sls-upload.timer'},'Invalid SLS unit identity')
        path=UNIT_ROOT/name
        if not os.path.lexists(path):continue
        need(name in set(known)|set(pending)|set(desired),'Unowned SLS unit already exists; no service action was performed')
        audit._unit_file(path);digest=hashlib.sha256(path.read_bytes()).hexdigest()
        need(digest in {known.get(name),pending.get(name),desired.get(name)},'SLS unit changed externally; retained')


def _stop(cfg):
    _check_units(cfg)
    for name in ['onepve-sls-upload.timer','onepve-sls-upload.service']:
        if (UNIT_ROOT/name).exists():
            command=['systemctl','disable','--now',name] if name.endswith('.timer') else ['systemctl','stop',name]
            run(command)


def configure(account_id,endpoint,project,logstore,credential_file):
    """Local configuration only: no cloud resources or requests, always disabled."""
    require_root()
    with _lock():
        previous=_config(True) if (ROOT/'config.json').exists() else None
        if previous:need(previous['account_id']==account_id,'SLS account cannot change over an existing host budget')
        cfg=dict(schema_version=1,owner=previous['owner'] if previous else uuid.uuid4().hex,
            account_id=account_id,endpoint=endpoint,project=project,logstore=logstore,
            credential_file=str(Path(credential_file).absolute()),enabled=False,
            initializing=previous is None or previous.get('initializing',False),
            units=(previous or {}).get('units',{}),units_pending=(previous or {}).get('units_pending',{}))
        need(isinstance(account_id,str) and re.fullmatch('[0-9]{16}',account_id),'Use the actual 16-digit account ID')
        SlsClient(endpoint,project,logstore,_credentials(cfg),lambda **unused:None)
        if (ROOT/'upload-state.json').exists():
            old=_state(previous or cfg)
            need(old['destination']==_destination(cfg),'Changing the SLS destination requires a separate reviewed migration; cursors and budgets were retained')
            state=old
        else:
            need(previous is None or previous.get('initializing') and not previous['enabled'] and not previous.get('units')
                 and not previous.get('units_pending') and not (ROOT/'budget').exists(),
                 'SLS cursor state is missing; retained configuration must be inspected')
            state=dict(schema_version=1,owner=cfg['owner'],destination=_destination(cfg),cursors={},halt=None,pending=None)
        if previous:_stop(previous)
        atomic_json(ROOT/'config.json',cfg);atomic_json(ROOT/'upload-state.json',state)
        cfg['initializing']=False;atomic_json(ROOT/'config.json',cfg)
        return status()


def approve_budget(month=None,raw_mib=100,requests=100000,verified=False):
    require_root();need(verified is True,'First verify the account\'s shared remaining free quota; an upload cap does not guarantee zero fees')
    with _lock():
        cfg=_config();directory=ROOT/'budget'
        function=budget.approve_month if directory.exists() else budget.init_budget
        return function(directory,cfg['account_id'],month,raw_mib*1024**2,requests)


def enable():
    require_root()
    with _lock():
        cfg=_config();state=_state(cfg)
        _stop(cfg);cfg['enabled']=False;atomic_json(ROOT/'config.json',cfg)
        client=_client(cfg);proof=client.preflight()
        need(proof.get('project_owner')==cfg['account_id'],'SLS Project owner differs from the approved budget account')
        runtime=_runtime();units=_units(cfg,runtime)
        desired={name:hashlib.sha256(text.encode()).hexdigest() for name,text in units.items()}
        _check_units(cfg,desired);cfg['units_pending']=desired;atomic_json(ROOT/'config.json',cfg)
        for name,text in units.items():
            # The private, deterministic complete file is safe to republish after an interrupted unit install.
            path=UNIT_ROOT/name
            descriptor,temp=tempfile.mkstemp(prefix='.onepve-sls-',dir=UNIT_ROOT)
            try:
                with os.fdopen(descriptor,'w',newline='\n') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
                os.chmod(temp,0o644);os.replace(temp,path)
            finally:Path(temp).unlink(missing_ok=True)
        cfg.update(units=desired,units_pending={},enabled=True,runtime=str(runtime))
        state.update(halt=None,pending=None);atomic_json(ROOT/'upload-state.json',state);atomic_json(ROOT/'config.json',cfg)
        try:
            run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','onepve-sls-upload.timer'])
        except Exception:
            cfg['enabled']=False;atomic_json(ROOT/'config.json',cfg)
            state['halt']=dict(reason='EnableServiceFailed',observed_utc=audit.utc());atomic_json(ROOT/'upload-state.json',state)
            raise
        return status()


def disable():
    require_root()
    if not (ROOT/'config.json').exists():return {'status':'DISABLED','enabled':False}
    with _lock():
        cfg=_config();cfg['enabled']=False;atomic_json(ROOT/'config.json',cfg);_stop(cfg)
        return status()


def _rows(state,cursor,limit=MAX_ROWS):
    cfg=audit._load(state)
    if not cfg or not cfg.get('enabled'):return []
    path=audit._paths(state['vmid'])[2]
    if not path.exists():return []
    need(isinstance(cursor,list) and len(cursor)==2 and isinstance(cursor[0],str) and type(cursor[1]) is int,'Invalid SLS source cursor')
    cutoff=audit.utc(datetime.now(timezone.utc)-timedelta(hours=audit.RETENTION_HOURS))
    with contextlib.closing(audit._database(path)) as db:
        return [dict(row) for row in db.execute('SELECT id,'+','.join(audit.FIELDS)+
            ' FROM flows WHERE last_seen_utc>=? AND (last_seen_utc>? OR (last_seen_utc=? AND id>?))'
            ' ORDER BY last_seen_utc,id LIMIT ?',(cutoff,cursor[0],cursor[0],cursor[1],limit))]


def _event(cfg,state,row):
    contents={key:'' if row[key] is None else str(row[key]) for key in audit.FIELDS}
    contents.update(vmid=str(state['vmid']),vm_owner=state['owner'],host_id=cfg['owner'])
    contents['event_id']=hashlib.sha256(json.dumps([contents,row['id']],sort_keys=True).encode()).hexdigest()
    timestamp=datetime.fromisoformat(row['last_seen_utc'])
    need(timestamp.tzinfo is not None,'Audit observation has no timezone')
    return {'time':int(timestamp.timestamp()),'contents':contents}


def _error_code(error):
    code=getattr(error,'code',None)
    if isinstance(code,str) and re.fullmatch('[A-Za-z0-9_.-]{1,128}',code):return code
    status=getattr(error,'status',None)
    return type(error).__name__+('_HTTP'+str(status) if type(status) is int and 100<=status<=599 else '')


def upload():
    require_root()
    if not (ROOT/'config.json').exists():return {'status':'DISABLED','enabled':False}
    with _lock():
        cfg=_config();state=_state(cfg)
        if not cfg['enabled']:return {'status':'DISABLED','enabled':False}
        if state.get('halt'):return dict(status='HALTED',halt=state['halt'])
        if state.get('pending'):
            state['halt']=dict(reason='PreviousUploadUncertain',observed_utc=audit.utc())
            atomic_json(ROOT/'upload-state.json',state)
            return dict(status='HALTED',halt=state['halt'])
        try:
            client=_client(cfg);validated=False;count=0
            for path in sorted(AGENT_STATE.glob('*/state.json')):
                _private(path);source=json.loads(path.read_text())
                need(type(source.get('vmid')) is int and str(source['vmid'])==path.parent.name,'Invalid audit source identity')
                key=str(source['vmid'])+':'+source['owner']
                rows=_rows(source,state['cursors'].get(key,['',0]));logs=[];sent=[];raw_size=0
                for row in rows:
                    event=_event(cfg,source,row);size=len(encode_logs([event]))
                    if raw_size+size>MAX_RAW_BYTES:break
                    logs.append(event);sent.append(row);raw_size+=size
                need(not rows or logs,'One audit event exceeds the bounded SLS batch size')
                if not logs:continue
                if not validated:
                    proof=client.preflight()
                    need(proof.get('project_owner')==cfg['account_id'],'SLS Project owner differs from the approved account')
                    validated=True
                state['pending']=dict(source=key,cursor=[sent[-1]['last_seen_utc'],sent[-1]['id']],rows=len(logs))
                atomic_json(ROOT/'upload-state.json',state)
                result=client.put_logs(logs)
                need(isinstance(result,dict) and result.get('rows')==len(logs),'SLS did not confirm the complete batch')
                updated=dict(state,cursors=dict(state['cursors']),pending=None,last_success_utc=audit.utc())
                updated['cursors'][key]=[sent[-1]['last_seen_utc'],sent[-1]['id']]
                atomic_json(ROOT/'upload-state.json',updated);state=updated;count+=len(logs)
            return dict(status='UPLOADED',rows=count,last_success_utc=state.get('last_success_utc'))
        except Exception as error:
            state['halt']=dict(reason=_error_code(error),observed_utc=audit.utc())
            atomic_json(ROOT/'upload-state.json',state)
            raise RuntimeError('SLS upload halted: '+state['halt']['reason']+'; local audit remains available') from None


def status():
    require_root()
    if not (ROOT/'config.json').exists():return {'status':'DISABLED','enabled':False,'configured':False}
    cfg=_config();state=_state(cfg)
    result=dict(status='HALTED' if state.get('halt') else 'ENABLED' if cfg['enabled'] else 'DISABLED',
        enabled=cfg['enabled'],configured=True,account_id=cfg['account_id'],endpoint=cfg['endpoint'],
        project=cfg['project'],logstore=cfg['logstore'],halt=state.get('halt'),pending=state.get('pending'),last_success_utc=state.get('last_success_utc'),
        cost_scope='This host only; not an account spending cap; shared cloud free quota must be verified each month.')
    try:result['budget']=budget.status(ROOT/'budget',cfg['account_id'])
    except (RuntimeError,OSError,ValueError):result['budget']={'approved':False,'status':'UNAVAILABLE'}
    if cfg['enabled']:
        try:
            _check_units(cfg)
            result['units_match']=set(cfg.get('units',{}))=={'onepve-sls-upload.service','onepve-sls-upload.timer'} and not cfg.get('units_pending') and all((UNIT_ROOT/name).is_file() for name in cfg['units'])
            result['timer_active']=run(['systemctl','is-active','--quiet','onepve-sls-upload.timer'],check=False).returncode==0
        except (RuntimeError,OSError,ValueError):result.update(units_match=False,timer_active=False)
        if not result['units_match'] or not result['timer_active'] or not result['budget'].get('approved'):result['status']='ATTENTION'
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='action',required=True)
    command=commands.add_parser('configure');command.add_argument('--account-id',required=True);command.add_argument('--endpoint',required=True)
    command.add_argument('--project',required=True);command.add_argument('--logstore',required=True);command.add_argument('--credential-file',type=Path,required=True)
    command=commands.add_parser('approve-budget');command.add_argument('--month');command.add_argument('--raw-mib',type=int,default=100)
    command.add_argument('--requests',type=int,default=100000);command.add_argument('--account-free-verified',action='store_true')
    for action in ['status','enable','disable','upload']:commands.add_parser(action)
    args=parser.parse_args(argv)
    if args.action=='configure':result=configure(args.account_id,args.endpoint,args.project,args.logstore,args.credential_file)
    elif args.action=='approve-budget':result=approve_budget(args.month,args.raw_mib,args.requests,args.account_free_verified)
    else:result=globals()[args.action]()
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as error:
        print(json.dumps({'status':'ERROR','error':'SLS operation failed: '+_error_code(error)}),file=sys.stderr);sys.exit(1)
