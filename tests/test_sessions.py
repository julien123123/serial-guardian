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

    def test_export_modes_intersect_filter_and_ignore_other_modes(self):
        self.assertEqual([r['id'] for r in self.export(scope='all', status='ANOMALY', from_id='4')['sessions']], [2, 4])
        self.assertEqual([r['id'] for r in self.export(scope='selected', status='ANOMALY', ids=['1', '2', '4'], from_id='4')['sessions']], [2, 4])
        self.assertEqual([r['id'] for r in self.export(scope='range', status='ANOMALY', from_id='3', to_id='4', ids=['2'])['sessions']], [4])
        for data in [dict(scope='selected'), dict(scope='selected', ids='bad'), dict(scope='selected', ids='0'), dict(scope='range'), dict(scope='bad')]:
            with self.subTest(data=data):
                self.assertEqual(self.client.post('/export', data=data).status_code, 400)
        labels.add(self.cfg, 'inside', ts=1021)
        labels.add(self.cfg, 'outside', ts=1041)
        self.assertEqual([l['text'] for l in self.export(scope='selected', ids=['2'])['labels']], ['inside'])

    def test_live_label_uses_same_timeline_storage(self):
        page = self.client.get('/').text
        self.assertIn('id="liveLabelToggle"', page)
        self.assertIn('id="liveLabelForm"', page)
        result = self.client.post('/api/labels', json=dict(text='Battery changed live', ts=1035))
        self.assertEqual(result.status_code, 200)
        self.assertIn('Battery changed live', self.client.get('/sessions').text)
        self.assertIn('Battery changed live', self.client.get('/sessions?status=ANOMALY').text)

    def test_live_stream_interleaves_labels_and_tracks_changes(self):
        self.monitor._handle_line(None, 'before label\n')
        response = self.client.post('/api/labels', json=dict(text='<b>new label</b>'))
        lid = response.json['label']['id']
        self.monitor._handle_line(None, 'after label\n')
        stream = self.client.get('/api/tail?stream=1').json['stream']
        self.assertEqual([item['type'] for item in stream], ['log', 'label', 'log'])
        self.assertEqual([item['text'] for item in stream], ['before label', '<b>new label</b>', 'after label'])
        self.assertEqual(self.client.get('/api/tail').json['tail'], ['before label', 'after label'])
        self.client.post(f'/api/labels/{lid}', json=dict(text='edited label'))
        self.assertEqual(self.client.get('/api/tail?stream=1').json['stream'][1]['text'], 'edited label')
        self.client.post(f'/api/labels/{lid}/delete')
        self.assertEqual([item['type'] for item in self.client.get('/api/tail?stream=1').json['stream']], ['log', 'log'])

    def test_label_before_first_serial_line_stays_in_stream(self):
        self.client.post('/api/labels', json=dict(text='before connection'))
        self.assertEqual([item['text'] for item in self.client.get('/api/tail?stream=1').json['stream']], ['before connection'])
        self.monitor._handle_line(None, 'first output\n')
        self.assertEqual([item['text'] for item in self.client.get('/api/tail?stream=1').json['stream']], ['before connection', 'first output'])

    def test_live_stream_is_bounded_and_resizes_with_tail(self):
        self.monitor._handle_line(None, 'old\n')
        self.client.post('/api/labels', json=dict(text='mark'))
        self.monitor._handle_line(None, 'new\n')
        self.cfg.LIVE_TAIL_LINES = 2
        self.monitor.resize_live_tail()
        self.assertEqual([item['text'] for item in self.client.get('/api/tail?stream=1').json['stream']], ['mark', 'new'])
        self.monitor._handle_line(None, 'newer\n')
        self.assertEqual([item['text'] for item in self.client.get('/api/tail?stream=1').json['stream']], ['new', 'newer'])
        self.assertEqual(len(self.monitor.live_times), 2)

    def test_deployment_status_reports_port_and_boot_events(self):
        state = self.client.get('/api/status').json
        self.assertEqual(state['serial_port'], self.cfg.SERIAL_PORT)
        self.assertFalse(state['serial_open'])
        self.assertEqual(state['boot_count'], 0)
        self.monitor._handle_line(None, self.cfg.BOOT_BANNER_MARKER + '\n')
        self.assertEqual(self.client.get('/api/status').json['boot_count'], 1)

    def test_bad_export_input(self):
        for data in [dict(from_id='0'), dict(to_id='-1'), dict(from_id='5', to_id='2'), dict(from_id='1.5'), dict(status='BAD')]:
            with self.subTest(data=data):
                self.assertEqual(self.client.post('/export', data=data).status_code, 400)

    def test_raw_export_is_inline_text_and_uses_same_selection(self):
        args = dict(scope='selected', status='ANOMALY', ids=['1', '2', '4'], delivery='raw')
        raw = self.client.post('/export', data=args)
        self.assertEqual(raw.status_code, 200)
        self.assertTrue(raw.headers['Content-Disposition'].startswith('inline;'))
        self.assertEqual(raw.mimetype, 'text/plain')
        self.assertEqual(raw.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual([r['id'] for r in json.loads(raw.data)['sessions']], [2, 4])
        text = self.client.post('/export', data=dict(scope='range', from_id='2', to_id='2', format='txt', delivery='raw'))
        self.assertIn(b'==== #2 ANOMALY', text.data)
        download = self.client.post('/export', data=dict(delivery='download'))
        self.assertTrue(download.headers['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(download.mimetype, 'application/json')

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
