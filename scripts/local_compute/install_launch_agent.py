#!/usr/bin/env python3
"""Install the explicitly requested local research schedule, scoped to this Mac."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import os

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config', required=True, type=Path)
args = parser.parse_args()
config = args.config.resolve()
data = json.loads(config.read_text())
state = Path(data['state_dir']).resolve()
state.mkdir(parents=True, exist_ok=True, mode=0o700)
label = 'com.weatheredge.v7.local-research'
path = Path.home() / 'Library/LaunchAgents' / f'{label}.plist'
path.parent.mkdir(parents=True, exist_ok=True)
content = {'Label': label,
           'ProgramArguments': ['/usr/bin/caffeinate', '-i', sys.executable,
                                str(Path(__file__).with_name('worker.py').resolve()),
                                '--config', str(config)],
           'StartInterval': 21600, 'RunAtLoad': True,
           'ProcessType': 'Background', 'LowPriorityIO': True, 'Nice': 10,
           'StandardOutPath': str(state / 'launchd.stdout'),
           'StandardErrorPath': str(state / 'launchd.stderr')}
path.write_bytes(plistlib.dumps(content))
os.chmod(path, 0o600)
# Refresh only our label; other local jobs are untouched.
subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(['launchctl', 'enable', f'gui/{os.getuid()}/{label}'], check=True)
subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
print('Installed local V7 research worker: every six hours, with job-scoped caffeinate.')
