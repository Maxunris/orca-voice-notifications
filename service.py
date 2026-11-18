#!/usr/bin/env python3
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = 'io.github.maxunris.orca-voice-notifications'
HOME = Path.home()
ROOT = HOME / 'Library/Application Support/Orca Voice Notifications'
PLIST = HOME / 'Library/LaunchAgents' / (LABEL + '.plist')
DOMAIN = f'gui/{os.getuid()}'
SERVICE = f'{DOMAIN}/{LABEL}'
LAUNCHCTL = '/bin/launchctl'


def run(*args, **kwargs):
    return subprocess.run(args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description='Install or remove the per-user macOS LaunchAgent.')
    parser.add_argument('action', choices=['install', 'uninstall'])
    parser.add_argument('--sounds', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if sys.platform != 'darwin' or os.getuid() == 0:
        parser.error('Run on macOS as the logged-in user, without sudo.')
    if args.action == 'uninstall':
        if run(LAUNCHCTL, 'print', SERVICE, capture_output=True).returncode == 0:
            run(LAUNCHCTL, 'bootout', SERVICE, check=True)
        PLIST.unlink(missing_ok=True)
        print(f'Service removed. Your audio and copied config remain at their original locations; helper files: {ROOT}')
        return
    if not args.sounds:
        parser.error('--sounds is required for install')
    if PLIST.exists() or run(LAUNCHCTL, 'print', SERVICE, capture_output=True).returncode == 0:
        parser.error('The service already exists. Uninstall it explicitly before reinstalling.')
    run(LAUNCHCTL, 'print', DOMAIN, stdout=subprocess.DEVNULL, check=True)
    src = Path(__file__).resolve().parent
    config = (args.config or src / 'sounds.json').expanduser().resolve(strict=True)
    sounds = args.sounds.expanduser().resolve(strict=True)
    profile = ['--profile', str(args.profile.expanduser().resolve(strict=True))] if args.profile else []
    python = str(Path(sys.executable).resolve(strict=True))
    run(python, str(src / 'voice.py'), '--sounds', str(sounds), '--config', str(config), *profile, '--check', check=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / 'voice.py').write_bytes((src / 'voice.py').read_bytes())
    (ROOT / 'sounds.json').write_bytes(config.read_bytes())
    with PLIST.open('xb') as file:
        plistlib.dump({'Label': LABEL, 'ProgramArguments': [python, str(ROOT / 'voice.py'), '--sounds', str(sounds), *profile],
                       'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
                       'StandardOutPath': str(ROOT / 'service.log'), 'StandardErrorPath': str(ROOT / 'service.log')}, file)
    try:
        run(LAUNCHCTL, 'bootstrap', DOMAIN, str(PLIST), check=True)
    except subprocess.CalledProcessError:
        PLIST.unlink()
        raise
    print(f'Service installed: {LABEL}')


if __name__ == '__main__':
    main()
