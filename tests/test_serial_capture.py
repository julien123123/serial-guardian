import copy
import json
import os
from pathlib import Path
import pty
import select
import tempfile
import threading
import time
import tty
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import serial
import config
from monitor import SerialSessionMonitor
import serial_capture
from serial_capture import CaptureSerial, read_chunk, wait_for_port


@unittest.skipUnless(os.name == 'posix', 'POSIX serial capture')
class CaptureTest(unittest.TestCase):
    def test_open_preserves_queued_startup_bytes_and_explicit_flush_still_works(self):
        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            os.write(master, b'early wake print\n')
            self.assertTrue(select.select([slave], [], [], 1)[0])
            with CaptureSerial(os.ttyname(slave), 115200, timeout=.02) as port:
                self.assertEqual(read_chunk(port, .1), b'early wake print\n')
                os.write(master, b'explicitly discard')
                self.assertTrue(select.select([slave], [], [], 1)[0])
                port.reset_input_buffer()
                self.assertEqual(read_chunk(port, .02), b'')
        finally:
            os.close(master); os.close(slave)

    def test_stock_pyserial_open_discards_the_same_queued_bytes(self):
        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            os.write(master, b'early wake print\n')
            self.assertTrue(select.select([slave], [], [], 1)[0])
            with serial.Serial(os.ttyname(slave), 115200, timeout=.02) as port:
                self.assertEqual(port.read(100), b'')
        finally:
            os.close(master); os.close(slave)

    def test_received_bytes_are_returned_before_a_subsequent_read_failure(self):
        master, slave = pty.openpty()
        try:
            with CaptureSerial(os.ttyname(slave), 115200, timeout=.1) as port:
                ready = ([port.fileno()], [], [])
                # Stock read(size) throws away its accumulated four bytes if
                # the following OS read disconnects before size is satisfied.
                with patch('select.select', return_value=ready), patch('os.read', side_effect=[b'1234', OSError('USB disconnected')]):
                    with self.assertRaises(serial.SerialException): port.read(8)
                with patch('select.select', return_value=ready), patch('os.read', side_effect=[b'1234', OSError('USB disconnected')]):
                    self.assertEqual(read_chunk(port, .1), b'1234')
                    with self.assertRaises(serial.SerialException): read_chunk(port, .1)
        finally:
            os.close(master); os.close(slave)

    @unittest.skipUnless(serial_capture._libc is not None, 'Linux inotify')
    def test_device_arrival_wakes_wait_and_does_not_leak_descriptors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'tty-test'
            def arrive():
                time.sleep(.02); path.touch()
            thread = threading.Thread(target=arrive)
            thread.start()
            try:
                self.assertTrue(wait_for_port(str(path), timeout=1))
            finally:
                thread.join(timeout=2)
            path.unlink()
            before = len(os.listdir('/proc/self/fd'))
            for _ in range(10): self.assertFalse(wait_for_port(str(path), timeout=.001))
            self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_existing_index_can_be_loaded_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(**{key: copy.deepcopy(getattr(config, key)) for key in dir(config) if key.isupper()})
            cfg.DATA_DIR = temp
            Path(temp, 'index.jsonl').write_text(json.dumps(dict(id=12, status='NORMAL', start=1, duration=1, lines=1))+'\n')
            monitor = SerialSessionMonitor(cfg)
            self.assertEqual(monitor.next_id, 13)
            self.assertEqual(monitor.stats['NORMAL'], 1)
            self.assertEqual(monitor.get_status()['empty_sessions'], 0)

    def test_fallback_is_bounded_without_inotify(self):
        with patch('serial_capture._libc', None), patch('serial_capture.time.sleep') as sleep:
            self.assertFalse(wait_for_port('/does/not/exist'))
            sleep.assert_called_once_with(.02)

    def test_repeated_100ms_wakes_capture_startup_and_unterminated_last_line(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(**{key: copy.deepcopy(getattr(config, key)) for key in dir(config) if key.isupper()})
            cfg.DATA_DIR = temp
            cfg.SERIAL_PORT = str(Path(temp)/'device')
            cfg.MIN_FREE_DISK_MB = 0
            monitor = SerialSessionMonitor(cfg)
            stop = threading.Event()
            errors = []
            def capture():
                try:
                    while not stop.is_set(): monitor._run_one_cycle()
                except BaseException as exc: errors.append(exc)
            worker = threading.Thread(target=capture, daemon=True)
            worker.start()
            expected = []
            try:
                for cycle in range(12):
                    master, slave = pty.openpty()
                    try:
                        tty.setraw(slave)
                        text = ''.join(f'wake {cycle} line {i}: café\n' for i in range(30)) + cfg.GOTOSLEEP_MARKER
                        expected.append(text)
                        os.write(master, text.encode())  # Already queued before open.
                        os.symlink(os.ttyname(slave), cfg.SERIAL_PORT)
                        time.sleep(.1)  # Ten times shorter than the reported wake window.
                        os.unlink(cfg.SERIAL_PORT)
                    finally:
                        os.close(master); os.close(slave)
                    deadline = time.monotonic()+2
                    while monitor.last_session_id < cycle+1 and not errors and time.monotonic()<deadline:
                        time.sleep(.002)
                    self.assertFalse(errors, errors)
                    self.assertEqual(monitor.last_session_id, cycle+1)
                records = list(monitor.iter_records())
                self.assertEqual(len(records), 12)
                self.assertTrue(all(record['status']=='NORMAL' for record in records))
                for record, text in zip(records, expected):
                    self.assertEqual(monitor.read_session_text(record['id']), text)
                state = monitor.get_status()
                self.assertEqual(state['serial_connections'], 12)
                self.assertEqual(state['empty_sessions'], 0)
                self.assertEqual(state['serial_bytes_received'], sum(len(text.encode()) for text in expected))
            finally:
                stop.set(); worker.join(timeout=2)

    def test_15_second_timeout_does_not_delay_pause_on_a_quiet_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(**{key: copy.deepcopy(getattr(config, key)) for key in dir(config) if key.isupper()})
            cfg.DATA_DIR = temp
            cfg.SERIAL_READ_TIMEOUT = 15.0
            master, slave = pty.openpty()
            cfg.SERIAL_PORT = os.ttyname(slave)
            monitor = SerialSessionMonitor(cfg)
            worker = threading.Thread(target=monitor._run_one_cycle, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic()+2
                while not monitor.get_status()['connected'] and time.monotonic()<deadline: time.sleep(.001)
                self.assertTrue(monitor.get_status()['connected'])
                monitor.pause_monitoring()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), 'Pause was blocked by the 15-second read timeout')
                self.assertFalse(monitor.get_status()['serial_open'])
            finally:
                os.close(master); os.close(slave)
                worker.join(timeout=2)

    def test_quiet_exception_still_runs_delayed_reset_and_records_its_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(**{key: copy.deepcopy(getattr(config, key)) for key in dir(config) if key.isupper()})
            cfg.DATA_DIR = temp
            monitor = SerialSessionMonitor(cfg)
            class Serial:
                def __init__(self): self.writes = []
                def write(self, data): self.writes.append(data)
                def flush(self): pass
            port = Serial()
            monitor._begin_session()
            monitor._handle_line(port, 'Traceback (most recent call last):\n')
            self.assertEqual(port.writes, [])
            monitor._pending_soft_reset_at = time.time()-1
            monitor._check_boot_timeout(port)  # No further serial lines arrive.
            self.assertEqual(port.writes, [cfg.SOFT_RESET_BYTES])
            monitor._finish_session()
            record = list(monitor.iter_records())[0]
            self.assertEqual(record['status'], 'EXCEPTION')
            self.assertEqual(record['exception_marker'], 'Traceback (most recent call last):')
            from webapp import create_app
            exported = create_app(monitor, cfg).test_client().post('/export').json
            self.assertEqual(exported['sessions'][0]['exception_marker'], record['exception_marker'])

    def test_log_lookup_does_not_scan_the_session_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(**{key: copy.deepcopy(getattr(config, key)) for key in dir(config) if key.isupper()})
            cfg.DATA_DIR = temp
            monitor = SerialSessionMonitor(cfg)
            for sid, status in enumerate(['NORMAL', 'ANOMALY', 'EXCEPTION', 'STOPPED'], 1):
                Path(monitor.sessions_dir, f'{sid:06d}_{status}.log').write_text(status)
            with patch('monitor.glob.glob', side_effect=AssertionError('unexpected directory scan')):
                for sid, status in enumerate(['NORMAL', 'ANOMALY', 'EXCEPTION', 'STOPPED'], 1):
                    self.assertEqual(monitor.read_session_text(sid), status)
                    self.assertEqual(monitor.read_session_text(sid, max_chars=2), status[:2])
                self.assertIsNone(monitor.read_session_text(99))
