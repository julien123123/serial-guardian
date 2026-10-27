import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

from deploy_support import source


class CancellationTest(unittest.TestCase):
    def test_interrupt_and_disconnect_release_remote_lock(self):
        actions = []
        state = dict(serial_port='/dev/test', serial_open=False, connected=False,
                     boot_count=0, halted=False, monitoring=True, stop_armed=True)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(state).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                try: self.wfile.write(body)
                except BrokenPipeError: pass  # expected if cancellation interrupts a request

            def do_POST(self):
                actions.append(self.path)
                self.do_GET()

            def log_message(self, *args): pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                lock_path = base/'deploy.lock'
                helper = base/'helper.py'
                helper.write_text(source.replace("'/tmp/serial-guardian-deploy.lock'", repr(str(lock_path))))
                module = base/'clock.mpy'; module.write_bytes(b'test')
                for sig in [signal.SIGINT, signal.SIGHUP, signal.SIGTERM]:
                    with self.subTest(signal=sig):
                        process = subprocess.Popen([sys.executable, str(helper), '--url',
                            f'http://127.0.0.1:{server.server_port}', '--mpremote', '/bin/true',
                            '--wait', '60', str(module)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        try:
                            self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'helper did not start')
                            self.assertIn('Waiting for device wake', process.stdout.readline())
                            process.send_signal(sig)
                            out, err = process.communicate(timeout=5)
                            self.assertEqual(process.returncode, 1, out+err)
                            self.assertIn('Deployment stopped', err)
                            with open(lock_path, 'a+') as lock:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        finally:
                            if process.poll() is None:
                                process.kill(); process.wait(timeout=5)
                            process.stdout.close(); process.stderr.close()
                self.assertEqual(actions, [])  # no reset/resume requests on cancellation
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
