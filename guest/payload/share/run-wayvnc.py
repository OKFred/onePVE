#!/usr/bin/env python3
"""Invoked only in the managed private compositor environment."""
import os
from pathlib import Path
from runtime_config import config

settings = config()
runtime = Path('/run/user') / str(settings['uid']) / 'onepve-agent-wayland'
if os.getuid() != settings['uid'] or os.environ.get('XDG_RUNTIME_DIR') != str(runtime):
    raise RuntimeError('Use the managed Wayland service')
os.execv('/usr/bin/wayvnc', ['/usr/bin/wayvnc', '--config=/dev/null', '--output=HEADLESS-1',
          '--disable-resizing', '--socket=' + str(runtime / 'wayvnc-control.sock'), '127.0.0.1', '5900'])
