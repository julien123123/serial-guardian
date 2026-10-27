import copy
import json
import tempfile
import unittest
from pathlib import Path

import config
import labels
from monitor import SerialSessionMonitor
from webapp import create_app


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = type('Config', (), {k: copy.deepcopy(getattr(config, k)) for k in dir(config) if k.isupper()})
        self.cfg.DATA_DIR = self.tmp.name
        self.monitor = SerialSessionMonitor(self.cfg)
        self.records = []
        for sid, status in enumerate(['NORMAL', 'ANOMALY', 'EXCEPTION', 'ANOMALY', 'STOPPED'], 1):
            record = dict(id=sid, status=status, start=1000 + sid * 10, duration=2, lines=1, file=f'{sid:06d}_{status}.log')
            self.records.append(record)
            (Path(self.monitor.sessions_dir) / record['file']).write_text(f'session {sid} <script>alert(1)</script>\n')
        Path(self.monitor.index_path).write_text(''.join(json.dumps(r)+'\n' for r in self.records))
        self.client = create_app(self.monitor, self.cfg).test_client()

    def export(self, **data):
        response = self.client.post('/export', data=data)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.data)

    def test_export_filter_and_inclusive_bounds(self):
        self.assertEqual([r['id'] for r in self.export()['sessions']], [1, 2, 3, 4, 5])
        self.assertEqual([r['id'] for r in self.export(status='ANOMALY')['sessions']], [2, 4])
        self.assertEqual([r['id'] for r in self.export(status='ANOMALY', from_id='3', to_id='4')['sessions']], [4])
        self.assertEqual([r['id'] for r in self.export(from_id='2', to_id='2')['sessions']], [2])
        self.assertEqual([r['id'] for r in self.export(to_id='2')['sessions']], [1, 2])
        self.assertEqual([r['id'] for r in self.export(from_id='4')['sessions']], [4, 5])
        self.assertEqual(self.export(status='ANOMALY', from_id='5')['sessions'], [])

    def test_bad_export_input(self):
        for data in [dict(from_id='0'), dict(to_id='-1'), dict(from_id='5', to_id='2'), dict(from_id='1.5'), dict(status='BAD')]:
            with self.subTest(data=data):
                self.assertEqual(self.client.post('/export', data=data).status_code, 400)

    def test_pruned_and_text_export(self):
        (Path(self.monitor.sessions_dir) / self.records[1]['file']).unlink()
        self.assertIsNone(self.export(from_id='2', to_id='2')['sessions'][0]['log'])
        result = self.client.post('/export', data=dict(status='ANOMALY', format='txt', from_id='3'))
        self.assertIn(b'==== #4 ANOMALY', result.data)
        self.assertNotIn(b'==== #2', result.data)
        self.assertNotIn('log', self.export(include_logs='0')['sessions'][0])

    def test_labels_positions_validation_and_export(self):
        for ts in [0, 1015.00001, 1021, 1041, 1055]:
            response = self.client.post('/api/labels', json=dict(text=f'label {ts}', ts=ts))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['label']['ts'], ts)
        for ts in ['bad', True, -1, float('inf')]:
            self.assertEqual(self.client.post('/api/labels', json=dict(text='bad', ts=ts)).status_code, 400)
        chosen = self.export(status='ANOMALY')['labels']
        self.assertEqual([l['ts'] for l in chosen], [1021, 1041])
        self.assertEqual(self.export(status='NORMAL', from_id='3')['labels'], [])
        label = labels.load(self.cfg)[1]
        self.assertEqual(self.client.post(f'/api/labels/{label["id"]}', json=dict(text='changed')).status_code, 200)
        for status in ['', 'ANOMALY', 'NORMAL']:
            page = self.client.get('/sessions', query_string=dict(status=status)).text
            self.assertIn('changed', page)
            self.assertLess(page.index('changed'), page.index('data-session-id="1"') if status != 'ANOMALY' else len(page))
        self.assertEqual(self.client.post(f'/api/labels/{label["id"]}/delete').status_code, 200)

    def test_log_preview_is_bounded_but_export_is_complete(self):
        path = Path(self.monitor.sessions_dir) / self.records[0]['file']
        path.write_text('x' * 70000)
        result = self.client.get('/api/sessions/1/log').json
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['log']), 64 * 1024)
        self.assertEqual(len(self.export(from_id='1', to_id='1')['sessions'][0]['log']), 70000)

    def test_page_and_lazy_log(self):
        page = self.client.get('/sessions?status=ANOMALY').text
        self.assertNotIn('rowsel', page)
        self.assertNotIn('lbl-flag', page)
        self.assertNotIn('name="ids"', page)
        self.assertIn('name="status" value="ANOMALY"', page)
        self.assertEqual(page.count('class="session-row"'), 2)
        self.assertEqual(page.count('class="label-insert"'), 3)
        self.assertNotIn('alert(1)', page)
        self.assertIn('<script>', self.client.get('/api/sessions/2/log').json['log'])
        self.assertEqual(self.client.get('/api/sessions/999/log').status_code, 404)
        (Path(self.monitor.sessions_dir) / self.records[1]['file']).unlink()
        self.assertIsNone(self.client.get('/api/sessions/2/log').json['log'])


if __name__ == '__main__':
    unittest.main()
