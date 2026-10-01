from copy import deepcopy
import pytest
from omnisend_sync import prepare_profiles, sync_profiles, OmnisendClient


def test_profiles_skip_shared_emails_and_duplicate_accounts():
    accounts=[{'id':'loyalty','customer_id':'a','balance':31},
              {'customer_id':'b','balance':5}, {'customer_id':'c','balance':5},
              {'customer_id':'d','balance':4}, {'customer_id':'d','balance':4}]
    customers={'a':{'email':'a@example.com','given_name':'Amy'},
               'b':{'email':'shared@example.com'},'c':{'email':'SHARED@example.com'},
               'd':{'email':'d@example.com'}}
    profiles,counts=prepare_profiles(accounts,customers)
    assert len(profiles)==1 and profiles[0]['firstName']=='Amy'
    assert profiles[0]['customProperties']['bc_loyalty_points']==31
    assert 'last_visit' not in profiles[0]['customProperties']
    assert counts['skipped_shared_email']==2 and counts['skipped_customer_link']==2


class Client:
    def __init__(self, existing=None):
        self.existing=existing
        self.writes=[]
    def lookup(self,email):return self.existing
    def request(self,*args,**kwargs):
        self.writes.append((args,kwargs))
        return 201,{}


@pytest.fixture
def profile():
    return {'email':'a@example.com','firstName':'Amy',
        'customProperties':{'bc_square_customer_id':'customer-a','bc_loyalty_points':31}}


def test_preview_never_writes(profile):
    client=Client()
    assert sync_profiles([profile],client)=={'would_create':1}
    assert client.writes==[]


@pytest.mark.parametrize('status',['unsubscribed','nonSubscribed',None])
def test_existing_suppression_preserved(profile,status):
    client=Client({'id':'one','_email_status':status})
    result=sync_profiles([profile],client,apply=True)
    assert not client.writes and sum(result.values())==1


def test_existing_update_cannot_change_channels(profile):
    client=Client({'id':'one','_email_status':'subscribed'})
    assert sync_profiles([profile],client,apply=True)=={'updated':1}
    args,kwargs=client.writes[0]
    assert args==('PATCH','/one')
    assert 'identifiers' not in kwargs['json'] and 'tags' not in kwargs['json']


def test_new_subscription_has_no_invented_historical_consent(profile):
    client=Client()
    assert sync_profiles([profile],client,apply=True)=={'created':1}
    args,kwargs=client.writes[0]
    identifier=kwargs['json']['identifiers'][0]
    assert args==('POST',)
    assert identifier['channels']['email']['status']=='subscribed'
    assert 'consent' not in identifier
    assert 'phone' not in kwargs['json']


def test_mismatched_identity_is_not_overwritten(profile):
    client=Client({'id':'one','_email_status':'subscribed',
                   'customProperties':{'bc_square_customer_id':'different'}})
    assert sync_profiles([profile],client,True)=={'skipped_identity_conflict':1}
    assert not client.writes


def test_lookup_response_is_verified(monkeypatch):
    client=OmnisendClient('test')
    monkeypatch.setattr(client,'request',lambda *a,**k:(200,{'contacts':[{'id':'one',
        'identifiers':[{'type':'email','id':'a@example.com','channels':{'email':{'status':'unsubscribed'}}}]}]}))
    assert client.lookup('a@example.com')['_email_status']=='unsubscribed'
    with pytest.raises(ValueError,match='identity'):
        client.lookup('different@example.com')
    monkeypatch.setattr(client,'request',lambda *a,**k:(200,{}))
    with pytest.raises(ValueError,match='Unexpected'):
        client.lookup('a@example.com')


@pytest.fixture
def linked(profile):
    profile['customProperties']['bc_loyalty_account_id']='loyalty-a'
    properties=deepcopy(profile['customProperties'])
    properties['bc_loyalty_points']=25
    return {'id':'one','_email_status':'subscribed','customProperties':properties}


def test_scheduled_only_patches_points_and_is_idempotent(profile,linked):
    client=Client(linked)
    assert sync_profiles([profile],client,True,scheduled=True)=={'updated':1}
    assert client.writes==[(('PATCH','/one'),{'json':{'customProperties':{'bc_loyalty_points':31}}})]
    linked['customProperties']['bc_loyalty_points']=31
    client.writes.clear()
    assert sync_profiles([profile],client,True,scheduled=True)=={'unchanged':1}
    assert not client.writes


@pytest.mark.parametrize('case',['missing','unlinked','wrong_account','unsubscribed'])
def test_scheduled_never_imports_or_relinks(profile,linked,case):
    if case=='missing':
        linked=None
    elif case=='unlinked':
        del linked['customProperties']['bc_square_customer_id']
    elif case=='wrong_account':
        linked['customProperties']['bc_loyalty_account_id']='another'
    else:
        linked['_email_status']='unsubscribed'
    client=Client(linked)
    result=sync_profiles([profile],client,True,scheduled=True)
    assert sum(result.values())==1
    assert not client.writes


def test_scheduled_preview_does_not_write(profile,linked):
    client=Client(linked)
    assert sync_profiles([profile],client,scheduled=True)=={'would_update':1}
    assert not client.writes


def test_lock_blocks_overlap_and_releases_after_failure(tmp_path):
    from omnisend_sync import sync_lock
    path=tmp_path/'sync.lock'
    with pytest.raises(RuntimeError):
        with sync_lock(path) as acquired:
            assert acquired
            with sync_lock(path) as second:
                assert not second
            raise RuntimeError('interrupted')
    with sync_lock(path) as acquired:
        assert acquired


def test_square_failure_prevents_stale_upload(monkeypatch):
    import sys
    import types
    from contextlib import nullcontext
    from omnisend_sync import load_fresh_profiles
    fake_app=types.SimpleNamespace(app_context=lambda:nullcontext())
    monkeypatch.setitem(sys.modules,'app',types.SimpleNamespace(app=fake_app,cache_set=lambda *a:pytest.fail('must not cache failure')))
    monkeypatch.setitem(sys.modules,'square_api',types.SimpleNamespace(
        fetch_loyalty_accounts=lambda:{'ok':False},
        fetch_customer_directory=lambda:{'ok':True,'customers':{}}))
    with pytest.raises(ValueError,match='Square refresh failed'):
        load_fresh_profiles()
