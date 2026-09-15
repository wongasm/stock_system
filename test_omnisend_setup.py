import pytest
import requests
import omnisend_setup as setup


def test_read_only_connection(monkeypatch):
    monkeypatch.setenv('OMNISEND_API_KEY', 'private-test-key')
    class Response:
        status_code = 200
    def get(url, **kwargs):
        assert url == 'https://api.omnisend.com/api/contacts'
        assert kwargs['params'] == {'limit': 1}
        assert kwargs['headers']['Authorization'] == 'Omnisend-API-Key private-test-key'
        assert kwargs['allow_redirects'] is False
        return Response()
    monkeypatch.setattr(setup.requests, 'get', get)
    assert 'Connected' in setup.check_connection()
    Response.status_code = 403
    with pytest.raises(ValueError, match='Contacts read'):
        setup.check_connection()


def test_missing_key_and_sanitized_errors(monkeypatch):
    monkeypatch.delenv('OMNISEND_API_KEY', raising=False)
    with pytest.raises(ValueError, match='missing'):
        setup.check_connection()
    monkeypatch.setenv('OMNISEND_API_KEY', 'private-test-key')
    def fail(*args, **kwargs):
        raise requests.ConnectionError('private-test-key')
    monkeypatch.setattr(setup.requests, 'get', fail)
    with pytest.raises(ValueError) as error:
        setup.check_connection()
    assert 'private-test-key' not in str(error.value)


def test_profile_readiness_does_not_treat_email_as_consent():
    result = setup.summarize_profiles([
        {'customer_id':'a','balance':31}, {'customer_id':'a','balance':31},
        {'customer_id':'b','balance':5}, {'customer_id':'c'}, {}, {'customer_id':'missing'}],
        {'a':{'email':'Test@example.com'},'b':{'email':'test@example.com'},'c':{'email':''}})
    assert result == {'loyalty_accounts':6,'duplicate_customer_links':1,
        'profiles_with_email':2,'profiles_with_points':2,'missing_or_invalid_email':1,
        'missing_customer_id':1,'missing_directory_profile':1,'shared_email_addresses':1}
