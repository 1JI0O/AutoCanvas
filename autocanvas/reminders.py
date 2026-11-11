"""Live QR observations and course-scoped email reminders; never signs attendance."""
import asyncio
import hashlib
import json
import os
import re
import smtplib
import ssl
import time
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import yaml

from .execution import blocking


def email_address(value):
    return (isinstance(value, str) and len(value) <= 254
            and re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}', value))


class ReminderConfig:
    def __init__(self, root):
        self.folder = root/'notifications'
        self.error = ''
        self.cached = {}

    def read(self, name):
        path = self.folder/(name+'.yml')
        default = ({'enabled': False, 'poll_seconds': 60, 'courses': {}} if name == 'courses' else
                   {'enabled': False, 'host': 'smtp.163.com', 'port': 465, 'security': 'ssl', 'user': '', 'password': ''})
        try:
            signature = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
            cached = self.cached.get(name)
            if cached and cached[0] == signature:
                return cached[1], cached[2]
            raw = path.read_text(encoding='utf-8') if signature else ''
            value = yaml.safe_load(raw) if raw else default
            self.validate(name, value)
            revision = hashlib.sha256(raw.encode()).hexdigest()
            self.cached[name] = (signature, value, revision)
            return value, revision
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            raise ValueError(f'{name}.yml 无法读取或格式不正确，请检查提醒配置文件') from None

    def validate(self, name, value):
        if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
            raise ValueError('enabled 必须是 true/false')
        if name == 'courses':
            if set(value) != {'enabled', 'poll_seconds', 'courses'}:
                raise ValueError('课程配置需要 enabled、poll_seconds、courses')
            if type(value['poll_seconds']) is not int or not 30 <= value['poll_seconds'] <= 3600:
                raise ValueError('直播列表轮询间隔必须为 30–3600 秒')
            if not isinstance(value['courses'], dict) or len(value['courses']) > 1000:
                raise ValueError('courses 必须按课程编号填写')
            for key, row in value['courses'].items():
                if not isinstance(key, str) or not key.isdigit() or not isinstance(row, dict):
                    raise ValueError('课程编号需要用引号包裹的数字字符串')
                if set(row) != {'name', 'enabled', 'recipients'} or type(row['enabled']) is not bool:
                    raise ValueError('每门课程需要 name、enabled、recipients')
                if not isinstance(row['name'], str) or len(row['name']) > 300 or any(c in row['name'] for c in '\r\n\x00'):
                    raise ValueError('课程名称不正确')
                recipients = row['recipients']
                if not isinstance(recipients, list) or len(recipients) > 100 or any(not email_address(r) for r in recipients):
                    raise ValueError('recipients 必须是邮箱列表')
                if len(set(r.casefold() for r in recipients)) != len(recipients):
                    raise ValueError('同一课程的邮箱不能重复')
                if row['enabled'] and not recipients:
                    raise ValueError('开启提醒的课程至少需要一个收件人')
        else:
            if set(value) != {'enabled', 'host', 'port', 'security', 'user', 'password'}:
                raise ValueError('SMTP 配置字段不正确')
            if value['security'] not in ('ssl', 'starttls'):
                raise ValueError('SMTP 需要 ssl 或 starttls 加密')
            if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
                raise ValueError('SMTP 端口不正确')
            if not isinstance(value['host'], str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', value['host']):
                raise ValueError('SMTP 主机不正确')
            if not isinstance(value['password'], str) or len(value['password']) > 2048 or any(c in value['password'] for c in '\r\n\x00'):
                raise ValueError('SMTP 授权码不正确')
            if value['user'] and not email_address(value['user']):
                raise ValueError('SMTP 用户名需要填写邮箱')
            if value['enabled'] and (not value['user'] or not value['password']):
                raise ValueError('开启 SMTP 需要用户名和授权码')

    def save(self, name, value, revision):
        current, actual = self.read(name)
        if revision != actual:
            raise ValueError('配置已在其他页面或文件中修改，请重新载入后保存')
        value = dict(value)
        if name == 'smtp' and value.get('password') == '':
            value['password'] = current['password']
        self.validate(name, value)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.folder/(name+'.yml')
        temp = self.folder/(name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
                handle.write(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def snapshot(self):
        courses, revision = self.read('courses')
        smtp, smtp_revision = self.read('smtp')
        return {'config': courses, 'revision': revision,
                'smtp': {k: v for k, v in smtp.items() if k != 'password'},
                'has_password': bool(smtp['password']), 'smtp_revision': smtp_revision,
                'paths': {'courses': str(self.folder/'courses.yml'), 'smtp': str(self.folder/'smtp.yml')}}


def send_email(config, recipient, subject, text, message_id):
    """Use the provider's SMTP submission service with certificate verification."""
    message = EmailMessage()
    message['From'], message['To'] = config['user'], recipient
    message['Subject'], message['Date'], message['Message-ID'] = subject, formatdate(localtime=True), message_id
    message.set_content(text)
    context = ssl.create_default_context()
    client = (smtplib.SMTP_SSL(config['host'], config['port'], timeout=20, context=context)
              if config['security'] == 'ssl' else smtplib.SMTP(config['host'], config['port'], timeout=20))
    try:
        if config['security'] == 'starttls':
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(config['user'], config['password'])
        refused = client.send_message(message, from_addr=config['user'], to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # A lost QUIT reply must not turn an already accepted message into a failure.
        client.close()


class Reminders:
    def __init__(self, store, root, discover, *, sender=send_email, attendance_courses=lambda: ()):
        self.store, self.root, self.discover, self.sender = store, root, discover, sender
        self.configuration = ReminderConfig(root)
        self.discovery = {}
        self.error = ''
        self.attendance_courses = attendance_courses

    def courses(self):
        try:
            config, _ = self.configuration.read('courses')
            self.error = ''
            return {key: row for key, row in config['courses'].items()
                    if config['enabled'] and row['enabled'] and self.store.get('courses', key, {}).get('active', False)}
        except ValueError as error:
            self.error = str(error)
            return {}

    def enabled(self, course):
        return course in self.courses()

    def status(self):
        snapshot = self.configuration.snapshot()
        return {**snapshot, 'error': self.error, 'discovery': list(self.discovery.values()),
                'detections': sorted(self.store.list('attendance_detections'), key=lambda r: r['at'], reverse=True)[:300],
                'deliveries': sorted(self.store.list('reminder_deliveries'), key=lambda r: r['at'], reverse=True)[:300]}

    def record(self, code, lecture, image):
        key = f"{code['course_id']}:{lecture['id']}:{code['history']}"
        if self.store.get('attendance_detections', key):
            return
        row = {'id': key, 'course_id': code['course_id'], 'lecture_id': lecture['id'],
               'history': code['history'], 'at': time.time(), 'image': image, 'type': 'live_qr'}
        course = self.courses().get(code['course_id'])
        deliveries = []
        for recipient in course['recipients'] if course else []:
            identity = hashlib.sha256((key+':'+recipient.casefold()).encode()).hexdigest()
            deliveries.append(('reminder_deliveries', identity, json.dumps(
                {**row, 'id': identity, 'recipient': recipient, 'course_name': course['name'],
                 'status': 'pending', 'message_id': make_msgid(domain='autocanvas.local')}, ensure_ascii=False)))
        # Commit observation and its mail outbox together: a crash cannot leave a
        # deduplicated observation whose recipients were never enqueued.
        with self.store.connect() as db:
            inserted = db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?)',
                                  ('attendance_detections', key, json.dumps(row, ensure_ascii=False)))
            if not inserted.rowcount:
                return
            db.executemany('INSERT INTO entities VALUES (?,?,?)', deliveries)
        folder = self.root/'outputs'/code['course_id']/lecture['id']/'live'/'attendance'
        folder.mkdir(parents=True, exist_ok=True)
        with (folder/'events.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False)+'\n')

    def test(self, course_id):
        course = self.courses().get(course_id)
        if not course:
            raise ValueError('请先保存并启用该课程提醒及收件人')
        smtp, _ = self.configuration.read('smtp')
        if not smtp['enabled']:
            raise ValueError('请先保存并开启 SMTP')
        if any(r.get('type') == 'test' and time.time()-r['at'] < 60 for r in self.store.list('reminder_deliveries')):
            raise ValueError('测试邮件每分钟最多发送一次')
        for recipient in course['recipients']:
            identity = uuid.uuid4().hex
            self.store.put('reminder_deliveries', identity,
                           {'id': identity, 'type': 'test', 'course_id': course_id, 'course_name': course['name'],
                            'lecture_id': '', 'at': time.time(), 'recipient': recipient, 'status': 'pending',
                            'message_id': make_msgid(domain='autocanvas.local')})

    async def deliver(self):
        for row in self.store.list('reminder_deliveries'):
            if row['status'] != 'pending':
                continue
            course = self.courses().get(row['course_id'])
            try:
                smtp, _ = self.configuration.read('smtp')
            except ValueError:
                continue
            if not course or row['recipient'] not in course['recipients']:
                row.update(status='cancelled', detail='课程提醒或收件人已关闭')
            elif time.time()-row['at'] > 600:
                row.update(status='expired', detail='超过十分钟，停止发送过期提醒')
            elif not smtp['enabled']:
                continue
            else:
                row.update(status='sending', detail='正在提交到邮件服务')
                self.store.put('reminder_deliveries', row['id'], row)
                at = datetime.fromtimestamp(row['at']).astimezone().isoformat(timespec='seconds')
                subject = f"[签到提醒] {row['course_name']}"
                text = (f"在课程直播中识别到签到二维码。\n课程：{row['course_name']} ({row['course_id']})\n"
                        f"检测时间：{at}\n课次：{row['lecture_id']}\n"
                        "本邮件只表示发现了签到码，不代表签到成功。是否提交及结果请查看 AutoCanvas 自动签到记录。\n"
                        "可在 AutoCanvas 的“签到提醒”页面查看截图和记录。")
                if row.get('type') == 'test':
                    subject = f"[AutoCanvas 测试邮件] {row['course_name']}"
                    text = f"这是签到提醒配置的测试邮件。\n课程：{row['course_name']}\n时间：{at}\n本测试不会触发扫码签到，也不表示发现了签到事件。"
                try:
                    await blocking(self.sender, smtp, row['recipient'], subject, text, row['message_id'])
                    row.update(status='sent', detail='SMTP 服务已接受；最终投递请以收件箱为准')
                except asyncio.CancelledError:
                    row.update(status='unknown', detail='发送中断，邮件可能已被接受，停止自动重发')
                    self.store.put('reminder_deliveries', row['id'], row)
                    raise
                except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
                        smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
                    row.update(status='failed', detail=type(error).__name__)
                except Exception as error:
                    row.update(status='unknown', detail=type(error).__name__+'；结果不确定，停止自动重发')
            row['updated'] = time.time()
            self.store.put('reminder_deliveries', row['id'], row)

    async def poll(self):
        while True:
            reminder_courses = set(self.courses())
            signing_courses = set(self.attendance_courses())
            wanted = reminder_courses | signing_courses
            self.discovery = {key: row for key, row in self.discovery.items() if key in wanted}
            for course in sorted(wanted):
                purposes = (['auto_sign'] if course in signing_courses else []) + (['reminder'] if course in reminder_courses else [])
                try:
                    rows = await self.discover(course)
                    self.discovery[course] = {'course_id': course, 'at': time.time(), 'status': 'ok',
                                              'live_count': len(rows or []), 'purposes': purposes}
                except Exception as error:
                    self.discovery[course] = {'course_id': course, 'at': time.time(), 'status': type(error).__name__, 'purposes': purposes}
            try:
                config, _ = self.configuration.read('courses')
                delay = config['poll_seconds']
            except ValueError:
                delay = 60
            await asyncio.sleep(delay)

    async def run(self):
        for row in self.store.list('reminder_deliveries'):
            if row['status'] == 'sending':
                row.update(status='unknown', detail='上次服务中断，邮件可能已发送，停止自动重发')
                self.store.put('reminder_deliveries', row['id'], row)
        poll = asyncio.create_task(self.poll())
        try:
            while True:
                if poll.done():
                    await poll
                await self.deliver()
                await asyncio.sleep(1)
        finally:
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
