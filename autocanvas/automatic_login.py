"""Bounded jAccount sign-in using saved credentials and a dedicated captcha service."""
import json
import os
import re
import time
from urllib.parse import urlsplit

import requests
from . import _jaccount
from .credentials import CredentialStore, CredentialStoreError
from .types import AuthenticationRequired, RemoteError

SOLVER_URL = 'https://geek.sjtu.edu.cn/captcha-solver/'
CAPTCHA_SUBMIT_MIN_SECONDS = 2.0
CAPTCHA_RETRY_SECONDS = 2.0


class LoginRejected(AuthenticationRequired):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def solve_captcha(image):
    # Deliberately use a separate session: never send school cookies or passwords.
    with requests.Session() as client:
        response = client.post(SOLVER_URL, files={'image': ('captcha.jpg', image, 'image/jpeg')},
                               timeout=(10, 20), allow_redirects=False)
        if response.status_code != 200:
            raise RemoteError('验证码识别服务暂不可用', stage='captcha_solver', code=response.status_code)
        try:
            text = response.json()['result']
        except (ValueError, KeyError, TypeError):
            raise RemoteError('验证码识别服务返回格式无效', stage='captcha_solver', code='invalid_response') from None
        if not isinstance(text, str) or not re.fullmatch(r'[A-Za-z0-9]{1,12}', text.strip()):
            raise RemoteError('验证码识别服务返回格式无效', stage='captcha_solver', code='invalid_result')
        return text.strip()


def fresh_login(username, password, solver=None):
    """Keep the previous session intact until a complete, verified login succeeds."""
    solver = solver or solve_captcha
    session = _jaccount.create_session()
    # A password submission must never be transparently retried by the transport.
    session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
    try:
        for attempt in range(3):
            url, html = _jaccount._get_jaccount_login_page(session)
            if urlsplit(url).hostname != 'jaccount.sjtu.edu.cn':
                if _jaccount.is_session_valid(session, strict=True):
                    return session
                raise RemoteError('登录页面结构已变化', stage='login_form', code='unexpected_redirect')
            params, uuid, has_captcha = _jaccount._parse_login_form(html, url)
            if not params.get('sid'):
                raise RemoteError('登录页面结构已变化', stage='login_form', code='missing_sid')
            captcha = ''
            if has_captcha:
                if not uuid:
                    raise RemoteError('无法获取验证码', stage='login_form', code='missing_uuid')
                image = session.get('https://jaccount.sjtu.edu.cn/jaccount/captcha',
                    params={'uuid': uuid, 't': int(time.time()*1000)}, headers={'Referer': url}, timeout=(10, 20))
                image.raise_for_status()
                if not image.headers.get('Content-Type', '').startswith('image/') or not 0 < len(image.content) <= 1024*1024:
                    raise RemoteError('验证码格式无效', stage='captcha_fetch', code='invalid_image')
                # A very fast solver can submit before jAccount accepts the new
                # challenge. Count solver time towards a short settling period.
                ready_at = time.monotonic() + CAPTCHA_SUBMIT_MIN_SECONDS
                captcha = solver(image.content)
                remaining = ready_at - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
            _, result = _jaccount._submit_login(session, params, uuid, username, password, captcha, url)
            if not isinstance(result, dict):
                raise RemoteError('登录响应格式已变化', stage='login_submit', code='invalid_response')
            if result.get('errno') == 0:
                if not _jaccount.is_session_valid(session, strict=True):
                    raise LoginRejected('callback_not_authenticated')
                return session
            # Do not infer captcha errors from errno=1: it may be a bad password.
            error = str(result.get('error', '')).lower()
            if has_captcha and ('验证码' in error or 'captcha' in error):
                if attempt < 2:
                    time.sleep(CAPTCHA_RETRY_SECONDS)
                # The next iteration fetches a new form/UUID and image, then
                # solves that image. Never resubmit the previous captcha.
                continue
            if any(word in error for word in ('密码', 'password', '用户名', '账号', '账户')):
                raise LoginRejected('credentials_rejected')
            raise LoginRejected('interactive_verification_required')
        raise RemoteError('验证码识别连续失败，稍后重试', stage='captcha_solver', code='attempts_exhausted')
    except BaseException:
        session.close()
        raise


class AutomaticLogin:
    def __init__(self, session_file, credentials=None):
        self.session_file = session_file
        self.path = session_file.with_name('automatic.json')
        self.credentials = credentials if credentials is not None else CredentialStore(session_file)

    def state(self):
        config = self.credentials.metadata()
        state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if state.get('config_revision') != config['config_revision']:
            state = {}
        return {'blocked': False, 'failures': 0, 'next_attempt': 0, **state, **config}

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(state, handle)
        temporary.replace(self.path)
        self.path.chmod(0o600)

    def configure(self, username, password):
        if not isinstance(username, str) or not username.strip() or len(username) > 256:
            raise ValueError('请输入有效的 jAccount 用户名')
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise ValueError('请输入有效密码')
        # Validate first. Incorrect credentials must not overwrite working ones.
        session = fresh_login(username.strip(), password)
        try:
            self.credentials.set(username.strip(), password)
            _jaccount.save_session(session, self.session_file)
            self._save({**self.credentials.metadata(), 'blocked': False, 'failures': 0, 'next_attempt': 0,
                        'last_success': time.time(), 'last_error': None, 'captcha_provider': 'sjtu_geek'})
        finally:
            session.close()
        return {'authenticated': True, 'automatic': self.state()}

    def login(self):
        state = self.state()
        if not state.get('enabled') or state.get('blocked'):
            raise AuthenticationRequired('自动登录尚未启用或需要更新凭据')
        if time.time() < state.get('next_attempt', 0):
            raise AuthenticationRequired('自动登录等待重试')
        try:
            credentials = self.credentials.get()
            if not credentials:
                raise LoginRejected('credentials_missing')
            session = fresh_login(credentials['username'], credentials['password'])
            try:
                _jaccount.save_session(session, self.session_file)
                self._save({**state, 'blocked': False, 'failures': 0, 'next_attempt': 0,
                            'last_success': time.time(), 'last_error': None,
                            'last_error_type': None, 'last_error_stage': None, 'last_error_code': None})
            except BaseException:
                session.close()
                raise
            return session
        except (LoginRejected, RemoteError, requests.RequestException, CredentialStoreError, _jaccount.LoginProtocolError) as error:
            failures = state.get('failures', 0) + 1
            blocked = isinstance(error, LoginRejected)
            code = error.reason if blocked else 'credentials_unavailable' if isinstance(error, CredentialStoreError) else 'temporary_login_failure'
            self._save({**state, 'blocked': blocked, 'failures': failures, 'last_error': code,
                        'last_error_type': type(error).__name__,
                        'last_error_stage': error.stage if isinstance(error, RemoteError) else None,
                        'last_error_code': error.code if isinstance(error, RemoteError) else None,
                        'last_failure': time.time(), 'next_attempt': time.time() + (60, 300, 900, 3600)[min(failures-1, 3)]})
            # Park work until the auth maintenance loop succeeds; do not exhaust
            # each execution's short media retry budget during a login outage.
            raise AuthenticationRequired('自动登录未完成，请查看自动登录状态') from None

    def disable(self, *, forget=False):
        if forget:
            self.credentials.delete()
        else:
            self.credentials.disable()
        return {'automatic': self.state()}
