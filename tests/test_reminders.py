import asyncio
import json
import smtplib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from autocanvas.attendance import parse_code
from autocanvas.bootstrap import build
from autocanvas.config import Settings
from autocanvas.http import create_app
from autocanvas.reminders import ReminderConfig, Reminders, send_email
from autocanvas.storage import Store
from autocanvas.types import MediaSource


def code(token='FIRST', history='event'):
    return parse_code('https://mlearning.sjtu.edu.cn/lms/mobile2/forscan/'
                      f'?courseCode=10001&rollCallToken={token}&signHistoryId={history}', '10001')


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state.sqlite3')
        self.store.put('courses', '10001', {'id': '10001', 'active': True})
        self.store.put('courses', '20002', {'id': '20002', 'active': True})
        self.sent, self.discovered = [], []
        async def discover(course):
            self.discovered.append(course)
        def sender(*args):
            self.sent.append(args)
        self.reminders = Reminders(self.store, self.root, discover, sender=sender)
        self.config = self.reminders.configuration
        self.rules = {'enabled': True, 'poll_seconds': 60, 'courses': {
            '10001': {'name': '算法设计', 'enabled': True, 'recipients': ['a@example.com', 'b@example.com']},
            '20002': {'name': '另一门课', 'enabled': False, 'recipients': []}}}
        self.smtp = {'enabled': True, 'host': 'smtp.163.com', 'port': 465,
                     'security': 'ssl', 'user': 'sender@example.com', 'password': 'SECRET'}
        for name, value in [('courses', self.rules), ('smtp', self.smtp)]:
            self.save(name, value)

    def save(self, section, value):
        self.config.save(section, value, self.config.read(section)[1])

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_detection_only_observer_never_prepares_or_submits_even_when_paused(self):
        from autocanvas.live_attendance import LiveAttendance
        self.store.put('control', 'automation', {'paused': True})
        client = Mock()
        detected = asyncio.Event()
        class Detector:
            def detect_bytes(self, data):
                return [{'content': 'https://mlearning.sjtu.edu.cn/lms/mobile2/forscan/'
                         '?courseCode=10001&rollCallToken=FIRST&signHistoryId=event'}]
        async def reader(source):
            yield b'image'
            detected.set()
            await asyncio.Event().wait()
        observer = LiveAttendance(self.store, self.root, None, client, reader=reader,
                                  detector_factory=Detector, reminders=self.reminders)
        self.assertTrue(observer.watching('10001'))
        self.assertFalse(observer.watching('20002'))
        self.assertFalse(observer.enabled('10001'))
        observing = asyncio.create_task(observer.observe({'id': 'lecture', 'course_id': '10001'}, MediaSource('fixture'), 0))
        worker = asyncio.create_task(observer.worker.run())
        try:
            await detected.wait()
            for _ in range(100):
                if self.store.list('attendance_detections'):
                    break
                await asyncio.sleep(.01)
            self.assertEqual(len(self.store.list('attendance_detections')), 1)
            await self.reminders.deliver()
            self.assertEqual(len(self.sent), 2)
            client.prepare.assert_not_called()
            client.submit.assert_not_called()
            self.assertFalse(self.store.list('attendance'))
        finally:
            observing.cancel(); worker.cancel()
            await asyncio.gather(observing, worker, return_exceptions=True)

    async def test_rotating_codes_multiview_and_restart_do_not_repeat_mail(self):
        lecture = {'id': 'lecture'}
        self.reminders.record(code(), lecture, 'outputs/evidence.jpg')
        self.reminders.record(code('SECOND'), lecture, 'outputs/evidence2.jpg')
        await self.reminders.deliver()
        restart = Reminders(self.store, self.root, None, sender=self.reminders.sender)
        restart.record(code('THIRD'), lecture, '')
        await restart.deliver()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.store.list('attendance_detections')), 1)
        self.assertTrue(all(r['status'] == 'sent' for r in self.store.list('reminder_deliveries')))
        data = json.dumps(self.reminders.status())
        for secret in ['FIRST', 'SECOND', 'THIRD', 'SECRET', 'rollCallToken']:
            self.assertNotIn(secret, data)
        self.reminders.record(code(history='another'), lecture, '')
        await self.reminders.deliver()
        self.assertEqual(len(self.sent), 4)

    async def test_disable_or_remove_recipient_cancels_already_queued_mail(self):
        self.reminders.record(code(), {'id': 'lecture'}, '')
        self.rules['courses']['10001']['recipients'] = ['a@example.com']
        self.save('courses', self.rules)
        await self.reminders.deliver()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual({r['status'] for r in self.store.list('reminder_deliveries')}, {'sent', 'cancelled'})
        self.reminders.record(code(history='next'), {'id': 'lecture'}, '')
        self.rules['enabled'] = False
        self.save('courses', self.rules)
        await self.reminders.deliver()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.reminders.enabled('10001'))

    async def test_smtp_disabled_holds_mail_but_old_mail_expires(self):
        self.reminders.record(code(), {'id': 'lecture'}, '')
        self.smtp['enabled'] = False
        self.save('smtp', self.smtp)
        await self.reminders.deliver()
        self.assertFalse(self.sent)
        for row in self.store.list('reminder_deliveries'):
            row['at'] -= 601
            self.store.put('reminder_deliveries', row['id'], row)
        await self.reminders.deliver()
        self.assertEqual({r['status'] for r in self.store.list('reminder_deliveries')}, {'expired'})

    async def test_unknown_result_not_retried_and_raw_smtp_error_not_exposed(self):
        self.reminders.record(code(), {'id': 'lecture'}, '')
        sender = Mock(side_effect=TimeoutError('SECRET server reply'))
        self.reminders.sender = sender
        await self.reminders.deliver()
        await self.reminders.deliver()
        self.assertEqual(sender.call_count, 2)
        self.assertEqual({r['status'] for r in self.store.list('reminder_deliveries')}, {'unknown'})
        self.assertNotIn('SECRET', json.dumps(self.reminders.status()))

    async def test_restart_marks_sending_unknown_without_resending(self):
        self.reminders.record(code(), {'id': 'lecture'}, '')
        for row in self.store.list('reminder_deliveries'):
            row['status'] = 'sending'
            self.store.put('reminder_deliveries', row['id'], row)
        task = asyncio.create_task(self.reminders.run())
        await asyncio.sleep(.03)
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.sent)
        self.assertEqual({r['status'] for r in self.store.list('reminder_deliveries')}, {'unknown'})

    async def test_discovery_only_queries_selected_active_courses(self):
        task = asyncio.create_task(self.reminders.poll())
        await asyncio.sleep(.02)
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.discovered, ['10001'])
        self.rules['courses'] = {}
        self.save('courses', self.rules)
        self.assertEqual(self.reminders.courses(), {})

    def test_config_hot_reload_conflict_validation_and_secret_preservation(self):
        snapshot = self.config.snapshot()
        self.assertNotIn('password', snapshot['smtp'])
        self.save('smtp', {**self.smtp, 'password': ''})
        self.assertEqual(self.config.read('smtp')[0]['password'], 'SECRET')
        self.rules['enabled'] = False
        self.save('courses', self.rules)
        self.assertFalse(self.reminders.enabled('10001'))
        with self.assertRaises(ValueError):
            self.config.save('courses', self.rules, snapshot['revision'])
        self.rules['courses']['10001']['recipients'] = ['bad\r\nBcc: x@example.com']
        with self.assertRaises(ValueError):
            self.save('courses', self.rules)
        (self.config.folder/'courses.yml').write_text('password: SECRET\n: [', encoding='utf-8')
        self.assertFalse(self.reminders.enabled('10001'))
        self.assertNotIn('SECRET', self.reminders.error)

    def test_test_email_is_explicit_and_rate_limited(self):
        self.reminders.test('10001')
        self.assertEqual({r['type'] for r in self.store.list('reminder_deliveries')}, {'test'})
        self.assertFalse(self.store.list('attendance_detections'))
        with self.assertRaises(ValueError):
            self.reminders.test('10001')
        with self.assertRaises(ValueError):
            self.reminders.test('20002')

    def test_smtp_certificate_verification_and_successful_acceptance(self):
        client = Mock()
        client.send_message.return_value = {}
        with patch('autocanvas.reminders.smtplib.SMTP_SSL', return_value=client) as smtp:
            send_email(self.smtp, 'a@example.com', '测试', 'body', '<id@localhost>')
        context = smtp.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        client.login.assert_called_once_with('sender@example.com', 'SECRET')
        client.close.assert_called_once()


class ReminderApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service, self.inference = build(Settings(root=self.root))
        self.client = TestClient(TestServer(create_app(self.service)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()
        await self.inference.close()
        self.tmp.cleanup()

    async def test_api_saves_private_yaml_without_echoing_password(self):
        response = await self.client.get('/api/reminders')
        self.assertEqual(response.status, 200)
        data = await response.json()
        smtp = {**data['smtp'], 'enabled': True, 'user': 'sender@example.com', 'password': 'SECRET'}
        response = await self.client.patch('/api/reminders', json={'section': 'smtp', 'values': smtp, 'revision': data['smtp_revision']})
        self.assertEqual(response.status, 200)
        saved = await response.json()
        self.assertNotIn('SECRET', json.dumps(saved))
        self.assertTrue(saved['has_password'])
        self.assertIn('SECRET', (self.root/'notifications/smtp.yml').read_text(encoding='utf-8'))
        self.assertFalse(self.service.attendance.config()['enabled'])
        response = await self.client.get('/api/file', params={'path': 'notifications/smtp.yml'})
        self.assertEqual(response.status, 404)
        response = await self.client.patch('/api/reminders', json={'section': 'smtp', 'values': smtp, 'revision': data['smtp_revision']})
        self.assertEqual(response.status, 400)
        response = await self.client.post('/api/reminders/test', json={'course_id': '10001'})
        self.assertEqual(response.status, 400)
