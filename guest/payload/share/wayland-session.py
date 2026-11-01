#!/usr/bin/env python3
"""Process-local Wayland environment; original profile and original user D-Bus remain intact."""
import argparse
import datetime
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time

from runtime_config import config, mode
SETTINGS=config()
UID=SETTINGS['uid']
HOME=SETTINGS['home']
RUNTIME=Path('/run/user')/str(UID)/'onepve-agent-wayland'
STATE=HOME/'.local/state/onepve-agent/wayland-state.json'
LOG=STATE.with_name('wayland.log')
BUS='unix:path=/run/user/'+str(UID)+'/bus'
BDF=mode()['gpu_bdf']
CONFIG='/opt/onepve-agent/share/wayland/sway.conf'


def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def guard():
    if sys.platform!='linux' or os.geteuid()!=UID or os.getuid()!=UID:
        raise RuntimeError('Run as the existing ordinary agent account')
    bus=Path('/run/user')/str(UID)/'bus'
    if not bus.exists() or not stat.S_ISSOCK(bus.stat().st_mode) or bus.stat().st_uid!=UID:
        raise RuntimeError('Original user D-Bus socket is unavailable')


def target_node():
    device=Path('/sys/bus/pci/devices')/BDF
    if ((device/'vendor').read_text().strip()!='0x8086' or (device/'driver').resolve(strict=True).name!='i915'):
        raise RuntimeError('Expected selected Intel GPU bound to i915')
    link=Path('/dev/dri/by-path')/('pci-'+BDF+'-render')
    if link.exists():nodes=[link.resolve(strict=True)]
    else:nodes=[Path('/dev/dri')/node.name for node in Path('/sys/class/drm').glob('renderD*')
                if (node/'device').resolve()==device.resolve()]
    if len(nodes)!=1:raise RuntimeError('Exact PCI VF does not have one render node')
    node=nodes[0]
    if (not stat.S_ISCHR(node.stat().st_mode) or not os.access(node,os.R_OK|os.W_OK)
            or (Path('/sys/class/drm')/node.name/'device').resolve()!=device.resolve()):
        raise RuntimeError('Resolved Intel VF render node is inaccessible or has wrong PCI identity')
    return str(node)


def write_state(value):
    STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if STATE.is_symlink():raise RuntimeError('Refusing symlink state file')
    temporary=STATE.with_name(STATE.name+'.tmp.'+str(os.getpid()))
    temporary.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    os.chmod(temporary,0o600);temporary.replace(STATE)


def starttime(pid):return (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]


def local_env(state=None):
    # Preserve useful locale/input-method settings, but never inherit display/backend overrides.
    env={key:value for key,value in os.environ.items() if key in
        ('LANG','LANGUAGE','LC_ALL','TZ','XMODIFIERS','GTK_IM_MODULE','QT_IM_MODULE','CLUTTER_IM_MODULE','GNOME_KEYRING_CONTROL')}
    env.update(HOME=str(HOME),USER=SETTINGS['user'],LOGNAME=SETTINGS['user'],PATH='/usr/local/bin:/usr/bin:/bin',
        XDG_RUNTIME_DIR=str(RUNTIME),XDG_SESSION_TYPE='wayland',XDG_CURRENT_DESKTOP='sway',
        DBUS_SESSION_BUS_ADDRESS=BUS,GDK_BACKEND='wayland',DRI_PRIME='pci-'+BDF.replace(':','_').replace('.','_'))
    if state:env.update(WAYLAND_DISPLAY=state['wayland_display'],SWAYSOCK=state['sway_socket'])
    return env


def owned_sockets(pid):
    inodes=set()
    for file in (Path('/proc')/str(pid)/'fd').iterdir():
        try:
            match=re.fullmatch(r'socket:\[(\d+)\]',os.readlink(file))
            if match:inodes.add(match[1])
        except OSError:continue
    paths=set()
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        columns=line.split(maxsplit=7)
        if len(columns)==8 and columns[6] in inodes:
            path=Path(columns[7])
            if path.parent==RUNTIME and path.exists() and path.stat().st_uid==UID and stat.S_ISSOCK(path.stat().st_mode):paths.add(path)
    wayland=[path for path in paths if re.fullmatch(r'wayland-[0-9]+',path.name)]
    ipc=[path for path in paths if re.fullmatch(r'sway-ipc\.'+str(UID)+r'\.[0-9]+\.sock',path.name)]
    if len(wayland)!=1 or len(ipc)!=1:raise RuntimeError('Actual Sway-owned Wayland/IPC sockets are not ready')
    return wayland[0],ipc[0]


def read_ready():
    state=json.loads(STATE.read_text())
    if state['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():raise RuntimeError('Stale boot state')
    pid=state['sway_pid'];process=Path('/proc')/str(pid)
    if process.stat().st_uid!=UID or starttime(pid)!=state['sway_starttime'] or os.readlink(process/'exe')!='/usr/bin/sway':
        raise RuntimeError('Sway process identity is not current')
    values=dict(part.split('=',1) for part in (process/'environ').read_bytes().decode(errors='replace').split(chr(0)) if '=' in part)
    node=target_node()
    if any(values.get(key)!=value for key,value in {'XDG_RUNTIME_DIR':str(RUNTIME),
        'DBUS_SESSION_BUS_ADDRESS':BUS,'WLR_BACKENDS':'headless','WLR_RENDERER':'gles2','WLR_RENDER_DRM_DEVICE':node}.items()):
        raise RuntimeError('Sway backend/environment differs from production contract')
    fds=[]
    for file in (process/'fd').iterdir():
        try:
            if os.readlink(file)==node:fds.append(file.name)
        except OSError:continue
    if not fds:raise RuntimeError('Sway has no actual descriptor to the exact Intel VF')
    with LOG.open('rb') as log:
        log.seek(state['sway_log_start_offset']);text=log.read(1024*1024).decode(errors='replace')
    renderers=re.findall(r'GL renderer:\s*([^\r\n]+)',text)
    if not renderers or 'intel' not in renderers[-1].lower() or re.search(r'llvmpipe|softpipe|swiftshader|lavapipe|software raster',renderers[-1],re.I):
        raise RuntimeError('Current Sway run has not proved an Intel hardware GL renderer')
    wayland,ipc=owned_sockets(pid)
    result=subprocess.run(['/usr/bin/swaymsg','-s',str(ipc),'-t','get_outputs','-r'],
        env=local_env(),capture_output=True,text=True,timeout=5,check=True)
    outputs=json.loads(result.stdout)
    if not any(output.get('name')=='HEADLESS-1' and output.get('active') and output.get('rect',{}).get('width')==1600
               and output.get('rect',{}).get('height')==900 for output in outputs):
        raise RuntimeError('1600x900 headless output is not ready')
    state.update(status='WAYLAND_INTEL_READY',runtime_dir=str(RUNTIME),wayland_display=wayland.name,
        sway_socket=str(ipc),user_dbus_address=BUS,guest_vf_bdf=BDF,render_node=node,
        sway_renderer=renderers[-1],sway_render_fds=fds,sway_outputs=outputs,ready_utc=now())
    return state


def wait_ready(seconds):
    deadline=time.monotonic()+seconds;last='Not started'
    while time.monotonic()<deadline:
        try:
            return read_ready()
        except (OSError,RuntimeError,KeyError,ValueError,subprocess.SubprocessError) as error:last=str(error);time.sleep(0.25)
    raise RuntimeError('Wayland readiness failed: '+last)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_subparsers(dest='action',required=True)
    actions.add_parser('run')
    ready=actions.add_parser('ready');ready.add_argument('--wait',type=float,default=30)
    execute=actions.add_parser('exec');execute.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args();os.umask(0o077);guard()
    if args.action=='run':
        node=target_node()
        if RUNTIME.is_symlink():raise RuntimeError('Private runtime must not be a symlink')
        RUNTIME.mkdir(mode=0o700,exist_ok=True)
        if RUNTIME.stat().st_uid!=UID:raise RuntimeError('Private runtime has wrong owner')
        os.chmod(RUNTIME,0o700)
        state={'status':'STARTING_WAYLAND','sway_pid':os.getpid(),'sway_starttime':starttime(os.getpid()),
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'sway_log_start_offset':LOG.stat().st_size if LOG.exists() else 0,'started_utc':now()}
        write_state(state)
        env=local_env();env.update(WLR_BACKENDS='headless',WLR_HEADLESS_OUTPUTS='1',WLR_RENDERER='gles2',
            WLR_RENDER_DRM_DEVICE=node,WLR_LIBINPUT_NO_DEVICES='1')
        os.execvpe('/usr/bin/sway',['/usr/bin/sway','--debug','--config',CONFIG],env)
    state=wait_ready(getattr(args,'wait',30))
    if args.action=='ready':write_state(state);print(json.dumps(state));return
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command or command[0] not in ('/usr/bin/chromium','/usr/bin/python3'):
        raise RuntimeError('Expected the managed Chromium or Python helper')
    os.execvpe(command[0],command,local_env(state))


if __name__=='__main__':main()
