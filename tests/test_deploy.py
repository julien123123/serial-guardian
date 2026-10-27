import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy_support import deploy_remote


class FakeGuardian:
    def __init__(self):
        self.state = dict(serial_port='/dev/test-board', serial_open=True, boot_count=0,
                          connected=True, halted=False, monitoring=True, stop_armed=False,
                          pause_pending=False)
        self.actions = []
        self.closing = 0
        self.no_boot = False

    def status(self):
        if self.closing:
            self.closing -= 1
            if not self.closing:
                self.state.update(serial_open=False, connected=False, halted=False)
        return dict(self.state)

    def call(self, path, post=False):
        assert post
        self.actions.append(path)
        if path == '/api/stop':
            self.state.update(halted=True, stop_armed=False)
        elif path == '/api/monitoring/pause':
            self.state['monitoring'] = False
            self.closing = 2
        elif path == '/api/monitoring/resume':
            self.state.update(monitoring=True, connected=True, serial_open=True)
        elif path == '/api/device/reset' and not self.no_boot:
            self.state['boot_count'] += 1
        return dict(self.state)


class DeployTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.files = [str(Path(tmp.name) / 'first file.mpy'), str(Path(tmp.name) / "second'file.mpy")]
        for file in self.files:
            Path(file).write_bytes(b'compiled')
        self.guardian = FakeGuardian()
        self.find = patch('deploy_remote.shutil.which', return_value='/usr/bin/mpremote')
        self.find.start(); self.addCleanup(self.find.stop)
        self.sleep = patch('deploy_remote.time.sleep')
        self.sleep.start(); self.addCleanup(self.sleep.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__(); self.addCleanup(self.output.__exit__, None, None, None)

    def run_deploy(self, **kwargs):
        deploy_remote.deploy(self.guardian, self.files, 'mpremote', wait=.01, boot_wait=.01, **kwargs)

    def test_handoff_one_connection_and_boot(self):
        def transfer(command, **kwargs):
            self.assertFalse(self.guardian.state['serial_open'])
            self.assertFalse(self.guardian.state['monitoring'])
            self.assertEqual(command, ['mpremote', 'connect', '/dev/test-board', 'resume', 'fs', 'cp'] + self.files + [':'])
        with patch('deploy_remote.subprocess.run', side_effect=transfer) as runner:
            self.run_deploy()
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(self.guardian.actions, ['/api/stop', '/api/monitoring/pause', '/api/monitoring/resume', '/api/device/reset'])

    def test_optional_reset_restores_monitoring_without_reset_or_boot_wait(self):
        self.guardian.no_boot = True
        with patch('deploy_remote.subprocess.run'):
            self.run_deploy(reset=False)
        self.assertTrue(self.guardian.state['monitoring'])
        self.assertNotIn('/api/device/reset', self.guardian.actions)
        self.assertEqual(self.guardian.actions[-1], '/api/monitoring/resume')

    def test_failed_transfer_never_resumes_or_resets(self):
        with patch('deploy_remote.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'mpremote')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_deploy()
        self.assertFalse(self.guardian.state['monitoring'])
        self.assertNotIn('/api/device/reset', self.guardian.actions)
        self.assertNotIn('/api/monitoring/resume', self.guardian.actions)

    def test_missing_mpremote_leaves_device_untouched(self):
        with patch('deploy_remote.shutil.which', return_value=None):
            with self.assertRaises(deploy_remote.DeploymentError):
                self.run_deploy()
        self.assertEqual(self.guardian.actions, [])

    def test_already_stopped_is_not_toggled(self):
        self.guardian.state['halted'] = True
        with patch('deploy_remote.subprocess.run'):
            self.run_deploy()
        self.assertNotIn('/api/stop', self.guardian.actions)

    def test_pending_stop_is_not_toggled_off(self):
        self.guardian.state['stop_armed'] = True
        status = self.guardian.status
        calls = 0
        def status_with_wake():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.guardian.state.update(halted=True, stop_armed=False)
            return status()
        self.guardian.status = status_with_wake
        with patch('deploy_remote.subprocess.run'):
            self.run_deploy()
        self.assertNotIn('/api/stop', self.guardian.actions)

    def test_queued_pause_can_finish_before_next_status_poll(self):
        self.guardian.state.update(stop_armed=True, pause_pending=True)
        status = self.guardian.status
        calls = 0
        def status_with_deferred_pause():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.guardian.state.update(halted=False, monitoring=False, connected=False,
                                           serial_open=False, stop_armed=False, pause_pending=False)
            return status()
        self.guardian.status = status_with_deferred_pause
        with patch('deploy_remote.subprocess.run'):
            self.run_deploy()
        self.assertNotIn('/api/stop', self.guardian.actions)
        self.assertNotIn('/api/monitoring/pause', self.guardian.actions)

    def test_boot_marker_is_required(self):
        self.guardian.no_boot = True
        with patch('deploy_remote.subprocess.run'):
            with self.assertRaisesRegex(deploy_remote.DeploymentError, 'boot marker'):
                self.run_deploy()
        self.assertEqual(self.guardian.actions.count('/api/device/reset'), 1)

    def test_outdated_guardian_does_not_stop_device(self):
        del self.guardian.state['serial_open']
        with self.assertRaisesRegex(deploy_remote.DeploymentError, 'Update and restart'):
            self.run_deploy()
        self.assertEqual(self.guardian.actions, [])
