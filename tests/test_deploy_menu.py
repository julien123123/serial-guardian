import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

WHIPTAIL = os.environ.get('GUARDIAN_TEST_WHIPTAIL') or shutil.which('whiptail')


@unittest.skipUnless(WHIPTAIL, 'whiptail is required for real terminal keyboard tests')
class MenuKeyboardTest(unittest.TestCase):
    def test_numbers_activate_without_enter_and_arrows_still_work(self):
        script = (Path(__file__).resolve().parents[1]/'compile_deploy.sh').read_text()
        code = script.split("cat <<'NUMBER_MENU_PY'\n", 1)[1].split('\nNUMBER_MENU_PY\n', 1)[0]
        for key, expected in [(b'1', b'1'), (b'2', b'2'), (b'3', b'3'), (b'4', b'4'), (b'5', b'5'), (b'6', b'6'), (b'\x1b[B\r', b'2')]:
            with self.subTest(key=key), tempfile.TemporaryFile() as result:
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 110, 0, 0))
                def setup():
                    os.setsid()
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                args = [sys.executable, '-c', code, WHIPTAIL, '--menu', 'Choose', '18', '85', '6']
                for tag in '123456': args.extend([tag, 'Reset [x]' if tag == '6' else 'Operation '+tag])
                process = subprocess.Popen(args, stdin=slave, stdout=result, stderr=slave,
                    preexec_fn=setup, env=dict(os.environ, TERM='xterm-256color'))
                os.close(slave)
                try:
                    rendered = bytearray()
                    deadline = time.monotonic()+5
                    while b'Reset [x]' not in rendered and time.monotonic() < deadline:
                        if select.select([master], [], [], .05)[0]:
                            rendered.extend(os.read(master, 65536))
                    self.assertIn(b'Reset [x]', rendered, 'menu did not finish rendering')
                    os.write(master, key)  # Numeric cases deliberately omit Enter.
                    deadline = time.monotonic()+5
                    while process.poll() is None and time.monotonic() < deadline:
                        if select.select([master], [], [], .05)[0]:
                            try: os.read(master, 65536)
                            except OSError: pass
                    self.assertIsNotNone(process.poll(), 'number did not activate menu item')
                    self.assertEqual(process.returncode, 0)
                    result.seek(0)
                    self.assertEqual(result.read(), expected)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=5)
                    os.close(master)

@unittest.skipUnless(WHIPTAIL, 'whiptail is required for full terminal integration tests')
class FullScriptMenuTest(unittest.TestCase):
    def test_real_terminal_choice_reaches_shell_and_operation_exits(self):
        # Use the deployment fixture's harmless compiler/SSH/SCP stand-ins,
        # but run the real menu AND real file selector through the full .sh.
        from test_deploy_script import DeployScriptTest
        for choice in [b'3', b'1', b'6']:
            with self.subTest(choice=choice):
                fixture = DeployScriptTest()
                fixture.setUp()
                process = None
                master = None
                try:
                    (fixture.bin/'whiptail').unlink()
                    (fixture.bin/'whiptail').symlink_to(WHIPTAIL)
                    master, slave = pty.openpty()
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 110, 0, 0))
                    def setup():
                        os.setsid()
                        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                    process = subprocess.Popen(['bash', str(fixture.script)], stdin=slave,
                        stdout=slave, stderr=slave, preexec_fn=setup,
                        env=dict(fixture.env, TERM='xterm-256color'))
                    os.close(slave)
                    output = bytearray()
                    def wait_text(text):
                        deadline = time.monotonic()+5
                        start = len(output)
                        while time.monotonic() < deadline:
                            if select.select([master], [], [], .05)[0]:
                                try: output.extend(os.read(master, 65536))
                                except OSError: break
                            if text in output[start:]: return
                        self.fail('Did not reach '+repr(text)+'; output='+repr(output[-1000:]))
                    wait_text(b'[x] Reset device after transfer')
                    os.write(master, choice)  # No Enter.
                    if choice == b'6':
                        wait_text(b'[ ] Reset device after transfer')
                        os.write(master, b'3')
                    elif choice == b'1':
                        wait_text(b'Select modules to deploy')
                        os.write(master, b' \r')  # Select first file and confirm.
                    deadline = time.monotonic()+8
                    while process.poll() is None and time.monotonic() < deadline:
                        if select.select([master], [], [], .05)[0]:
                            try: output.extend(os.read(master, 65536))
                            except OSError: pass
                    self.assertIsNotNone(process.poll(), 'full script did not execute/exit: '+repr(output[-1000:]))
                    self.assertEqual(process.returncode, 0, repr(output[-1000:]))
                    rows = fixture.trace_rows()
                    self.assertEqual(sum(row[0]=='compiler' for row in rows), 2)
                    self.assertTrue((fixture.output/'a file.mpy').exists())
                    if choice == b'1':
                        self.assertEqual(sum(row[0]=='scp' for row in rows), 1)
                        self.assertIn(b'Deployment complete.', output)
                    else:
                        self.assertFalse(any(row[0] in ['ssh','scp'] for row in rows))
                finally:
                    if process is not None and process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=5)
                    if master is not None: os.close(master)
                    fixture.doCleanups()
