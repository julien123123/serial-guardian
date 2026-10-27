#!/usr/bin/env python3
"""Short-lived deployment coordinator, staged on the Pi by compile_deploy.sh."""
import argparse
import fcntl
import json
import math
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error


class DeploymentError(RuntimeError):
    pass


class Guardian:
    def __init__(self, url):
        self.url = url.rstrip('/')

    def call(self, path, post=False):
        req = urllib.request.Request(self.url + path, data=b'{}' if post else None,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def status(self):
        try:
            return self.call('/api/status')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise DeploymentError('Update and restart Serial Guardian on the Pi first (/api/status unavailable)') from exc
            raise


def wait_for(guardian, predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while True:
        state = guardian.status()
        if predicate(state):
            return state
        if time.monotonic() >= deadline:
            raise DeploymentError('Timed out: ' + description)
        time.sleep(0.25)


def deploy(guardian, files, mpremote, wait=300, boot_wait=30, transfer_wait=120, reset=True):
    command = shlex.split(mpremote)
    if not command or not shutil.which(command[0]):
        raise DeploymentError('mpremote executable not found on the Pi: ' + mpremote)
    if not files or any(not Path(path).is_file() for path in files):
        raise DeploymentError('Staged deployment files are missing')
    state = guardian.status()  # Fail before stopping anything if Guardian is outdated.
    required = {'serial_port', 'serial_open', 'boot_count', 'halted', 'monitoring', 'stop_armed'}
    if not required <= state.keys():
        raise DeploymentError('Update and restart Serial Guardian on the Pi first')
    port = state['serial_port']
    print('Waiting for device wake / Stop to take effect…', flush=True)
    if not state['monitoring']:
        guardian.call('/api/monitoring/resume', post=True)
    # Stop and Pause are toggles. Never blindly retry them.
    if not state['halted'] and not state['stop_armed']:
        guardian.call('/api/stop', post=True)
    queued_pause = state.get('pause_pending', False)
    wait_for(guardian, lambda s: s['halted'] or (queued_pause and not s['monitoring']
             and not s['serial_open'] and not s['connected']), wait, 'device Stop')
    print('Stop sent. Releasing the serial port…', flush=True)
    state = guardian.status()
    if state['monitoring'] and not state.get('pause_pending'):
        guardian.call('/api/monitoring/pause', post=True)
    wait_for(guardian, lambda s: not s['monitoring'] and not s['serial_open'] and not s['connected'],
             wait, 'serial port release')
    print('Serial port released. Transferring {} file(s)…'.format(len(files)), flush=True)
    # A single mpremote connection, explicit port, no implicit soft reboot.
    # A partial/failed transfer raises before any resume or reset request.
    subprocess.run(command + ['connect', port, 'resume', 'fs', 'cp'] + list(files) + [':'],
                   check=True, timeout=transfer_wait)
    print('Transfer complete. Restoring monitoring…', flush=True)
    guardian.call('/api/monitoring/resume', post=True)
    state = wait_for(guardian, lambda s: s['monitoring'] and s['connected'] and s['serial_open'],
                     wait, 'monitor reconnect')
    if not reset:
        print('Monitoring restored. Device left at the REPL; use Reset in Guardian when ready.', flush=True)
        return
    baseline = state['boot_count']
    guardian.call('/api/device/reset', post=True)
    print('Reset requested. Waiting for the configured boot marker…', flush=True)
    wait_for(guardian, lambda s: s['boot_count'] > baseline, boot_wait, 'boot marker (files transferred; inspect Live)')
    print('Boot detected. Watch the Guardian Live dashboard.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--mpremote', default='mpremote')
    parser.add_argument('--wait', type=float, default=300)
    parser.add_argument('--boot-wait', type=float, default=30)
    parser.add_argument('--transfer-wait', type=float, default=120)
    parser.add_argument('--no-reset', action='store_true', help='restore monitoring but leave the board at the REPL')
    parser.add_argument('files', nargs='+')
    args = parser.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in (args.wait, args.boot_wait, args.transfer_wait)):
        parser.error('timeouts must be positive')
    # Serialize cooperating deploy invocations on this Pi, including different
    # staging directories. The advisory lock is released on every exit path.
    with open('/tmp/serial-guardian-deploy.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another deployment is running on this Pi.', file=sys.stderr)
            return 1
        try:
            deploy(Guardian(args.url), args.files, args.mpremote,
                   args.wait, args.boot_wait, args.transfer_wait, reset=not args.no_reset)
        except (Exception, KeyboardInterrupt) as exc:
            print('Deployment stopped: {}\nNo further reset/resume will be attempted. '
                  'If transfer failed, leave the device halted and retry; '
                  'a pending Stop may need cancellation in the dashboard.'.format(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
