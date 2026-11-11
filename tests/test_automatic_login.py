from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from autocanvas import _jaccount
from autocanvas.auth import Auth
from autocanvas.automatic_login import AutomaticLogin, LoginRejected, fresh_login, solve_captcha
from autocanvas.browser_auth import BrowserAuth
from autocanvas.credentials import CredentialStore, CredentialStoreError
from autocanvas.types import AuthenticationRequired, RemoteError


class Response:
    def __init__(self, url, body=None, status=200, text='', content=b'captcha'):
        self.url, self.body, self.status_code = url, body, status
        self.text, self.content = text, content
        self.headers = {'Content-Type': 'image/jpeg'}

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('private url must not escape')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class LoginSession(requests.Session):
    """Runs real parsing, submission and callback code against a fake school."""
    def __init__(self, results=None, captcha=True):
        super().__init__()
        self.results = iter(results or [{'errno': 0, 'url': 'https://oc.sjtu.edu.cn/callback'}])
        self.captcha = captcha
        self.authenticated = False
        self.closed = False
        self.submissions = []
        self.pages = 0

    def get(self, url, **kwargs):
        if url.endswith('/callback'):
            self.authenticated = True
            self.cookies.set('canvas', 'test-cookie', domain='oc.sjtu.edu.cn')
            return Response(url)
        if 'dashboard_cards' in url:
            return Response(url, [] if self.authenticated else {}, status=200 if self.authenticated else 401)
        if '/captcha' in url:
            return Response(url)
        self.pages += 1
        html = 'const loginContext = {sid: "sid", uuid: "uuid-%d"};' % self.pages
        if self.captcha:
            html += '<img id="captcha-img">'
        return Response('https://jaccount.sjtu.edu.cn/jaccount/jalogin', text=html)

    def post(self, url, **kwargs):
        self.submissions.append(kwargs['data'])
        return Response(url, next(self.results))

    def close(self):
        self.closed = True
        super().close()


class FreshLoginTests(unittest.TestCase):
    def setUp(self):
        self.sleep = self.enterContext(patch('autocanvas.automatic_login.time.sleep'))

    def test_fast_solver_waits_before_submitting_new_captcha(self):
        session = LoginSession()
        def settle(seconds):
            self.assertFalse(session.submissions)
            self.assertEqual(seconds, 1.75)
        self.sleep.side_effect = settle
        with patch('autocanvas._jaccount.create_session', return_value=session), patch('autocanvas.automatic_login.time.monotonic', side_effect=[100, 100.25]):
            fresh_login('user', 'password', lambda image: 'abcd')
        self.sleep.assert_called_once_with(1.75)
        self.assertEqual(len(session.submissions), 1)
        session.close()

    def test_slow_solver_does_not_add_another_wait(self):
        session = LoginSession()
        with patch('autocanvas._jaccount.create_session', return_value=session), patch('autocanvas.automatic_login.time.monotonic', side_effect=[100, 103]):
            fresh_login('user', 'password', lambda image: 'abcd')
        self.sleep.assert_not_called()
        session.close()

    def test_complete_oidc_callback_and_canvas_validation(self):
        session = LoginSession()
        solver = Mock(return_value='abcde')
        with patch('autocanvas._jaccount.create_session', return_value=session):
            self.assertIs(fresh_login('user', 'password', solver), session)
        self.assertTrue(session.authenticated)
        self.assertFalse(session.closed)
        self.assertEqual(session.submissions[0]['captcha'], 'abcde')
        self.assertEqual(session.submissions[0]['pass'], 'password')
        self.assertEqual(session.adapters['https://'].max_retries.total, 0)
        solver.assert_called_once_with(b'captcha')
        session.close()

    def test_captcha_error_refreshes_form_then_succeeds(self):
        session = LoginSession([{'errno': 1, 'error': '验证码错误'}, {'errno': 0, 'url': 'https://oc.sjtu.edu.cn/callback'}])
        # Distinct images and answers must stay paired across a rejected captcha.
        original_get = session.get
        def get(url, **kwargs):
            response = original_get(url, **kwargs)
            if '/captcha' in url:
                response.content = ('image-' + kwargs['params']['uuid']).encode()
            return response
        solver = Mock(side_effect=['wrong', 'right'])
        pauses = []
        self.sleep.side_effect = lambda seconds: pauses.append((seconds, len(session.submissions), session.pages))
        with patch('autocanvas._jaccount.create_session', return_value=session), patch.object(session, 'get', side_effect=get):
            fresh_login('user', 'password', solver)
        self.assertEqual(len(session.submissions), 2)
        self.assertNotEqual(session.submissions[0]['uuid'], session.submissions[1]['uuid'])
        self.assertEqual([call.args[0] for call in solver.call_args_list], [b'image-uuid-2', b'image-uuid-4'])
        self.assertEqual([item['captcha'] for item in session.submissions], ['wrong', 'right'])
        self.assertIn((2.0, 1, 2), pauses)  # Back off before fetching the next form.
        session.close()

    def test_password_error_is_not_retried_even_with_errno_one(self):
        session = LoginSession([{'errno': 1, 'error': '用户名或密码错误'}])
        with patch('autocanvas._jaccount.create_session', return_value=session):
            with self.assertRaises(LoginRejected) as caught:
                fresh_login('user', 'password', lambda image: 'abcd')
        self.assertEqual(caught.exception.reason, 'credentials_rejected')
        self.assertEqual(len(session.submissions), 1)
        self.assertTrue(session.closed)

    def test_captcha_retries_are_bounded(self):
        session = LoginSession([{'errno': 1, 'error': 'captcha error'}] * 3)
        with patch('autocanvas._jaccount.create_session', return_value=session):
            with self.assertRaises(RemoteError):
                fresh_login('user', 'password', lambda image: 'abcd')
        self.assertEqual(len(session.submissions), 3)
        self.assertTrue(session.closed)

    def test_no_captcha_and_extra_verification(self):
        session = LoginSession(captcha=False)
        solver = Mock(side_effect=AssertionError('must not run'))
        with patch('autocanvas._jaccount.create_session', return_value=session):
            fresh_login('user', 'password', solver)
        self.assertEqual(session.submissions[0]['captcha'], '')
        self.sleep.assert_not_called()
        session.close()
        session = LoginSession([{'errno': 2, 'error': '需要短信验证'}])
        with patch('autocanvas._jaccount.create_session', return_value=session):
            with self.assertRaises(LoginRejected) as caught:
                fresh_login('user', 'password', lambda image: 'abcd')
        self.assertEqual(caught.exception.reason, 'interactive_verification_required')

    def test_unexpected_callback_never_followed(self):
        session = LoginSession([{'errno': 0, 'url': 'https://foreign.example/callback'}])
        with patch('autocanvas._jaccount.create_session', return_value=session):
            with self.assertRaises(_jaccount.LoginProtocolError):
                fresh_login('user', 'password', lambda image: 'abcd')
        self.assertFalse(session.authenticated)
        self.assertTrue(session.closed)

    def test_solver_sends_only_image_and_validates_result(self):
        for result, valid in [('abcde', True), ('abcd', True), ('<html>', False), (None, False)]:
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.post.return_value = Response('solver', {'result': result})
            with patch('autocanvas.automatic_login.requests.Session', return_value=client):
                if valid:
                    self.assertEqual(solve_captcha(b'image'), result)
                else:
                    with self.assertRaises(RemoteError):
                        solve_captcha(b'image')
            kwargs = client.post.call_args.kwargs
            self.assertEqual(set(kwargs), {'files', 'timeout', 'allow_redirects'})
            self.assertEqual(kwargs['files']['image'][1], b'image')
            self.assertFalse(kwargs['allow_redirects'])


class StoredLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'auth'/'session.json'
        self.manager = AutomaticLogin(self.path)
        self.manager.credentials.set('user', 'secret-password')

    def tearDown(self):
        self.tmp.cleanup()

    def test_yaml_comments_and_special_characters_round_trip(self):
        store = self.manager.credentials
        store.path.write_text("# User-editable config\nenabled: true\nusername: '012345'\npassword: 'p:#\\word''quote'\n", encoding='utf-8')
        self.assertEqual(store.get(), {'username': '012345', 'password': "p:#\\word'quote"})
        self.assertNotIn('password', json.dumps(store.metadata()))
        store.set('012345', "p:#\\word'quote")
        self.assertEqual(store.get()['password'], "p:#\\word'quote")
        if os.name != 'nt':
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_yaml_does_not_expose_secret(self):
        for body in ("password: [SECRET", "username: 1234\npassword: secret", "!!python/object/apply:os.system ['bad']"):
            self.manager.credentials.path.write_text(body)
            with self.assertRaises(CredentialStoreError) as caught:
                self.manager.credentials.get()
            self.assertNotIn('SECRET', str(caught.exception))

    def test_success_persists_session_and_survives_restart(self):
        session = LoginSession()
        with patch('autocanvas._jaccount.create_session', return_value=session), patch('autocanvas.automatic_login.solve_captcha', return_value='abcd'):
            self.manager.login().close()
        restarted = AutomaticLogin(self.path)
        self.assertTrue(restarted.state()['last_success'])
        self.assertEqual(restarted.state()['failures'], 0)
        self.assertNotIn('secret-password', self.path.read_text())
        self.assertNotIn('secret-password', restarted.path.read_text())

    def test_transient_failure_backoff_survives_restart(self):
        self.path.write_text('old-session')
        with patch('autocanvas.automatic_login.time.time', return_value=1000), patch('autocanvas.automatic_login.fresh_login', side_effect=requests.Timeout('SECRET')) as login:
            for manager in (self.manager, AutomaticLogin(self.path)):
                with self.assertRaises(AuthenticationRequired):
                    manager.login()
            self.assertEqual(login.call_count, 1)
            self.assertEqual(self.manager.state()['next_attempt'], 1060)
        self.assertEqual(self.path.read_text(), 'old-session')
        self.assertNotIn('SECRET', self.manager.path.read_text())

    def test_captcha_failure_details_are_retained_without_response_or_credentials(self):
        failure = RemoteError('SECRET response body', stage='captcha_solver', code='attempts_exhausted')
        with patch('autocanvas.automatic_login.fresh_login', side_effect=failure):
            with self.assertRaises(AuthenticationRequired):
                self.manager.login()
        state = self.manager.state()
        self.assertFalse(state['blocked'])
        self.assertEqual(state['last_error_type'], 'RemoteError')
        self.assertEqual(state['last_error_stage'], 'captcha_solver')
        self.assertEqual(state['last_error_code'], 'attempts_exhausted')
        self.assertNotIn('SECRET', self.manager.path.read_text())
        self.assertNotIn('secret-password', self.manager.path.read_text())

    def test_bad_credentials_block_until_config_changes(self):
        with patch('autocanvas.automatic_login.fresh_login', side_effect=LoginRejected('credentials_rejected')) as login:
            for manager in (self.manager, AutomaticLogin(self.path)):
                with self.assertRaises(AuthenticationRequired):
                    manager.login()
            self.assertEqual(login.call_count, 1)
        self.assertTrue(self.manager.state()['blocked'])
        previous = self.manager.credentials.path.stat().st_mtime_ns
        self.manager.credentials.set('user', 'new-password')
        os.utime(self.manager.credentials.path, ns=(previous+1_000_000, previous+1_000_000))
        self.assertFalse(self.manager.state()['blocked'])
        self.assertEqual(self.manager.state()['next_attempt'], 0)

    def test_valid_cookie_never_submits_password(self):
        auth = Auth(self.path)
        with patch('autocanvas.auth._jaccount.ensure_session', return_value=Mock()), patch.object(auth.automatic, 'login') as login:
            auth.session().close()
            login.assert_not_called()

    def test_jaccount_refresh_precedes_password_login(self):
        auth = Auth(self.path)
        old, fresh = Mock(), Mock()
        with patch('autocanvas._jaccount.load_session', return_value=old), patch('autocanvas._jaccount.is_session_valid', return_value=False), patch('autocanvas._jaccount._refresh_with_jacookie', return_value=fresh), patch('autocanvas._jaccount.save_session') as save, patch.object(auth.automatic, 'login') as login:
            self.assertIs(auth.session(), fresh)
            login.assert_not_called()
            save.assert_called_once_with(fresh, self.path)
        old.close.assert_called_once()

    def test_configure_only_replaces_credentials_after_verified_login(self):
        original = self.manager.credentials.path.read_bytes()
        with patch('autocanvas.automatic_login.fresh_login', side_effect=LoginRejected('credentials_rejected')):
            with self.assertRaises(LoginRejected):
                self.manager.configure('bad-user', 'bad-password')
        self.assertEqual(self.manager.credentials.path.read_bytes(), original)
        with patch('autocanvas.automatic_login.fresh_login', return_value=requests.Session()):
            result = self.manager.configure('new-user', 'new-password')
        self.assertTrue(result['authenticated'])
        self.assertEqual(self.manager.credentials.get()['username'], 'new-user')
        self.assertNotIn('new-password', json.dumps(result))

    def test_portable_process_lock_blocks_second_owner(self):
        from autocanvas.bootstrap import service_lock
        root = Path(self.tmp.name)
        with service_lock(root):
            with self.assertRaises(RuntimeError):
                with service_lock(root):
                    self.fail('second owner acquired lock')
        with service_lock(root):
            pass

    def test_network_failure_does_not_trigger_password_login(self):
        auth = Auth(self.path)
        with patch('autocanvas.auth._jaccount.ensure_session', side_effect=requests.Timeout('SECRET')), patch.object(auth.automatic, 'login') as login:
            with self.assertRaises(RemoteError) as caught:
                auth.session()
            login.assert_not_called()
            self.assertNotIn('SECRET', str(caught.exception))

    def test_concurrent_session_requests_perform_one_full_login(self):
        auth = Auth(self.path)
        def ensure(*args, **kwargs):
            if not self.path.exists():
                raise _jaccount.SessionExpired()
            return requests.Session()
        with patch('autocanvas.auth._jaccount.ensure_session', side_effect=ensure), patch('autocanvas.automatic_login.fresh_login', side_effect=lambda *args: requests.Session()) as login:
            with ThreadPoolExecutor(max_workers=6) as pool:
                sessions = list(pool.map(lambda _: auth.session(), range(6)))
            self.assertEqual(login.call_count, 1)
            for session in sessions:
                session.close()

    def test_logout_disables_automatic_login(self):
        browser = BrowserAuth(Auth(self.path))
        self.path.write_text('old-session')
        browser.logout()
        self.assertFalse(self.path.exists())
        self.assertFalse(AutomaticLogin(self.path).state()['enabled'])
        with patch('autocanvas.automatic_login.fresh_login') as login:
            with self.assertRaises(AuthenticationRequired):
                browser.auth.session()
            login.assert_not_called()

    def test_strict_session_check_distinguishes_network_from_expiry(self):
        session = Mock()
        session.get.side_effect = requests.Timeout('SECRET')
        with self.assertRaises(requests.Timeout):
            _jaccount.is_session_valid(session, strict=True)
        session.get.side_effect = None
        session.get.return_value = Response('https://oc.sjtu.edu.cn/api/v1/dashboard/dashboard_cards', {}, status=401)
        self.assertFalse(_jaccount.is_session_valid(session, strict=True))
        session.get.return_value = Response('https://oc.sjtu.edu.cn/api/v1/dashboard/dashboard_cards', ValueError(), status=200)
        with self.assertRaises(_jaccount.LoginProtocolError):
            _jaccount.is_session_valid(session, strict=True)
