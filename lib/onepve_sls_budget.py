"""Host-local SLS attempt budgets, not an Alibaba Cloud account spending cap.

Host root can delete or roll back this ledger; never treat it as tamper proof.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
try:
    import fcntl
except ImportError:
    fcntl = None

from onepve_common import atomic_json

ROOT = Path('/var/lib/onepve/sls/budget')
MAX_RAW = 200 * 1024 ** 2
MAX_REQUESTS = 100000
DEFAULT_RAW = 100 * 1024 ** 2


class CostLimitError(RuntimeError):
    pass


def need(value, message):
    if not value:raise CostLimitError(message)


def _month(value=None):
    current=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m')
    need(value is None or value==current,'Only the current Beijing calendar month can be approved or reserved')
    return current


def _attempt_month(value):
    now=datetime.now(timezone(timedelta(hours=8)))
    need((now+timedelta(minutes=5)).strftime('%Y-%m')==now.strftime('%Y-%m'),
         'Month-end safety window; cloud upload is paused until the next explicit month approval')
    return _month(value)


def _account(account_id):
    need(isinstance(account_id,str) and re.fullmatch(r'[0-9]{16}',account_id),'Use the actual 16-digit Alibaba Cloud account ID')


def _private(path, directory=False):
    info=path.lstat()
    need(not path.is_symlink() and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink==1),
         'Budget path is redirected or not a private regular resource')
    if os.name=='posix':need(info.st_uid==0 and not info.st_mode&0o077,'Budget resources must be root private')


def _directory(directory):
    directory=Path(directory).absolute()
    need(directory.resolve()==directory,'Budget directory is redirected')
    _private(directory,True)
    return directory


def _save(path, data):
    atomic_json(path,data)
    if os.name=='posix':
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)


@contextlib.contextmanager
def _lock(directory):
    need(fcntl is not None,'SLS budgeting requires Linux file locking')
    descriptor=os.open(directory/'operation.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        _private(directory/'operation.lock')
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        yield
    finally:os.close(descriptor)


@contextlib.contextmanager
def _closed():
    try:yield
    except CostLimitError:raise
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise CostLimitError('SLS budget unavailable or corrupt; no upload is authorized') from error


def _limits(raw_limit, request_limit):
    need(type(raw_limit) is int and 1<=raw_limit<=MAX_RAW,'Raw byte budget exceeds the hard ceiling or is invalid')
    need(type(request_limit) is int and 1<=request_limit<=MAX_REQUESTS,'Request budget exceeds the hard ceiling or is invalid')


def _checksum(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _write_ledger(directory,data):
    _save(directory/'budget.json',dict(data,sha256=_checksum(data)))


def _read(directory, account_id):
    _account(account_id)
    for name in ['identity.json','budget.json']:_private(directory/name)
    identity=json.loads((directory/'identity.json').read_text())
    need(identity==dict(schema_version=1,account_id=account_id),'Budget account identity differs; it cannot be reset by changing credentials')
    data=json.loads((directory/'budget.json').read_text())
    need(isinstance(data,dict),'Invalid budget ledger')
    digest=data.pop('sha256',None)
    need(isinstance(digest,str) and digest==_checksum(data),'Budget ledger checksum differs; no upload is authorized')
    need(set(data)=={'schema_version','account_id','sequence','months'} and data['schema_version']==1
         and data['account_id']==account_id and type(data['sequence']) is int and 0<=data['sequence']<2**63
         and isinstance(data['months'],dict),'Invalid budget ledger identity/schema')
    for month,record in data['months'].items():
        need(re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])',month) and isinstance(record,dict)
             and set(record)=={'raw_limit','request_limit','raw_bytes_used','requests_used'},'Invalid budget month record')
        _limits(record['raw_limit'],record['request_limit'])
        need(type(record['raw_bytes_used']) is int and 0<=record['raw_bytes_used']<=record['raw_limit']
             and type(record['requests_used']) is int and 0<=record['requests_used']<=record['request_limit'], 'Invalid budget usage')
    return data


def _view(data, month):
    record=data['months'].get(month)
    result=dict(account_id=data['account_id'],month=month,approved=record is not None,sequence=data['sequence'])
    if record:
        result.update(record,raw_bytes_remaining=record['raw_limit']-record['raw_bytes_used'],
                      requests_remaining=record['request_limit']-record['requests_used'])
    return result


def init_budget(directory, account_id, month=None, raw_limit=DEFAULT_RAW, request_limit=MAX_REQUESTS):
    """Explicit initial approval only; an identity anchor prevents missing-ledger resets."""
    with _closed():
        _account(account_id);month=_month(month);_limits(raw_limit,request_limit)
        directory=Path(directory).absolute()
        need(directory.parent.resolve()==directory.parent,'Budget parent is redirected')
        if not directory.exists():directory.mkdir(mode=0o700,parents=True)
        directory=_directory(directory)
        with _lock(directory):
            need({path.name for path in directory.iterdir()}=={'operation.lock'},'Budget already initialized or incomplete; never initialize over existing evidence')
            _save(directory/'identity.json',dict(schema_version=1,account_id=account_id))
            data=dict(schema_version=1,account_id=account_id,sequence=0,months={month:
                dict(raw_limit=raw_limit,request_limit=request_limit,raw_bytes_used=0,requests_used=0)})
            _write_ledger(directory,data)
            return _view(data,month)


def approve_month(directory, account_id, month=None, raw_limit=DEFAULT_RAW, request_limit=MAX_REQUESTS):
    with _closed():
        month=_month(month);_limits(raw_limit,request_limit);directory=_directory(directory)
        with _lock(directory):
            data=_read(directory,account_id)
            need(month not in data['months'],'This month was already approved; no reset or top-up is allowed')
            data['months'][month]=dict(raw_limit=raw_limit,request_limit=request_limit,raw_bytes_used=0,requests_used=0)
            _write_ledger(directory,data)
            return _view(data,month)


def reserve(directory, account_id, month, raw_bytes, request_count=1, raw_limit=None, request_limit=None):
    """Persist before a network attempt. Failed/unknown attempts are never refunded."""
    with _closed():
        month=_attempt_month(month);directory=_directory(directory)
        need(type(raw_bytes) is int and 0<=raw_bytes<=MAX_RAW and type(request_count) is int and 1<=request_count<=MAX_REQUESTS,'Invalid attempt size/count')
        with _lock(directory):
            _attempt_month(month)
            data=_read(directory,account_id);record=data['months'].get(month)
            need(record is not None,'This month has no explicit upload budget approval')
            raw_limit=record['raw_limit'] if raw_limit is None else raw_limit
            request_limit=record['request_limit'] if request_limit is None else request_limit
            _limits(raw_limit,request_limit)
            need(raw_limit<=record['raw_limit'] and request_limit<=record['request_limit'],'Existing approval cannot be increased')
            need(record['raw_bytes_used']+raw_bytes<=raw_limit and record['requests_used']+request_count<=request_limit,'SLS upload budget exhausted; keep local audit only')
            record['raw_bytes_used']+=raw_bytes;record['requests_used']+=request_count;data['sequence']+=1
            _write_ledger(directory,data)
            _attempt_month(month)
            return _view(data,month)


def status(directory, account_id):
    """Read only; never creates a lock, approves a new month, or contacts SLS."""
    with _closed():return _view(_read(_directory(directory),account_id),_month())
