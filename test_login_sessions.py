"""Persistent staff login expiry, renewal and logout without live services."""
import re
from datetime import timedelta

from itsdangerous import TimestampSigner

from test_dashboard import web


def sign_in(web):
    client = web.app.test_client()
    page = client.get('/login')
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.text).group(1)
    response = client.post('/login', data={
        'username': 'admin', 'password': 'test-password', 'csrf_token': token,
    })
    assert response.status_code == 302
    return client


def test_login_survives_browser_restart_and_logout(web):
    client = sign_in(web)
    assert web.app.permanent_session_lifetime == timedelta(days=90)
    cookie = client.get_cookie('session')
    assert cookie.expires is not None
    assert cookie.http_only
    assert cookie.same_site == 'Lax'
    # A new browser process retains persistent cookies, but no in-memory state.
    reopened = web.app.test_client()
    reopened.set_cookie('session', cookie.value)
    assert reopened.get('/check_session').json == {'active': True}
    reopened.get('/logout')
    assert reopened.get('/check_session').json == {'active': False}


def test_activity_renews_session_but_inactivity_expires_it(web, monkeypatch):
    now = [1800000000]
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: now[0])
    client = sign_in(web)
    original_cookie = client.get_cookie('session').value
    now[0] += 89 * 86400
    assert client.get('/check_session').json == {'active': True}
    assert client.get_cookie('session').value != original_cookie
    now[0] += 2 * 86400
    assert client.get('/check_session').json == {'active': True}
    idle = web.app.test_client()
    idle.set_cookie('session', original_cookie)
    assert idle.get('/check_session').json == {'active': False}


def test_failed_login_does_not_create_persistent_authenticated_session(web):
    client = web.app.test_client()
    page = client.get('/login')
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.text).group(1)
    client.post('/login', data={
        'username': 'admin', 'password': 'wrong-password', 'csrf_token': token,
    })
    assert client.get('/check_session').json == {'active': False}
    assert client.get_cookie('session').expires is None
