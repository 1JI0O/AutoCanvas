import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from autocanvas.bootstrap import build
from autocanvas.config import Settings


class MobileClient:
    def __init__(self):
        self.sent = []

    async def prepare(self):
        pass

    async def submit(self, code):
        self.sent.append(code)
        return {'status': 'succeeded'}


class IndependentControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service, self.inference = build(Settings(root=self.root, auto_asr=False, auto_slides=False, auto_live=False))
        self.store = self.service.store
        self.store.put('courses', '98432', {'id': '98432', 'active': True})
        self.store.put('courses', '93921', {'id': '93921', 'active': True})
        self.store.put('control', 'automation', {'paused': True})
        self.service.attendance.configure({'enabled': True, 'course_ids': ['98432']})
        self.mobile = MobileClient()
        self.service.attendance.worker.client = self.mobile
        self.service.attendance.worker.sign_delay = 0

    async def asyncTearDown(self):
        await self.service.close()
        await self.inference.close()
        self.tmp.cleanup()

    async def test_signing_while_global_paused_is_limited_to_explicit_active_courses(self):
        worker = self.service.attendance.worker
        for course in ['98432', '93921']:
            url = ('https://mlearning.sjtu.edu.cn/lms/mobile2/forscan/'
                   f'?courseCode={course}&rollCallToken=NEW&signHistoryId=event')
            worker.offer(course, [{'content': url}], course, 'lesson', time.monotonic())
        await worker.dispatch()
        self.assertEqual([row['course_id'] for row in self.mobile.sent], ['98432'])
        self.assertTrue(self.store.get('control', 'automation')['paused'])
        self.assertEqual(self.store.executions(), [])
        self.assertEqual(self.service.attendance.status()['enabled_course_ids'], ['98432'])
        self.store.put('courses', '98432', {'id': '98432', 'active': False})
        self.assertFalse(self.service.attendance.enabled('98432'))

    def test_empty_signing_scope_never_means_all_courses(self):
        self.service.attendance.configure({'enabled': True, 'course_ids': []})
        self.assertFalse(self.service.attendance.enabled('98432'))
        self.assertEqual(self.service.attendance.courses(), set())
        self.service.attendance.configure({'enabled': False, 'course_ids': ['98432']})
        self.assertFalse(self.service.attendance.enabled('98432'))

    def test_switch_only_update_preserves_latest_course_scope(self):
        self.service.attendance.configure({'enabled': True, 'course_ids': ['93921']})
        self.service.attendance.configure({'enabled': False})
        self.assertEqual(self.service.attendance.config(), {'enabled': False, 'course_ids': ['93921']})
        self.service.attendance.configure({'enabled': True})
        self.assertTrue(self.service.attendance.enabled('93921'))
        self.assertFalse(self.service.attendance.enabled('98432'))

    async def test_switch_api_changes_only_requested_controls(self):
        from aiohttp.test_utils import TestClient, TestServer
        from autocanvas.http import create_app
        client = TestClient(TestServer(create_app(self.service)))
        await client.start_server()
        try:
            response = await client.patch('/api/attendance', json={'enabled': False})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['config']['course_ids'], ['98432'])
            response = await client.patch('/api/attendance', json={'enabled': True})
            self.assertEqual((await response.json())['enabled_course_ids'], ['98432'])
            config = await (await client.get('/api/settings')).json()
            self.assertFalse(config['current']['download_assignment_attachments'])
            self.assertFalse(config['current']['auto_assignments'])
            for enabled in (True, False):
                response = await client.patch('/api/settings', json={
                    'values': {'download_assignment_attachments': enabled}, 'revision': config['revision']})
                self.assertEqual(response.status, 200)
                config = await response.json()
                self.assertEqual(config['current']['download_assignment_attachments'], enabled)
                self.assertEqual(config['restart_required'], [])
            self.assertEqual(await (await client.get('/api/automation')).json(), {'paused': True})
            for enabled in (True, False):
                response = await client.patch('/api/settings', json={
                    'values': {'auto_assignments': enabled}, 'revision': config['revision']})
                self.assertEqual(response.status, 200)
                config = await response.json()
                self.assertEqual(config['current']['auto_assignments'], enabled)
                self.assertFalse(config['current']['download_assignment_attachments'])
                self.assertEqual(config['restart_required'], [])
            self.assertFalse(Settings.load(root=self.root).auto_assignments)
            self.assertTrue(self.store.get('control', 'automation')['paused'])
            self.assertTrue(self.service.attendance.enabled('98432'))
            self.assertFalse(self.store.executions())
        finally:
            await client.close()

    async def test_live_discovery_without_reminders_or_global_scheduler(self):
        self.service.catalog.live = Mock(return_value=[])
        self.service.catalog.videos = Mock(side_effect=AssertionError('Replay must not synchronize'))
        self.service.assignments.run = Mock(side_effect=AssertionError('Assignments must not synchronize'))
        now = datetime.now(timezone.utc)
        self.store.put('lectures', '98432:live:lesson', {
            'course_id': '98432', 'id': 'lesson', 'kind': 'live',
            'begin': (now-timedelta(minutes=1)).isoformat(), 'end': (now+timedelta(minutes=1)).isoformat(),
        })
        watching = asyncio.Event()
        async def lecture(row):
            watching.set()
            await asyncio.Event().wait()
        self.service.attendance.lecture = lecture
        await self.service.start(automation=False)
        await asyncio.wait_for(watching.wait(), 2)
        for _ in range(100):
            if self.service.reminders.discovery:
                break
            await asyncio.sleep(.01)
        self.service.catalog.live.assert_called_once_with('98432')
        self.service.catalog.videos.assert_not_called()
        self.service.assignments.run.assert_not_called()
        self.assertEqual(self.store.executions(), [])
        self.assertFalse(any(t.get_name() == 'automation' for t in self.service.background))
        self.assertEqual(self.service.reminders.discovery['98432']['purposes'], ['auto_sign'])

    async def test_reminder_and_signing_discovery_share_one_query_per_course(self):
        config = self.service.reminders.configuration
        config.save('courses', {'enabled': True, 'poll_seconds': 60, 'courses': {
            '98432': {'name': '线代', 'enabled': True, 'recipients': ['one@example.com']},
            '93921': {'name': '算法', 'enabled': True, 'recipients': ['one@example.com']},
        }}, config.read('courses')[1])
        self.service.catalog.live = Mock(return_value=[])
        task = asyncio.create_task(self.service.reminders.poll())
        try:
            for _ in range(100):
                if len(self.service.reminders.discovery) == 2:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(self.service.catalog.live.call_count, 2)
            self.assertEqual(self.service.reminders.discovery['98432']['purposes'], ['auto_sign', 'reminder'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_assignment_download_switch_applies_to_existing_flow_without_restart(self):
        class Response:
            status_code = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, size): yield b'pdf-content'
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.return_value = Response()
        assignment = {'id': 1, 'name': 'Exercise', 'description': '<p>正文</p>', 'updated_at': 'today',
                      'attachments': [{'id': 1, 'display_name': 'a.pdf', 'url': 'https://example.test/a.pdf'}]}
        self.service.assignments.canvas = Mock()
        self.service.assignments.canvas.assignments.return_value = [assignment]
        self.service.assignments.session_factory = lambda: session
        self.service.assignments.run('98432')
        session.get.assert_not_called()
        result = self.store.list('assignments')[0]
        self.assertEqual(result['description'], '正文')
        self.assertEqual(result['attachments'][0]['status'], 'skipped')
        config = self.service.configuration
        saved = config.save({'download_assignment_attachments': True}, config.revision)
        self.assertEqual(saved['waiting_for_idle'], [])
        self.service.assignments.run('98432')
        session.get.assert_called_once()
        path = Path(self.store.list('assignments')[0]['attachments'][0]['path'])
        self.assertTrue(path.exists())
        config.save({'download_assignment_attachments': False}, config.revision)
        assignment['attachments'].append({'id': 2, 'display_name': 'b.pdf', 'url': 'https://example.test/b.pdf'})
        self.service.assignments.run('98432')
        session.get.assert_called_once()
        self.assertTrue(path.exists())
        self.assertEqual([r['status'] for r in self.store.list('assignments')[0]['attachments']], ['succeeded', 'skipped'])
        self.assertFalse(Settings.load(root=self.root).download_assignment_attachments)
        with self.assertRaises(ValueError):
            config.save({'download_assignment_attachments': 'false'}, config.revision)
