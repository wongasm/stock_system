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
