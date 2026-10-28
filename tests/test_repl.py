import copy
import os
import pty
import select
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from monitor import SerialSessionMonitor
from webapp import create_app


class ReplTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = SimpleNamespace(**{
            key: copy.deepcopy(getattr(config, key))
            for key in dir(config) if key.isupper()
        })
        self.cfg.DATA_DIR = temp.name
        self.cfg.SERIAL_PORT = str(Path(temp.name) / 'device')
        self.cfg.MIN_FREE_DISK_MB = 0
        self.monitor = SerialSessionMonitor(self.cfg)
        self.client = create_app(self.monitor, self.cfg).test_client()

    def test_repl_resumes_monitoring_and_catches_the_next_connection(self):
        self.monitor.pause_monitoring()
        for _ in range(2):
            state = self.client.post('/api/device/repl').json
            self.assertTrue(state['monitoring'])
            self.assertTrue(state['stop_armed'])
            self.assertFalse(state['halted'])
        self.monitor.pause_monitoring()  # Queue a pause behind the interrupt.
        self.assertTrue(self.monitor.get_status()['pause_pending'])
        self.assertFalse(self.client.post('/api/device/repl').json['pause_pending'])

        master, slave = pty.openpty()
        os.symlink(os.ttyname(slave), self.cfg.SERIAL_PORT)
        errors = []
        def capture():
            try:
                self.monitor._run_one_cycle()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=capture, daemon=True)
        worker.start()
        try:
            received = b''
            deadline = time.monotonic() + 2
            while len(received) < len(self.cfg.STOP_BYTES) and time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    received += os.read(master, 1024)
            self.assertEqual(received, self.cfg.STOP_BYTES)
            state = self.monitor.get_status()
            self.assertTrue(state['halted'])
            self.assertTrue(state['monitoring'])
            self.assertFalse(state['stop_armed'])
            self.assertEqual(state['stop_count'], 1)
            self.client.post('/api/device/repl')  # Already halted: no second interrupt.
            self.assertEqual(self.monitor.get_status()['stop_count'], 1)
            self.assertFalse(select.select([master], [], [], .05)[0])

            self.client.post('/api/resume')
            self.assertTrue(select.select([master], [], [], 1)[0])
            self.assertEqual(os.read(master, 1024), b'\x04')
        finally:
            os.close(master)
            os.close(slave)
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_connected_repl_interrupt_cancels_exception_recovery(self):
        class Port:
            def __init__(self): self.writes = []
            def write(self, data): self.writes.append(data)
            def flush(self): pass
        port = Port()
        self.monitor._ser = port
        self.monitor.connected = True
        self.monitor._pending_soft_reset_at = time.time() - 1
        self.monitor._awaiting_boot = True
        self.monitor._boot_deadline = time.time() - 1
        state = self.client.post('/api/device/repl').json
        self.assertTrue(state['halted'])
        self.monitor._handle_line(port, self.cfg.BOOT_BANNER_MARKER + '\n')
        self.assertTrue(self.monitor.get_status()['halted'])
        self.monitor._handle_line(port, 'Traceback (most recent call last):\n')
        self.monitor._handle_line(port, 'KeyboardInterrupt\n')
        with patch.object(self.monitor, '_hard_reset') as reset:
            self.monitor._check_boot_timeout(port)
        reset.assert_not_called()
        self.assertEqual(port.writes, [self.cfg.STOP_BYTES])
        self.assertIsNone(self.monitor._pending_soft_reset_at)
        self.assertFalse(self.monitor._awaiting_boot)
        self.assertFalse(self.monitor._is_exception)
