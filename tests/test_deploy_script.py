import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, shlex, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['TRACE'], 'a') as f:
    f.write(json.dumps([name] + sys.argv[1:]) + '\n')
if name == 'compiler':
    if os.environ.get('FAIL_COMPILE'): sys.exit(42)
    pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(b'compiled')
elif name == 'ssh':
    command = sys.argv[-1]
    if command.startswith('mktemp'): print('/tmp/serial-guardian-deploy.Test1234')
    elif not command.startswith('rm '):
        with open(os.environ['REMOTE_ARGS'], 'w') as f: json.dump(shlex.split(command), f)
        sys.exit(int(os.environ.get('FAIL_REMOTE', '0')))
elif name == 'scp':
    files = sys.argv[2:-1]
    assert files and all(pathlib.Path(path).suffix == '.mpy' for path in files), files
    pathlib.Path(os.environ['TRANSFER_FILES']).write_text(json.dumps(files))
    sys.exit(int(os.environ.get('FAIL_SCP', '0')))
elif name == 'whiptail':
    if '--menu' in sys.argv:
        if os.environ.get('CANCEL_MENU'): sys.exit(1)
        counter = pathlib.Path(os.environ['MENU_COUNTER'])
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count+1))
        choices = os.environ.get('MENU_CHOICES', '15')
        print(choices[count] if count < len(choices) else '5', file=sys.stderr)
    elif '--checklist' in sys.argv:
        if os.environ.get('CANCEL_FILES'): sys.exit(1)
        print("a file.mpy\nb'file.mpy", file=sys.stderr)
'''


class DeployScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.standalone = self.base / 'standalone'; self.standalone.mkdir()
        self.script = self.standalone / 'compile_deploy.sh'
        shutil.copy(ROOT / 'compile_deploy.sh', self.script)
        self.bin = self.base / 'bin'; self.bin.mkdir()
        for tool in ['ssh', 'scp', 'whiptail', 'compiler']:
            path = self.bin / tool; path.write_text(STUB); path.chmod(0o755)
        self.source = self.base / 'source'; self.source.mkdir()
        self.output = self.base / 'built'; self.output.mkdir()
        for name in ['a file.py', "b'file.py"]:
            (self.source / name).write_text('pass\n')
        self.config = self.base / 'config/serial-guardian'; self.config.mkdir(parents=True)
        self.trace = self.base / 'trace'
        self.remote = self.base / 'remote-args'
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        XDG_CONFIG_HOME=str(self.base/'config'), TRACE=str(self.trace),
                        REMOTE_ARGS=str(self.remote), MENU_COUNTER=str(self.base/'menu-counter'), TRANSFER_FILES=str(self.base/'transfer-files'))
        self.write_config()

    def write_config(self, remembered=False):
        values = dict(SCRIPT_DIR=str(self.standalone), SOURCE_DIR=str(self.source), OUTPUT_DIR=str(self.output),
                      MPY_CROSS=str(self.bin/'compiler'), SSH_TARGET='pi@test-host', MPREMOTE='python3 -m mpremote')
        if remembered:
            values['LAST_SELECTION_KEY'] = f'{self.source}|{self.output}|pi@test-host'
        text = ''.join(f'{key}={shlex.quote(value)}\n' for key, value in values.items())
        if remembered: text += "LAST_SELECTED_FILES=(" + ' '.join(map(shlex.quote, ['a file.mpy', "b'file.mpy"])) + ')\n'
        (self.config/'compile_deploy.conf').write_text(text)

    def run_script(self, *args, **extra):
        menu_input = extra.pop('MENU_INPUT', '15')
        return subprocess.run(['bash', str(self.script), *args], env=dict(self.env, MENU_CHOICES=menu_input, **extra),
                              capture_output=True, text=True, timeout=15)

    def trace_rows(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_standalone_script_restores_dialog_with_checked_reset_on_first_screen(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        rows = self.trace_rows()
        self.assertEqual(sum(row[0] == 'compiler' for row in rows), 2)
        menu = next(row for row in rows if row[0]=='whiptail' and '--menu' in row)
        items = menu[menu.index('--menu')+5:]
        self.assertEqual(items[::2], list('123456'))
        self.assertEqual(items[items.index('1')+1], 'Compile all and deploy selected files')
        self.assertEqual(items[items.index('6')+1], '[x] Reset device after transfer')
        self.assertFalse((self.standalone/'deploy_remote.py').exists())
        transferred = json.loads((self.base/'transfer-files').read_text())
        self.assertEqual([Path(path).name for path in transferred], ['a file.mpy', "b'file.mpy"])
        args = json.loads(self.remote.read_text())
        self.assertNotIn('--no-reset', args)
        self.assertEqual(args[:2], ['python3', '-c'])
        compile(args[2], '<inline coordinator>', 'exec')
        self.assertEqual(sum(row[0]=='whiptail' and '--menu' in row for row in rows), 1)
        self.assertFalse(any('--msgbox' in row for row in rows))
        remote_ssh = next(row for row in rows if row[0]=='ssh' and "'-c'" in row[-1])
        self.assertIn('-tt', remote_ssh)
        self.assertEqual(args[args.index('--mpremote')+1], 'python3 -m mpremote')
        self.assertEqual(args[-2:], ['/tmp/serial-guardian-deploy.Test1234/a file.mpy', "/tmp/serial-guardian-deploy.Test1234/b'file.mpy"])
        self.assertLess(next(i for i,r in enumerate(rows) if r[0]=='scp'), next(i for i,r in enumerate(rows) if r[0]=='ssh' and "'-c'" in r[-1]))
        self.assertFalse(any('Deployment options' in row for row in rows))

    def test_selections_start_empty_every_time_even_with_old_config(self):
        self.write_config(remembered=True)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        (self.base/'menu-counter').unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        checklists = [row for row in self.trace_rows() if row[0]=='whiptail' and '--checklist' in row and 'reset' not in row]
        self.assertEqual(len(checklists), 2)
        for checklist in checklists:
            self.assertEqual(checklist[checklist.index('a file.mpy')+2], 'OFF')
            self.assertEqual(checklist[checklist.index("b'file.mpy")+2], 'OFF')

    def test_reset_can_be_disabled(self):
        result = self.run_script(MENU_INPUT='615')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-reset', json.loads(self.remote.read_text()))
        menus = [row for row in self.trace_rows() if row[0]=='whiptail' and '--menu' in row]
        items = menus[1][menus[1].index('--menu')+5:]
        self.assertEqual(items[items.index('6')+1], '[ ] Reset device after transfer')
        self.assertEqual(menus[1][menus[1].index('--default-item')+1], '6')

    def test_cancel_file_selection_leaves_device_untouched(self):
        result = self.run_script(CANCEL_FILES='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(row[0] in ['ssh','scp'] for row in self.trace_rows()))

    def test_compile_failure_does_not_stage_or_delete_previous_build(self):
        old = self.output / 'previous.mpy'; old.write_bytes(b'previous')
        result = self.run_script(FAIL_COMPILE='1')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Compilation failed', result.stderr)
        self.assertEqual(old.read_bytes(), b'previous')
        self.assertFalse(any(row[0] in ['ssh','scp'] for row in self.trace_rows()))

    def test_staging_failure_never_starts_coordinator(self):
        result = self.run_script(FAIL_SCP='1')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.remote.exists())
        self.assertTrue(any(row[0]=='ssh' and row[-1].startswith('rm ') for row in self.trace_rows()))

    def test_compile_only_is_option_three(self):
        result = self.run_script(MENU_INPUT='35')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(row[0]=='compiler' for row in self.trace_rows()), 2)
        self.assertFalse(any(row[0] in ['ssh','scp'] for row in self.trace_rows()))
