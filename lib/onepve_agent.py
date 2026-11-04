"""Create/rebuild a dedicated Debian agent VM; never prepare host GPU drivers."""
import argparse
import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import struct
import subprocess
import sys
import tarfile
import time
import uuid
try:
    import fcntl
except ImportError:
    fcntl = None

import onepve_artifacts as artifacts
import onepve_gpu as gpu
from onepve_common import (STATE_ROOT, Qga, atomic_json, node_name, pve_get,
                          progress, progress_session, repo_root, require_root, run, vm_config)

AGENT_STATE = STATE_ROOT / 'agent'
VM_CONFIGS = Path('/etc/pve/qemu-server')
PVE_NODES = Path('/etc/pve/nodes')
FINISHED = {'READY_GPU', 'READY_SOFTWARE', 'GPU_FAILED_SOFTWARE_READY'}
IN_PROGRESS = {'VM_CREATING', 'DISK_IMPORTING', 'DISK_CONFIGURING', 'CLOUD_CONFIGURING',
               'CLOUD_WAIT', 'PAYLOAD_UPLOADING', 'INSTALL_RUNNING', 'BASE_VERIFYING',
               'BASE_READY', 'GUEST_DRIVER_RUNNING', 'GUEST_DRIVER_READY', 'GPU_ATTACHING',
               'GPU_BOOTING', 'GPU_MODE_SWITCHING', 'GPU_VERIFYING', 'GPU_FALLBACK'}
PHASE_LABELS = {
    'PLANNED': '准备部署工作机', 'VM_CREATING': '创建工作机 VM', 'VM_CREATED': 'VM 已创建',
    'DISK_IMPORTING': '导入 Debian 系统镜像', 'DISK_CONFIGURING': '配置系统磁盘', 'DISK_READY': '系统磁盘已准备',
    'CLOUD_CONFIGURING': '配置 Cloud-init 和网络', 'CLOUD_WAIT': '等待 VM 启动和 Cloud-init 完成',
    'CLOUD_READY': 'Debian 基础系统已就绪', 'PAYLOAD_UPLOADING': '上传工作环境安装文件', 'PAYLOAD_READY': '安装文件已上传',
    'INSTALL_RUNNING': '安装桌面、浏览器和 Node.js', 'BASE_VERIFYING': '验证基础工作环境', 'BASE_READY': '基础工作环境已就绪',
    'GUEST_DRIVER_RUNNING': '编译并安装客机核显驱动', 'GUEST_DRIVER_READY': '客机核显驱动已准备',
    'GPU_ATTACHING': '接入已准备的显卡设备', 'GPU_BOOTING': '等待 GPU 模式重启', 'GPU_MODE_SWITCHING': '切换 Wayland 桌面',
    'GPU_VERIFYING': '验证原生 EGL 和浏览器硬件加速', 'READY_GPU': 'GPU 工作机验收完成', 'READY_SOFTWARE': '软件桌面工作机验收完成',
    'GPU_FALLBACK': 'GPU 验收未完成，正在恢复软件桌面', 'GPU_FAILED_SOFTWARE_READY': 'GPU 失败，软件桌面已恢复可用',
    'REMOVE_REQUESTED': '准备删除指定工作机', 'DELETING': '删除指定工作机', 'DELETED': '指定 VM 已删除', 'REMOVED': '工作机专属资源已移除',
}


def need(value, message):
    if not value: raise RuntimeError(message)


def _digest(path, algorithm='sha256'):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def _state_path(vmid):
    return AGENT_STATE / str(int(vmid)) / 'state.json'


def _save(state, phase=None, **changes):
    if phase is not None: state['phase'] = phase
    state.update(changes, updated_utc=datetime.now(timezone.utc).isoformat())
    atomic_json(_state_path(state['vmid']), state)
    if phase is not None:
        progress('VM %s：%s' % (state['vmid'], PHASE_LABELS.get(phase, '更新部署状态')),
                 '宿主状态：' + str(_state_path(state['vmid'])))


@contextlib.contextmanager
def _lock(vmid, dry_run=False):
    if dry_run:
        yield; return
    require_root(); need(fcntl is not None, 'VM provisioning requires Linux')
    directory = _state_path(vmid).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    need(directory.resolve() == directory and directory.stat().st_uid == 0
         and not directory.stat().st_mode & 0o077, 'Agent state must be a root-private real directory')
    descriptor = os.open(directory/'operation.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB); yield


def public_key(value):
    need(isinstance(value,str) and '\n' not in value and '\r' not in value and '\0' not in value, 'Use one SSH public-key line')
    parts=value.strip().split(None,2)
    need(len(parts)>=2 and parts[0] in {'ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256','ecdsa-sha2-nistp384','ecdsa-sha2-nistp521'},
         'Only a public OpenSSH key is accepted; never supply a private key')
    try:
        decoded=base64.b64decode(parts[1],validate=True)
        offset=0
        def field():
            nonlocal offset
            length=struct.unpack('>I',decoded[offset:offset+4])[0];offset+=4
            need(0<length<=16384 and offset+length<=len(decoded),'Invalid SSH public-key field length')
            result=decoded[offset:offset+length];offset+=length;return result
        encoded_type=field().decode('ascii')
        need(encoded_type==parts[0],'SSH public-key type and wire payload differ')
        if encoded_type=='ssh-ed25519':need(len(field())==32,'Invalid Ed25519 public key')
        elif encoded_type=='ssh-rsa':
            exponent,modulus=field(),field();need(len(exponent)<=8 and len(modulus)>=256,'Invalid or short RSA public key')
        else:
            curve=field().decode('ascii');point=field();bits=int(encoded_type.rsplit('p',1)[1])
            need(curve=='nistp'+str(bits) and len(point)==1+2*((bits+7)//8) and point[0]==4,'Invalid ECDSA public key')
        need(offset==len(decoded),'Unexpected trailing SSH public-key wire data')
    except (ValueError,UnicodeDecodeError,struct.error) as error:
        raise RuntimeError('Invalid SSH public-key wire data') from error
    return parts[0]+' '+parts[1]+(' '+parts[2] if len(parts)>2 else '')


def normalize(values):
    values=dict(values)
    defaults=dict(name='agent-browser',cores=4,memory=8192,disk=32,gpu='auto',viewer_port=16080,
                  mac=None,expected_ip=None,snippets_storage=None,address=None,gateway=None,nameserver=None)
    for key,value in defaults.items(): values.setdefault(key,value)
    for field,minimum,maximum in [('vmid',100,999999999),('cores',4,1024),('memory',8192,10485760),('disk',32,1048576),('viewer_port',1024,65535)]:
        need(type(values.get(field)) is int and minimum<=values[field]<=maximum, 'Invalid/minimum resource: '+field)
    need(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,62}',values['name']), 'Invalid VM name')
    for field in ['storage','bridge']:
        need(isinstance(values.get(field),str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',values[field]), 'Invalid '+field)
    if values['snippets_storage']:
        need(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',values['snippets_storage']), 'Invalid snippets storage')
    if values['mac']:
        need(re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',values['mac']), 'Invalid MAC address')
        need(not int(values['mac'].split(':')[0],16)&1, 'Use a unicast MAC address')
        values['mac']=values['mac'].upper()
    if values['expected_ip']:
        address=ipaddress.ip_address(values['expected_ip']);need(address.version==4,'Expected address must be IPv4')
        values['expected_ip']=str(address)
    for field in ['gateway','nameserver']:
        if values[field]:
            try:address=ipaddress.IPv4Address(values[field])
            except (ValueError,TypeError) as error:raise RuntimeError(field+' must be one IPv4 address') from error
            need(not(address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved),field+' is not a usable network address')
            values[field]=str(address)
    if values['address']:
        need(isinstance(values['address'],str) and '/' in values['address'],'Static address requires an explicit IPv4 CIDR')
        try:interface=ipaddress.IPv4Interface(values['address'])
        except ValueError as error:raise RuntimeError('Static address must be an IPv4 CIDR') from error
        address=interface.ip;network=interface.network
        need(not(address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved),'Static address is not a usable host address')
        need(network.prefixlen>=31 or address not in {network.network_address,network.broadcast_address},'Static address is the network/broadcast address')
        need(values['gateway'],'Static address requires --gateway')
        gateway=ipaddress.IPv4Address(values['gateway'])
        need(gateway in network and gateway!=address,'Static gateway must be another address in the same subnet')
        need(network.prefixlen>=31 or gateway not in {network.network_address,network.broadcast_address},'Gateway is the network/broadcast address')
        need(not values['expected_ip'] or values['expected_ip']==str(address),'Expected IP must match the static address')
        values['address']=str(interface);values['expected_ip']=str(address)
    else:need(not values['gateway'],'--gateway requires a static --address CIDR')
    values['ssh_key']=public_key(values['ssh_key'])
    need(values['gpu'] in {'auto','none'} or gpu.canonical_bdf(values['gpu']), 'Invalid GPU selection')
    if values['gpu'] not in {'auto','none'}: values['gpu']=gpu.canonical_bdf(values['gpu'])
    return values


def _exists(vmid):
    return (VM_CONFIGS/(str(vmid)+'.conf')).exists() or any(PVE_NODES.glob('*/qemu-server/'+str(vmid)+'.conf'))


def _resources(config):
    node=node_name()
    storages=pve_get('/nodes/'+node+'/storage')
    images=[s for s in storages if s.get('storage')==config['storage'] and s.get('active') and s.get('enabled',1)
            and 'images' in s.get('content','').split(',')]
    need(len(images)==1,'Selected active images storage is unavailable')
    snippets=[s for s in storages if s.get('active') and s.get('enabled',1) and 'snippets' in s.get('content','').split(',')]
    if config.get('snippets_storage'): snippets=[s for s in snippets if s['storage']==config['snippets_storage']]
    else: snippets.sort(key=lambda s:(s['storage']!=config['storage'],s['storage']!='local',s['storage']))
    need(snippets,'No active snippets-capable storage; this workflow does not change host storage definitions')
    bridges=pve_get('/nodes/'+node+'/network')
    need(any(x.get('iface')==config['bridge'] and x.get('type')=='bridge' and x.get('active') for x in bridges),'Selected bridge is not active')
    selected=images[0];thin=selected['type'] in {'dir','nfs','cifs','cephfs','glusterfs','lvmthin','zfspool','rbd'}
    reserve=20*1024**3 if thin else (config['disk']+2)*1024**3
    need(int(selected.get('avail',0))>=reserve,'Insufficient target storage reserve')
    return dict(storage=selected['storage'],storage_type=selected['type'],
                disk_format='qcow2' if selected['type'] in {'dir','nfs','cifs','cephfs','glusterfs'} else 'raw',
                snippets_storage=snippets[0]['storage'],bridge=config['bridge'])


def _select_gpu(config):
    if config['gpu']=='none': return None
    if config['gpu']=='auto':
        choices=gpu.available_vfs(target_vmid=config['vmid'])
        return choices[0] if choices else None
    return gpu.validate_assignment(config['gpu'],config['vmid'])


def plan(config, rebuild=False):
    require_root();config=normalize(config)
    need(rebuild or not _exists(config['vmid']),'VMID already exists; use the separate explicit rebuild action')
    resources=_resources(config);selected=_select_gpu(config)
    image=artifacts.resolve_image()
    return dict(status='PLAN_ONLY',config=config,resources=resources,image=image,
                selected_gpu=selected,mode='wayland' if selected else 'x11',
                host_gpu_driver_changes=False,host_reboot=False,backup_existing_vm=False,
                destructive_vmid=config['vmid'] if rebuild else None)


def prepare():
    require_root();return artifacts.prepare_artifacts()


def _validate_artifacts(data):
    image=data['image'];path=Path(image['path'])
    need(path.is_file() and not path.is_symlink() and _digest(path,'sha512')==image['sha512'],'Prepared Debian image changed')
    root=Path(data['runtime_cache']);manifest=json.loads((root/'runtime-manifest.json').read_text())
    artifacts._verify_tree(root,manifest,'runtime-manifest.json')
    for version,filename in data['driver_sources'].items():
        spec=next((dict(x,version=k) for k,x in gpu.PINNED_DRIVERS.items() if k==version),None)
        need(spec and _digest(filename)==spec['sha256'],'Prepared driver source changed')


def _owned(state):
    need(_exists(state['vmid']),'Owned VM is missing')
    config=vm_config(state['vmid'])
    need(state['owner'] in config.get('description','') and 'onepve-agent' in config.get('tags','').split(';'),
         'VMID now belongs to a different/unmanaged VM; no action performed')
    return config


def _raw_config(vmid):
    return run(['qm','config',str(vmid)]).stdout


def _disk_entries(text):
    result=[]
    for line in text.splitlines():
        key,separator,value=line.partition(':')
        if not separator or not re.fullmatch(r'(?:scsi|sata|virtio|ide|unused)\d+|efidisk0|tpmstate0',key.strip()):continue
        value=value.strip()
        if value.startswith('none') or value.startswith('cdrom'):continue
        volume=value.split(',',1)[0].removeprefix('file=')
        if volume:result.append(dict(volume=volume,readonly='media=cdrom' in value or 'ro=1' in value))
    return result


def _disk_refs(text):
    return [entry['volume'] for entry in _disk_entries(text)]


def _block_dm_uuid(info):
    if info is None or not stat.S_ISBLK(info.st_mode):return ''
    path=Path('/sys/dev/block')/(str(os.major(info.st_rdev))+':'+str(os.minor(info.st_rdev)))/'dm/uuid'
    try:return path.read_text().strip()
    except FileNotFoundError:return ''


def _lvm_identity(path,info):
    # --readonly reads LV metadata without activation or device-mapper updates.
    answer=run(['lvs','--readonly','--reportformat','json','--options',
                'vg_name,vg_uuid,lv_name,lv_uuid,lv_path,lv_dm_path'],check=False)
    need(answer.returncode==0,'Cannot read LVM metadata to identify device alias: '+str(path))
    try:rows=[row for report in json.loads(answer.stdout)['report'] for row in report.get('lv',[])]
    except (ValueError,KeyError,TypeError) as error:raise RuntimeError('Invalid read-only LVM metadata report') from error
    requested=os.path.normpath(path.as_posix());dm_uuid=_block_dm_uuid(info);matches=[]
    for row in rows:
        row={key:str(value).strip() for key,value in row.items()}
        vg,lv=row.get('vg_name',''),row.get('lv_name','')
        vg_id,lv_id=row.get('vg_uuid','').replace('-',''),row.get('lv_uuid','').replace('-','')
        aliases={os.path.normpath(value) for value in [row.get('lv_path'),row.get('lv_dm_path'),
                 '/dev/'+vg+'/'+lv,'/dev/mapper/'+vg.replace('-','--')+'-'+lv.replace('-','--')] if value}
        named=requested in aliases
        device=bool(dm_uuid and re.fullmatch(r'[A-Za-z0-9]{32}',vg_id) and re.fullmatch(r'[A-Za-z0-9]{32}',lv_id)
                    and dm_uuid=='LVM-'+vg_id+lv_id)
        if not named and not device:continue
        need(re.fullmatch(r'[A-Za-z0-9]{32}',vg_id) and re.fullmatch(r'[A-Za-z0-9]{32}',lv_id),'LVM metadata has missing/invalid UUIDs')
        if info is not None:
            need(stat.S_ISBLK(info.st_mode) and device,'Existing LVM alias does not point to its metadata UUID: '+str(path))
        matches.append(dict(path='/dev/'+vg+'/'+lv,identity=['lvm',row['vg_uuid'],row['lv_uuid']]))
    need(len(matches)<=1,'Ambiguous LVM device identity: '+str(path))
    need(matches or not dm_uuid.startswith('LVM-'),'Active LVM device UUID is missing from metadata: '+str(path))
    return matches[0] if matches else None


def _volume_identity(volume):
    if Path(volume).is_absolute():
        path=Path(volume)
    else:
        need(':' in volume,'Unresolved disk volume reference: '+volume)
        answer=run(['pvesm','path',volume],check=False)
        need(answer.returncode==0,'Cannot resolve disk alias: '+volume)
        path=Path(answer.stdout.strip())
    need(path.is_absolute(),'Storage returned a non-local/unresolved disk path: '+volume)
    original=path;info=None;failure=None
    try:
        path=path.resolve(strict=True);info=path.stat()
    except OSError as error:failure=error
    if original.as_posix().startswith('/dev/') or path.as_posix().startswith('/dev/'):
        lvm=_lvm_identity(original,info)
        if lvm:return lvm
    if failure is not None:raise RuntimeError('Cannot identify disk alias '+volume+': '+str(failure)) from failure
    if stat.S_ISREG(info.st_mode):identity=['file',info.st_dev,info.st_ino]
    elif stat.S_ISBLK(info.st_mode):identity=['block',info.st_rdev]
    else:raise RuntimeError('Disk is neither a regular file nor a block device: '+str(path))
    return dict(path=str(path),identity=identity)


def _deletion_check(vmid):
    need(_exists(vmid),'VM does not exist')
    raw=_raw_config(vmid);config=vm_config(vmid);volumes={};storage_content={}
    need(str(config.get('template','0'))!='1','Template disks may have implicit linked-clone consumers; inspect backing chains and remove manually')
    need(not re.search(r'^args:.*(?:\s|^)-(?:drive|blockdev)(?:\s|=|$)',raw,re.M),'Custom QEMU disk arguments require manual disk ownership inspection')
    for entry in _disk_entries(raw):
        volume=entry['volume']
        if Path(volume).is_absolute() or ':' not in volume:
            need(entry['readonly'],'Direct host disk paths require manual removal; no automatic disk deletion')
            continue
        storage=volume.split(':',1)[0]
        if storage not in storage_content:
            storage_content[storage]=pve_get('/nodes/'+node_name()+'/storage/'+storage+'/content','--vmid',str(vmid))
        owned={x.get('volid') for x in storage_content[storage]
               if str(x.get('vmid',vmid))==str(vmid) and x.get('content')!='iso'}
        if volume not in owned:
            cloudinit=bool(re.search(r'(?:^|/)vm-'+str(vmid)+r'-cloudinit(?:[./-]|$)',volume.split(':',1)[1]))
            need(entry['readonly'] and not cloudinit,'Disk ownership cannot be proved for target VM: '+volume)
            continue  # Common installer ISOs are read-only and never owned by this deletion.
        volumes[volume]=_volume_identity(volume)
    shared=[]
    configurations=set(VM_CONFIGS.glob('*.conf'))|set(PVE_NODES.glob('*/qemu-server/*.conf'))
    for path in configurations:
        if path.stem==str(vmid):continue
        text=path.read_text()
        need(not re.search(r'^args:.*(?:\s|^)-(?:drive|blockdev)(?:\s|=|$)',text,re.M),
             'Other VM '+path.stem+' has unresolved custom QEMU disk arguments; inspect/remove them manually')
        for entry in _disk_entries(text):
            volume=entry['volume']
            if volume in volumes:
                shared.append(dict(vmid=path.stem,config=str(path),volume=volume));continue
            try:identity=_volume_identity(volume)
            except (RuntimeError,OSError):
                need(entry['readonly'],'Unresolved writable disk alias in VM '+path.stem+': '+volume)
                continue
            if any(identity['identity']==target['identity'] for target in volumes.values()):
                shared.append(dict(vmid=path.stem,config=str(path),volume=volume,resolved=identity))
    need(not shared,'Other VM configurations/snapshots reference target disks: '+json.dumps(shared))
    return dict(config_sha256=hashlib.sha256(raw.encode()).hexdigest(),name=config.get('name'),volumes=sorted(volumes),identities=volumes)


def _stop(vmid):
    if run(['qm','status',str(vmid)]).stdout.strip()=='status: stopped':return
    run(['qm','shutdown',str(vmid),'--timeout','120'],check=False,timeout=None)
    if run(['qm','status',str(vmid)]).stdout.strip()!='status: stopped':
        run(['qm','stop',str(vmid)],timeout=None)
    need(run(['qm','status',str(vmid)]).stdout.strip()=='status: stopped','Target VM did not stop')


def _delete(state):
    vmid=state['vmid'];guard=state['delete_guard']
    need(hashlib.sha256(_raw_config(vmid).encode()).hexdigest()==guard['config_sha256'],'Target VM configuration changed after deletion preflight')
    current=_deletion_check(vmid)
    need(current.get('identities')==guard.get('identities'),'Target disk identities changed after deletion preflight')
    _save(state,'DELETING');_stop(vmid)
    need(hashlib.sha256(_raw_config(vmid).encode()).hexdigest()==guard['config_sha256'],'Target configuration changed while stopping; deletion was not continued')
    current=_deletion_check(vmid)
    need(current.get('identities')==guard.get('identities'),'Target disk identities changed while stopping; deletion was not continued')
    run(['qm','destroy',str(vmid),'--purge','1'],timeout=None)
    need(not _exists(vmid),'Target VM deletion is not confirmed')
    _save(state,'DELETED',deleted_vmid=vmid)


def _snippet(state,label,content,executable=False):
    name='onepve-'+str(state['vmid'])+'-'+state['owner']+'-'+label
    volume=state['resources']['snippets_storage']+':snippets/'+name
    path=Path(run(['pvesm','path',volume]).stdout.strip())
    need(path.is_absolute() and path.parent.resolve()==path.parent and path.name==name,'Unsafe snippets path returned by PVE')
    data=content.encode();digest=hashlib.sha256(data).hexdigest();record=state.setdefault('snippets',{}).get(label)
    if record:
        need(record['path']==str(path) and record['sha256']==digest,'Owned snippet intent changed')
        if path.exists():
            need(path.is_file() and not path.is_symlink() and _digest(path)==digest,'Owned snippet changed; refusing overwrite')
            return volume
        need(not record.get('created'),'Created snippet disappeared; refusing silent replacement')
    need(not os.path.lexists(path),'Snippet path already exists outside this operation')
    state['snippets'][label]=dict(path=str(path),volume=volume,sha256=digest,mode=0o755 if executable else 0o600,created=False)
    _save(state)
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o755 if executable else 0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    path.chmod(0o755 if executable else 0o600)
    state['snippets'][label]['created']=True;_save(state)
    return volume


def _cloud_config(state):
    cfg=state['config']
    data={'hostname':cfg['name'],'manage_etc_hosts':True,'disable_root':True,'ssh_pwauth':False,
          'users':[{'name':'agent','shell':'/bin/bash','lock_passwd':True,'groups':['video'],
                    'sudo':None,'ssh_authorized_keys':[cfg['ssh_key']]}],
          'apt':{'preserve_sources_list':False,'sources_list':
                 'Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\n'
                 'Types: deb\nURIs: https://security.debian.org/debian-security\nSuites: trixie-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n'},
          'write_files':[{'path':'/etc/apt/apt.conf.d/80onepve-agent-network','owner':'root:root','permissions':'0644',
                          'content':'Acquire::http::Timeout "15";\nAcquire::https::Timeout "15";\nAcquire::Retries "2";\n'}],
          'package_update':True,'packages':['qemu-guest-agent','openssh-server','python3'],
          'runcmd':[['systemctl','enable','--now','ssh.service'],['systemctl','start','qemu-guest-agent.service'],
                    ['touch','/root/onepve-cloud-ready']]}
    return '#cloud-config\n'+json.dumps(data,indent=2)+'\n'


def _create_vm(state):
    cfg=state['config'];vmid=str(state['vmid']);resource=state['resources']
    if state['phase']=='VM_CREATING':
        if _exists(state['vmid']):_owned(state)
        else:
            net='virtio'+('='+cfg['mac'] if cfg['mac'] else '')+',bridge='+cfg['bridge']
            run(['qm','create',vmid,'--name',cfg['name'],'--description','onePVE owner='+state['owner'],
                 '--tags','onepve-agent','--cores',str(cfg['cores']),'--memory',str(cfg['memory']),'--balloon','0',
                 '--cpu','host','--machine','q35','--bios','ovmf','--scsihw','virtio-scsi-single','--vga','virtio',
                 '--serial0','socket','--agent','enabled=1','--onboot','1','--net0',net],timeout=None)
            _owned(state)
        _save(state,'VM_CREATED')
    if state['phase']=='VM_CREATED':
        _save(state,'DISK_IMPORTING')
        run(['qm','disk','import',vmid,state['artifacts']['image']['path'],cfg['storage'],
             '--format',resource['disk_format']],timeout=None)
        _save(state,'DISK_CONFIGURING')
    if state['phase'] in {'DISK_IMPORTING','DISK_CONFIGURING'}:
        config=_owned(state)
        if 'scsi0' not in config:
            imported=[value.split(',',1)[0] for key,value in config.items() if re.fullmatch(r'unused\d+',key)]
            need(len(imported)==1,'Disk import outcome is uncertain; inspect the PVE task/storage before retrying, no disk was overwritten')
            run(['qm','set',vmid,'--scsi0',imported[0]+',discard=on,iothread=1,ssd=1'],timeout=None)
        _save(state,'DISK_READY',disk_volume=_owned(state)['scsi0'].split(',',1)[0])
    if state['phase']=='DISK_READY':
        _save(state,'CLOUD_CONFIGURING')
    if state['phase']=='CLOUD_CONFIGURING':
        seed=_snippet(state,'user.yaml',_cloud_config(state))
        current=_owned(state)
        if 'efidisk0' not in current:
            run(['qm','set',vmid,'--efidisk0',cfg['storage']+':0,efitype=4m,pre-enrolled-keys=0,format='+resource['disk_format']],timeout=None)
        if 'ide2' not in current:
            run(['qm','set',vmid,'--ide2',cfg['storage']+':cloudinit'],timeout=None)
        network='ip='+cfg['address']+',gw='+cfg['gateway'] if cfg.get('address') else 'ip=dhcp'
        command=['qm','set',vmid,'--cicustom','user='+seed,'--ipconfig0',network,'--boot','order=scsi0']
        if cfg.get('nameserver'):command.extend(['--nameserver',cfg['nameserver']])
        run(command,timeout=None)
        run(['qm','disk','resize',vmid,'scsi0',str(cfg['disk'])+'G'],timeout=None)
        _save(state,'CLOUD_WAIT')
    if state['phase']=='CLOUD_WAIT':
        current=_owned(state)
        need('scsi0' in current and state['owner'] in current.get('cicustom',''),'Cloud seed/disk does not belong to this operation')
        if run(['qm','status',vmid]).stdout.strip()=='status: stopped':run(['qm','start',vmid],timeout=None)
        _wait_cloud(state)
        _save(state,'CLOUD_READY')


def _qga_try(vmid,command,timeout=30):
    with Qga(vmid) as q:return q.execute(command,timeout=timeout)


def _wait_cloud(state,seconds=1800):
    deadline=time.monotonic()+seconds;last='Guest Agent not available'
    while time.monotonic()<deadline:
        try:
            result=_qga_try(state['vmid'],['/usr/bin/python3','-c',"from pathlib import Path;assert Path('/root/onepve-cloud-ready').is_file();print('CLOUD_READY')"],10)
            if result['returncode']==0:return
            last=result['stderr'][-300:]
        except (OSError,RuntimeError,TimeoutError,KeyError) as error:last=type(error).__name__
        time.sleep(2)
    raise TimeoutError('Initial cloud setup is incomplete; resume after inspection: '+last)


def _wait_desktop(state,seconds=180):
    source="""import os,pwd,stat,subprocess,time,urllib.request,json
from pathlib import Path
account=pwd.getpwnam('agent');runtime=Path('/run/user')/str(account.pw_uid)
assert runtime.is_dir() and runtime.stat().st_uid==account.pw_uid
bus=runtime/'bus';assert stat.S_ISSOCK(bus.stat().st_mode) and bus.stat().st_uid==account.pw_uid
environment=dict(HOME=account.pw_dir,USER=account.pw_name,LOGNAME=account.pw_name,PATH='/usr/local/bin:/usr/bin:/bin',XDG_RUNTIME_DIR=str(runtime),DBUS_SESSION_BUS_ADDRESS='unix:path='+str(bus))
def user(*args,env=environment):
 return subprocess.check_output(['/usr/sbin/runuser','-u',account.pw_name,'--',*args],env=env,text=True,timeout=4)
imported=dict(line.split('=',1) for line in user('/bin/systemctl','--user','show-environment').splitlines() if '=' in line)
assert imported.get('DISPLAY') and imported.get('XAUTHORITY')
authority=Path(imported['XAUTHORITY']);assert authority.is_file() and authority.stat().st_uid==account.pw_uid
key=Path(account.pw_dir)/'.local/state/onepve-agent/session-key'
assert '|'+imported['DISPLAY']+'|'+imported['XAUTHORITY'] in key.read_text()
boot=time.time()-float(Path('/proc/uptime').read_text().split()[0]);assert key.stat().st_mtime>=boot-5
user('/usr/bin/xdpyinfo','-display',imported['DISPLAY'],env=dict(environment,**imported))
assert user('/bin/systemctl','--user','is-active','onepve-agent-session.target').strip()=='active'
assert user('/bin/systemctl','--user','is-active','onepve-agent-fixture.service').strip()=='active'
assert user('/bin/systemctl','--user','is-active','onepve-agent-keyring.service').strip()=='active'
with urllib.request.urlopen('http://127.0.0.1:3000/',timeout=4) as response:
 assert response.status==200 and b'<title>Agent browser acceptance</title>' in response.read(1024*1024)
print(json.dumps({'status':'DESKTOP_READY','boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'uid':account.pw_uid,'display':imported['DISPLAY'],'xauthority':str(authority),'ui_input_issued':False}))
"""
    deadline=time.monotonic()+seconds;last='Desktop session has not imported its display'
    while time.monotonic()<deadline:
        try:
            answer=_qga_try(state['vmid'],['/usr/bin/python3','-c',source],30)
            if answer['returncode']==0:
                evidence=json.loads(answer['stdout']);need(evidence.get('status')=='DESKTOP_READY','Unexpected desktop readiness evidence')
                state['desktop_ready']=evidence;_save(state);return evidence
            last=answer['stderr'][-300:]
        except (OSError,RuntimeError,TimeoutError,ValueError) as error:last=type(error).__name__
        time.sleep(2)
    raise TimeoutError('Fresh normal-user desktop is not ready; no browser input was issued: '+last)


def _bundle(state):
    _validate_artifacts(state['artifacts'])
    directory=_state_path(state['vmid']).parent
    path=directory/('payload-'+state['owner']+'.tar.gz')
    if 'bundle_sha256' in state:
        need(path.is_file() and _digest(path)==state['bundle_sha256'],'Staged source bundle changed')
        return path
    need(not path.exists(),'Unrecorded bundle exists; inspect before replacing')
    root=repo_root();runtime=Path(state['artifacts']['runtime_cache'])
    with tarfile.open(path,'x:gz',compresslevel=1) as stream:
        for source in sorted((root/'guest').rglob('*')):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix!='.pyc':
                stream.add(source,arcname='source/'+str(source.relative_to(root)).replace('\\','/'),recursive=False)
        for name in ['onepve_common.py','onepve_gpu.py']:
            stream.add(root/'lib'/name,arcname='source/lib/'+name,recursive=False)
        stream.add(runtime,arcname='cache',recursive=True)
        for version,source in state['artifacts']['driver_sources'].items():
            stream.add(source,arcname='drivers/'+version+'.tar.gz',recursive=False)
    path.chmod(0o600);_save(state,bundle_sha256=_digest(path),bundle_bytes=path.stat().st_size)
    return path


def _guest(state,argv,timeout=60):
    result=_qga_try(state['vmid'],argv,timeout)
    need(result['returncode']==0,'Guest command failed: '+str(argv[0])+': '+result['stderr'][-1400:])
    return result


def _upload_payload(state):
    path=_bundle(state);stage='/opt/onepve-stage/'+state['owner'];payload=stage+'/payload'
    _save(state,'PAYLOAD_UPLOADING',guest_stage=stage)
    with Qga(state['vmid']) as q:
        staged=q.execute(['/usr/bin/python3','-c',
                   'from pathlib import Path;import os; p=Path('+repr(stage)+');p.mkdir(parents=True,exist_ok=True,mode=0o700);assert p.resolve()==p and p.stat().st_uid==0;os.chmod(p,0o700)'],30)
        need(staged['returncode']==0,'Could not prepare root-owned guest staging directory')
        q.write(stage+'/payload.tar.gz',path.read_bytes())
        source="""import hashlib,tarfile,os,json
from pathlib import Path
stage=Path(STAGE);archive=stage/'payload.tar.gz';assert hashlib.sha256(archive.read_bytes()).hexdigest()==DIGEST
target=stage/'payload'
if target.exists():
 assert (target/'.bundle-sha256').read_text()==DIGEST
else:
 target.mkdir(mode=0o700)
 with tarfile.open(archive,'r:gz') as stream:stream.extractall(target,filter='data')
 (target/'.bundle-sha256').write_text(DIGEST)
manifest=dict(MANIFEST,cache_dir=str(target/'cache'))
(stage/'manifest.json').write_text(json.dumps(manifest));(stage/'manifest.json').chmod(0o600)
""".replace('STAGE',repr(stage)).replace('DIGEST',repr(state['bundle_sha256'])).replace('MANIFEST',repr(dict(artifacts.VERSIONS,user='agent',viewer_port=state['config']['viewer_port'],start_desktop=True,gpu_bdf='')))
        result=q.execute(['/usr/bin/python3','-c',source],180)
        need(result['returncode']==0,'Guest payload extraction/checksum failed: '+result['stderr'][-1000:])
    _save(state,'PAYLOAD_READY',guest_payload=payload)


def _job(state,name,command,timeout=7200,retry_failed=False):
    jobs=state.setdefault('jobs',{});job=jobs.get(name)
    if job and job.get('result',{}).get('status')=='SUCCESS':return job['result']
    if job and job.get('result',{}).get('status')=='FAILED':
        need(retry_failed,'Previous '+name+' job failed; explicit resume is required')
        job=None
    if job is None:
        attempt=len([x for x in state.setdefault('job_history',[]) if x['name']==name])+1
        unit='onepve-'+name+'-'+state['owner'][:12]+'-'+str(attempt)
        directory='/var/lib/onepve-agent/jobs';result=directory+'/'+unit+'.json';script=state['guest_stage']+'/'+unit+'.py'
        job=dict(name=name,unit=unit,path=result,script=script,command=command)
        jobs[name]=job;state['job_history'].append(dict(job));_save(state)
    progress('VM %s：%s' % (state['vmid'], PHASE_LABELS.get(state.get('phase'), '运行客机安装任务')),
             '客机日志：' + str(Path(job['path']).with_suffix('.log')))
    wrapper="""import subprocess,json,os
from pathlib import Path
p=Path(RESULT);p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);os.umask(0o077)
def write(value):
 t=p.with_suffix('.new');t.write_text(json.dumps(value));t.replace(p)
write({'status':'RUNNING'})
with p.with_suffix('.log').open('ab',buffering=0) as log:
 try:
  s=subprocess.run(COMMAND,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
  write({'status':'SUCCESS' if s.returncode==0 else 'FAILED','returncode':s.returncode,'log':str(p.with_suffix('.log'))})
 except BaseException as e:
  write({'status':'FAILED','error_type':type(e).__name__,'log':str(p.with_suffix('.log'))});raise
""".replace('RESULT',repr(job['path'])).replace('COMMAND',repr(job['command']))
    # The unit and result path are deterministic: resume observes rather than
    # launching a duplicate while a slow installation/build is still running.
    with Qga(state['vmid']) as q:
        observe=q.execute(['/bin/systemctl','show',job['unit']+'.service','-p','LoadState,ActiveState'],30)
        absent='LoadState=not-found' in observe['stdout']
        if absent:
            exists=q.execute(['/usr/bin/test','-f',job['path']],10)
            if exists['returncode']!=0:
                # A previous CLI may have stopped after recording the intent.
                q.write(job['script'],wrapper.encode())
                launch=q.execute(['/usr/bin/systemd-run','--no-block','--unit='+job['unit'],'--property=Type=oneshot',
                                  '--property=RemainAfterExit=yes','--property=TimeoutStartSec=infinity',
                                  '/usr/bin/python3',job['script']],30)
                need(launch['returncode']==0,'Could not launch persistent guest job')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            with Qga(state['vmid']) as q:result=json.loads(q.read(job['path']))
        except (OSError,RuntimeError,json.JSONDecodeError):result={}
        if result.get('status') in {'SUCCESS','FAILED'}:
            job['result']=result;_save(state)
            need(result['status']=='SUCCESS',name+' failed; inspect guest '+result.get('log',job['path']))
            return result
        time.sleep(2)
    raise TimeoutError(name+' outcome is still running/uncertain; resume will observe '+job['unit'])


def _runtime_verify(state,hardware=False):
    command=['/opt/onepve-agent/bin/agent-verify']+(['--gpu'] if hardware else [])
    result=_guest(state,command,120)
    document=json.loads(result['stdout'])
    need(document.get('status')==('PASS_GPU_RUNTIME' if hardware else 'PASS_BASE_RUNTIME'),'Requested runtime verification did not pass')
    acceptance=_guest(state,['/usr/sbin/runuser','-u','agent','--','/opt/onepve-agent/bin/agent-mcp-acceptance']+(['--gpu'] if hardware else []),180)
    proof=json.loads(acceptance['stdout'])
    need(proof.get('status')=='passed','MCP acceptance did not pass')
    state.setdefault('verification',{})['gpu' if hardware else 'base']=dict(runtime=document,mcp=proof)
    _save(state)
    return document


def _connection(state):
    source="""import json,socket,subprocess
from pathlib import Path
links=json.loads(subprocess.check_output(['ip','-j','-4','address','show','scope','global'],text=True))
addresses=[a['local'] for n in links for a in n.get('addr_info',[]) if a.get('family')=='inet']
p=Path('/etc/ssh/ssh_host_ed25519_key.pub');key=p.read_text().strip()
print(json.dumps({'ipv4':addresses,'host_public_key':key,'host_key_fingerprint':subprocess.check_output(['ssh-keygen','-lf',str(p),'-E','sha256'],text=True).strip()}))
"""
    data=json.loads(_guest(state,['/usr/bin/python3','-c',source],30)['stdout'])
    need(not state['config'].get('expected_ip') or state['config']['expected_ip'] in data['ipv4'],
         'Guest IP differs from the expected FRPC destination; host bridge/FRPC/SSH host checking were not changed')
    state['connection']=data;_save(state);return data


def _driver_script(state):
    spec=state['driver_spec'];stage=state['guest_payload']
    return """import sys,subprocess,json,hashlib,tarfile,os
from pathlib import Path
sys.path.insert(0,LIB)
import onepve_gpu as gpu
spec=SPEC;kernel=os.uname().release
assert kernel==spec['kernel'];assert kernel.startswith('6.12.')
release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
assert release.get('ID','').strip('"')=='debian' and release.get('VERSION_ID','').strip('"')=='13'
source=Path('/usr/src')/('i915-sriov-dkms-'+spec['version']);archive=Path(ARCHIVE)
assert hashlib.sha256(archive.read_bytes()).hexdigest()==spec['sha256']
marker=source/'.onepve-origin.json'
if source.exists():assert marker.is_file() and json.loads(marker.read_text())['archive_sha256']==spec['sha256']
else:
 gpu._extract_source(archive,source);marker.write_text(json.dumps({'archive_sha256':spec['sha256']}));marker.chmod(0o600)
environment=dict(os.environ,DEBIAN_FRONTEND='noninteractive')
def run(*a):subprocess.run(a,check=True,env=environment)
policy=subprocess.run(['apt-cache','policy','firmware-intel-graphics'],capture_output=True,text=True,check=True).stdout
if not any(line.strip().startswith('Candidate:') and line.split(':',1)[1].strip() not in {'','(none)'} for line in policy.splitlines()):
 apt_source=Path('/etc/apt/sources.list.d/onepve-agent-firmware.sources')
 apt_body='Types: deb\\nURIs: https://deb.debian.org/debian\\nSuites: trixie\\nComponents: non-free-firmware\\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\\n\\nTypes: deb\\nURIs: https://security.debian.org/debian-security\\nSuites: trixie-security\\nComponents: non-free-firmware\\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\\n'
 assert Path('/usr/share/keyrings/debian-archive-keyring.gpg').is_file()
 if apt_source.exists() or apt_source.is_symlink():assert not apt_source.is_symlink() and apt_source.stat().st_uid==0 and apt_source.read_text()==apt_body
 else:
  with os.fdopen(os.open(apt_source,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644),'w') as stream:stream.write(apt_body)
run('apt-get','update');run('apt-get','install','-y','--no-install-recommends','dkms','build-essential','linux-headers-'+kernel,'firmware-intel-graphics')
registered=subprocess.run(['dkms','status','-m','i915-sriov-dkms','-v',spec['version']],capture_output=True,text=True)
if not registered.stdout.strip():run('dkms','add','-m','i915-sriov-dkms','-v',spec['version'])
names=['i915','xe','kvmgt','intel_sriov_compat']
installed=all(subprocess.run(['modinfo','-k',kernel,'-F','version',name],capture_output=True,text=True).stdout.strip()==spec['version']+'-sriov' for name in names)
build=Path('/var/lib/dkms/i915-sriov-dkms')/spec['version']/'build'
if not installed:
 run('dkms','build','-m','i915-sriov-dkms','-v',spec['version'],'-k',kernel,'-j','2')
 modules=sorted((Path('/var/lib/dkms/i915-sriov-dkms')/spec['version']/kernel/os.uname().machine/'module').glob('*.ko*'))
 evidence=gpu._verify_built_modules(kernel,build,modules)
 Path('/var/lib/onepve-agent/driver-build.json').write_text(json.dumps({'spec':spec,'modules':evidence},indent=2))
 run('dkms','install','-m','i915-sriov-dkms','-v',spec['version'],'-k',kernel,'-j','2')
else:
 modules=[Path(subprocess.check_output(['modinfo','-k',kernel,'-F','filename',name],text=True).strip()) for name in names]
 gpu._verify_built_modules(kernel,build,modules)
for name in names:
 assert subprocess.check_output(['modinfo','-k',kernel,'-F','version',name],text=True).strip()==spec['version']+'-sriov'
 magic=subprocess.check_output(['modinfo','-k',kernel,'-F','vermagic',name],text=True);assert magic.split()[0]==kernel
path=Path('/etc/modprobe.d/onepve-agent-gpu.conf')
body='options i915 enable_guc=3'+(' force_probe=7d67' if DEVICE=='0x7d67' else '')+'\\nblacklist xe\\n'
if path.exists():assert path.read_text()==body
else:path.write_text(body)
run('update-initramfs','-u','-k',kernel)
print(json.dumps({'status':'GUEST_DRIVER_READY','kernel':kernel,'version':spec['version']}))
""".replace('LIB',repr(stage+'/source/lib')).replace('SPEC',repr(spec)).replace('ARCHIVE',repr(stage+'/drivers/'+spec['version']+'.tar.gz')).replace('DEVICE',repr(state['selected_gpu']['device']))


def _gpu_hook(state):
    device=state['selected_gpu'];vmid=str(state['vmid'])
    return """#!/usr/bin/python3
import sys,json,re
from pathlib import Path
if len(sys.argv)!=3:raise SystemExit('Expected virtual-machine ID and hook phase')
if sys.argv[2]!='pre-start':raise SystemExit(0)
assert sys.argv[1]==VMID
text=Path('/etc/pve/qemu-server/'+VMID+'.conf').read_text()
assert OWNER in text
current=re.search(r'^hostpci0:\\s*(?:host=)?([^,\\s]+)',text,re.M)
assert current and current[1]==BDF
p=Path('/sys/bus/pci/devices')/BDF
assert (p/'vendor').read_text().strip()==VENDOR and (p/'device').read_text().strip()==DEVICE
assert (p/'driver').resolve(strict=True).name=='vfio-pci'
assert sorted(x.name for x in (p/'iommu_group/devices').iterdir())==[BDF]
if (p/'physfn').exists():assert (p/'physfn/driver').resolve(strict=True).name=='i915'
""".replace('VMID',repr(vmid)).replace('OWNER',repr(state['owner'])).replace('BDF',repr(device['bdf'])).replace('VENDOR',repr(device['vendor'])).replace('DEVICE',repr(device['device']))


def _fallback(state,error):
    error=str(error)
    timestamp=state.get('gpu_error_utc') if state.get('gpu_error')==error else None
    _save(state,'GPU_FALLBACK',gpu_error=error,gpu_error_utc=timestamp or datetime.now(timezone.utc).isoformat())
    _detach_gpu(state)
    _wait_desktop(state)
    _guest(state,['/opt/onepve-agent/bin/agent-set-mode','x11'],180)
    _runtime_verify(state,False)
    _save(state,'GPU_FAILED_SOFTWARE_READY',mode='x11')


def _detach_gpu(state):
    config=_owned(state)
    if state.get('owned_gpu_bdf'):
        actual=config.get('hostpci0','').split(',',1)[0].removeprefix('host=')
        need(not actual or actual==state['owned_gpu_bdf'],'GPU assignment was edited outside onePVE; fallback stopped')
        hook=state.get('snippets',{}).get('gpu-hook.py',{}).get('volume')
        need(not config.get('hookscript') or config['hookscript']==hook,'GPU prestart hook was edited outside onePVE; fallback stopped')
        if actual:
            _stop(state['vmid'])
            fields='hostpci0'+(',hookscript' if config.get('hookscript')==hook else '')
            run(['qm','set',str(state['vmid']),'--delete',fields],timeout=None)
            run(['qm','start',str(state['vmid'])],timeout=None);_wait_cloud(state,300)


def _archive_gpu_error(state):
    if 'gpu_error' not in state:return
    previous=dict(error=state.pop('gpu_error'),archived_utc=datetime.now(timezone.utc).isoformat())
    if 'gpu_error_utc' in state:previous['failed_utc']=state.pop('gpu_error_utc')
    log=re.search(r'inspect guest (/\S+)',str(previous['error']))
    if log:previous['log']=log[1]
    state.setdefault('gpu_failure_history',[]).append(previous)


def _continue(state,retry=False):
    need(state.get('operation') in {'create','rebuild','remove'},'Operation kind is missing/unknown; inspect state before any recovery action')
    if state.get('operation')=='remove':return _resume_remove(state)
    cfg=state['config']
    if state['phase']=='DELETING':
        need(not _exists(state['vmid']),'Deletion outcome requires inspection; resume never repeats deletion')
        _save(state,'DELETED')
    if state['phase']=='DELETED' and state.get('previous_snippets'):
        _remove_snippets({'snippets':state['previous_snippets']});_save(state,previous_snippets={})
    if state['phase'] in {'PLANNED','DELETED'}:_save(state,'VM_CREATING')
    if state['phase'] in {'VM_CREATING','VM_CREATED','DISK_IMPORTING','DISK_CONFIGURING','DISK_READY','CLOUD_CONFIGURING','CLOUD_WAIT'}:_create_vm(state)
    _owned(state)
    if state['phase'] in {'CLOUD_READY','PAYLOAD_UPLOADING'}:_upload_payload(state)
    if state['phase'] in {'PAYLOAD_READY','INSTALL_RUNNING'}:
        _save(state,'INSTALL_RUNNING')
        _job(state,'install',['/bin/bash',state['guest_payload']+'/source/guest/install.sh','--manifest',state['guest_stage']+'/manifest.json'],retry_failed=retry)
        _save(state,'BASE_VERIFYING')
    if state['phase']=='BASE_VERIFYING':
        # LightDM/XFCE user startup follows the installer asynchronously.
        deadline=time.monotonic()+120
        while True:
            try:_runtime_verify(state,False);break
            except RuntimeError:
                if time.monotonic()>=deadline:raise
                time.sleep(2)
        _connection(state);_save(state,'BASE_READY',mode='x11')
    if state['phase']=='BASE_READY' and not state.get('selected_gpu'):
        _save(state,'READY_SOFTWARE');return state
    if state['phase'] in {'BASE_READY','GUEST_DRIVER_RUNNING','GUEST_DRIVER_READY','GPU_ATTACHING','GPU_BOOTING','GPU_MODE_SWITCHING','GPU_VERIFYING','GPU_FALLBACK'}:
        if state['phase']=='GPU_FALLBACK':_fallback(state,state.get('gpu_error','Interrupted GPU fallback'));return state
        try:
            if state['phase'] in {'BASE_READY','GUEST_DRIVER_RUNNING'}:
                kernel=_guest(state,['/bin/uname','-r'],30)['stdout'].strip();spec=gpu.driver_spec(kernel)
                need(spec['version'] in state['artifacts']['driver_sources'],'Driver archive for actual guest kernel was not prepared before rebuild')
                state['driver_spec']=spec;_save(state,'GUEST_DRIVER_RUNNING')
                path=state['guest_stage']+'/prepare-driver.py'
                with Qga(state['vmid']) as q:q.write(path,_driver_script(state).encode())
                _job(state,'driver',['/usr/bin/python3',path],retry_failed=retry)
                _save(state,'GUEST_DRIVER_READY')
            if state['phase'] in {'GUEST_DRIVER_READY','GPU_ATTACHING'}:
                _save(state,'GPU_ATTACHING')
                selected=gpu.validate_assignment(state['selected_gpu']['bdf'],state['vmid'])
                hook=_snippet(state,'gpu-hook.py',_gpu_hook(state),True)
                current=_owned(state)
                if 'hostpci0' in current:
                    need(current['hostpci0'].split(',',1)[0].removeprefix('host=')==selected['bdf'],'Foreign hostpci0 assignment')
                _stop(state['vmid']);_save(state,owned_gpu_bdf=selected['bdf'])
                run(['qm','set',str(state['vmid']),'--hostpci0',selected['bdf']+',pcie=1','--hookscript',hook],timeout=None)
                _save(state,'GPU_BOOTING')
            if state['phase']=='GPU_BOOTING':
                if run(['qm','status',str(state['vmid'])]).stdout.strip()=='status: stopped':run(['qm','start',str(state['vmid'])],timeout=None)
                _wait_cloud(state,300);_save(state,'GPU_MODE_SWITCHING')
            if state['phase']=='GPU_MODE_SWITCHING':
                _wait_desktop(state)
                source="from pathlib import Path;import json;print(json.dumps([p.name for p in Path('/sys/bus/pci/devices').iterdir() if (p/'vendor').read_text().strip()=="+repr(state['selected_gpu']['vendor'])+" and (p/'device').read_text().strip()=="+repr(state['selected_gpu']['device'])+" and (p/'driver').resolve().name=='i915']))"
                devices=json.loads(_guest(state,['/usr/bin/python3','-c',source],30)['stdout'])
                need(len(devices)==1,'The assigned Intel GPU was not uniquely discovered under i915')
                state['guest_gpu_bdf']=devices[0]
                _guest(state,['/opt/onepve-agent/bin/agent-set-mode','wayland',devices[0]],180)
                _save(state,'GPU_VERIFYING')
            if state['phase']=='GPU_VERIFYING':
                _runtime_verify(state,True);_connection(state);_archive_gpu_error(state);_save(state,'READY_GPU',mode='wayland')
        except (RuntimeError,TimeoutError,OSError,subprocess.SubprocessError) as error:
            _fallback(state,error)
    return state


def _new_state(config,prepared,resources,selected,operation='create'):
    return dict(schema_version=1,operation=operation,vmid=config['vmid'],owner=uuid.uuid4().hex,phase='PLANNED',
                config=config,artifacts=prepared,resources=resources,selected_gpu=selected,snippets={})


def create(config,dry_run=False):
    require_root();config=normalize(config)
    if dry_run:return plan(config)
    with _lock(config['vmid']):
        need(not _exists(config['vmid']),'VMID already exists; create never overwrites it')
        need(not _state_path(config['vmid']).exists(),'A previous operation exists; use status/resume or explicit rebuild')
        resources=_resources(config);selected=_select_gpu(config);prepared=prepare();_validate_artifacts(prepared)
        state=_new_state(config,prepared,resources,selected);_save(state)
        _bundle(state)
        return _continue(state)


def rebuild(config,confirm_vmid,dry_run=False):
    require_root();config=normalize(config)
    need(confirm_vmid==config['vmid'],'Rebuild requires --confirm-vmid matching exactly')
    if dry_run:
        result=plan(config,True);result['delete_guard']=_deletion_check(config['vmid']);return result
    with _lock(config['vmid']):
        resources=_resources(config);selected=_select_gpu(config);guard=_deletion_check(config['vmid'])
        prepared=prepare();_validate_artifacts(prepared)
        old=_state_path(config['vmid']);previous=None
        if old.exists():
            previous=json.loads(old.read_text());need(previous.get('phase') in FINISHED|{'REMOVED'},'Existing operation is unfinished; inspect/resume rather than replacing its evidence')
            archived=old.with_name('state.previous-'+previous['owner']+'.json');need(not archived.exists(),'Previous operation archive already exists');old.rename(archived)
        state=_new_state(config,prepared,resources,selected,operation='rebuild');state['delete_guard']=guard
        if previous:state['previous_snippets']=previous.get('snippets',{})
        _save(state)
        _bundle(state);_delete(state)
        return _continue(state)


def resume(vmid,gpu_selection=None):
    require_root()
    with _lock(vmid):
        state=json.loads(_state_path(vmid).read_text());need(state['vmid']==vmid,'State VMID differs')
        need(state.get('operation') in {'create','rebuild','remove'},'Operation kind is missing/unknown; inspect state before any recovery action')
        if state.get('operation')=='remove':return _resume_remove(state)
        _validate_artifacts(state['artifacts'])
        if gpu_selection is not None:
            need(state['phase'] in FINISHED|{'BASE_READY'},'GPU selection can change only at a verified stable checkpoint')
            state['config']['gpu']=gpu_selection;state['config']=normalize(state['config']);state['selected_gpu']=_select_gpu(state['config'])
            if not state['selected_gpu']:
                _detach_gpu(state);_wait_desktop(state);_guest(state,['/opt/onepve-agent/bin/agent-set-mode','x11'],180);_runtime_verify(state,False)
            _save(state,'BASE_READY')
        if state['phase'] in FINISHED:return state
        return _continue(state,retry=True)


def _remove_snippets(state):
    for entry in state.get('snippets',{}).values():
        path=Path(entry['path'])
        if not path.exists():continue
        need(not path.is_symlink() and _digest(path)==entry['sha256'],'Owned snippet changed; it was retained: '+str(path))
        path.unlink()


def _resume_remove(state):
    need(state['phase'] in {'REMOVE_REQUESTED','DELETING','DELETED','REMOVED'},'Unrecognized removal checkpoint')
    need(not _exists(state['vmid']),'Removal outcome requires inspection: resume never deletes or recreates a VM')
    _remove_snippets(state);_save(state,'REMOVED');return state


def remove(vmid,confirm_vmid,dry_run=False):
    require_root();need(type(vmid)is int and 100<=vmid<=999999999 and confirm_vmid==vmid,'Remove requires the exact explicit VMID confirmation')
    guard=_deletion_check(vmid)
    if dry_run:return dict(status='PLAN_ONLY',action='remove',vmid=vmid,delete_guard=guard,backup=False)
    with _lock(vmid):
        path=_state_path(vmid)
        if path.exists():
            state=json.loads(path.read_text())
            if state.get('operation')!='remove':_owned(state);state['managed_vm']=True
            elif state.get('managed_vm'):_owned(state)
            else:need(state.get('delete_guard',{}).get('config_sha256')==guard['config_sha256'],'Unmanaged removal target changed; inspect before any new operation')
        else:state=dict(schema_version=1,owner=uuid.uuid4().hex,vmid=vmid,phase='REMOVE_REQUESTED',snippets={})
        state['operation']='remove';state['delete_guard']=guard;_save(state,'REMOVE_REQUESTED');_delete(state);_remove_snippets(state);_save(state,'REMOVED')
        return state


def status(vmid=None):
    if vmid is None:
        return {'vms':[json.loads(p.read_text()) for p in sorted(AGENT_STATE.glob('*/state.json'))]}
    path=_state_path(vmid);need(path.is_file(),'No onePVE Agent operation is recorded for this VMID')
    state=json.loads(path.read_text());state['vm_exists']=_exists(vmid)
    if state['vm_exists']:
        config=vm_config(vmid);state['ownership_matches']=state['owner'] in config.get('description','') and 'onepve-agent' in config.get('tags','').split(';')
    return state


def verify(vmid):
    require_root()
    with _lock(vmid):
        state=json.loads(_state_path(vmid).read_text());_owned(state)
        result=_runtime_verify(state,state.get('phase')=='READY_GPU');_connection(state)
        return dict(status='VERIFIED',vmid=vmid,mode=state.get('mode'),runtime=result,connection=state['connection'])


@progress_session()
def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='action',required=True)
    commands.add_parser('prepare')
    for action in ['plan','create','rebuild']:
        child=commands.add_parser(action)
        child.add_argument('--vmid',type=int,required=True);child.add_argument('--name',default='agent-browser')
        child.add_argument('--storage',required=True);child.add_argument('--snippets-storage');child.add_argument('--bridge',required=True)
        child.add_argument('--cores',type=int,default=4);child.add_argument('--memory',type=int,default=8192);child.add_argument('--disk',type=int,default=32)
        keys=child.add_mutually_exclusive_group(required=True);keys.add_argument('--ssh-key');keys.add_argument('--ssh-key-file',type=Path)
        child.add_argument('--gpu',default='auto');child.add_argument('--mac');child.add_argument('--expected-ip');child.add_argument('--viewer-port',type=int,default=16080)
        child.add_argument('--address',help='Optional static IPv4 CIDR; default is DHCP')
        child.add_argument('--gateway',help='IPv4 gateway for --address');child.add_argument('--nameserver',help='One IPv4 DNS server')
        child.add_argument('--dry-run',action='store_true')
        if action=='rebuild':child.add_argument('--confirm-vmid',type=int,required=True)
    commands.add_parser('status').add_argument('--vmid',type=int)
    commands.add_parser('verify').add_argument('--vmid',type=int,required=True)
    command=commands.add_parser('resume');command.add_argument('--vmid',type=int,required=True);command.add_argument('--gpu')
    command=commands.add_parser('remove');command.add_argument('--vmid',type=int,required=True);command.add_argument('--confirm-vmid',type=int,required=True);command.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    if args.action in {'plan','create','rebuild'}:
        config={k:v for k,v in vars(args).items() if k not in {'action','ssh_key_file','dry_run','confirm_vmid'}}
        if args.ssh_key_file:config['ssh_key']=args.ssh_key_file.read_text().strip()
        if args.action=='plan':result=plan(config)
        elif args.action=='create':result=create(config,args.dry_run)
        else:result=rebuild(config,args.confirm_vmid,args.dry_run)
    elif args.action=='prepare':result=prepare()
    elif args.action=='status':result=status(args.vmid)
    elif args.action=='verify':result=verify(args.vmid)
    elif args.action=='resume':result=resume(args.vmid,args.gpu)
    else:result=remove(args.vmid,args.confirm_vmid,args.dry_run)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (RuntimeError,OSError,ValueError,subprocess.SubprocessError) as error:
        print(json.dumps({'status':'ERROR','error':str(error)},ensure_ascii=False),file=sys.stderr);sys.exit(1)
