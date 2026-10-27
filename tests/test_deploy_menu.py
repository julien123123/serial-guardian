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
                for tag in '123645': args.extend([tag, 'Reset [x]' if tag == '6' else 'Operation '+tag])
                process = subprocess.Popen(args, stdin=slave, stdout=result, stderr=slave,
                    preexec_fn=setup, env=dict(os.environ, TERM='xterm-256color'))
                os.close(slave)
                try:
                    self.assertTrue(select.select([master], [], [], 5)[0], 'menu did not render')
                    os.read(master, 65536)
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
