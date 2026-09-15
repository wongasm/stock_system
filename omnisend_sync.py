"""Sync loyalty profiles to Omnisend. Preview by default; no marketing events."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote
import requests


def prepare_profiles(accounts, customers):
    counts = Counter()
    customer_counts = Counter(a.get('customer_id') for a in accounts)
    # Shared directory emails are ambiguous even if only one owner joined loyalty.
    email_counts = Counter((c.get('email') or '').strip().lower() for c in customers.values())
    profiles = []
    for account in accounts:
        cid = account.get('customer_id')
        customer = customers.get(cid, {})
        email = (customer.get('email') or '').strip().lower()
        if not cid or customer_counts[cid] != 1:
            counts['skipped_customer_link'] += 1
        elif not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            counts['skipped_email'] += 1
        elif email_counts[email] != 1:
            counts['skipped_shared_email'] += 1
        elif not isinstance(account.get('balance'), int) or isinstance(account.get('balance'), bool):
            counts['skipped_points'] += 1
        else:
            properties = {'bc_square_customer_id': cid,
                'bc_loyalty_account_id': account.get('id') or '',
                'bc_loyalty_points': account['balance'],
                'bc_marketing_permission_basis': 'Owner confirmed loyalty enrollment requires email marketing opt-in',
                'bc_purchase_tracking_ready': False}
            profile = {'email': email, 'customProperties': properties}
            for key, source in [('firstName', 'given_name'), ('lastName', 'family_name')]:
                if customer.get(source):
                    profile[key] = customer[source]
            profiles.append(profile)
    counts['eligible_profiles'] = len(profiles)
    return profiles, dict(counts)


class OmnisendClient:
    def __init__(self, key):
        if not key:
            raise ValueError('OMNISEND_API_KEY is missing.')
        self.key = key

    def request(self, method, path='', **kwargs):
        time.sleep(.2)  # <=300 requests/minute; endpoint limit is 400.
        try:
            result = requests.request(method, 'https://api.omnisend.com/api/contacts' + path,
                headers={'Authorization':'Omnisend-API-Key ' + self.key,
                         'Omnisend-Version':'2026-03-15'},
                timeout=(10,30), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ValueError('Network interruption. The last write outcome may be unknown; no automatic retry was made.') from None
        if result.status_code not in (200,201):
            raise ValueError('Omnisend HTTP %s. Stopped; previously completed updates remain saved.' % result.status_code)
        try:
            body = result.json()
        except ValueError:
            raise ValueError('Unexpected Omnisend response. Stopped without retrying.') from None
        return result.status_code, body

    def lookup(self, email):
        _, result = self.request('GET', params={'email':email,'limit':2})
        contacts = result.get('contacts')
        if not isinstance(contacts,list) or len(contacts)>1:
            raise ValueError('Unexpected or ambiguous contact lookup. No write made for this profile.')
        if not contacts:
            return None
        contact=contacts[0]
        emails=[i for i in contact.get('identifiers',[]) if i.get('type')=='email'
                and i.get('id','').lower()==email]
        if len(emails)!=1 or not contact.get('id'):
            raise ValueError('Contact identity could not be verified. No write made for this profile.')
        contact['_email_status']=emails[0].get('channels',{}).get('email',{}).get('status')
        return contact


def sync_profiles(profiles, client, apply=False):
    counts=Counter()
    # Timestamp denotes this import's subscription registration, not the original
    # customer opt-in date. Never fabricate a consent.createdAt field.
    imported_at=datetime.now(timezone.utc).isoformat()
    for profile in profiles:
        existing=client.lookup(profile['email'])
        if existing and existing['_email_status']!='subscribed':
            counts['skipped_existing_'+str(existing['_email_status'])]+=1
            continue
        if existing:
            previous=existing.get('customProperties',{}).get('bc_square_customer_id')
            if previous and previous!=profile['customProperties']['bc_square_customer_id']:
                counts['skipped_identity_conflict']+=1
                continue
        payload={k:v for k,v in profile.items() if k!='email'}
        if not apply:
            counts['would_update' if existing else 'would_create']+=1
            continue
        if existing:
            # No identifiers/channel writes: existing unsubscribes are never reset.
            client.request('PATCH','/'+quote(existing['id'],safe=''),json=payload)
            counts['updated']+=1
        else:
            payload['identifiers']=[{'type':'email','id':profile['email'],
                'channels':{'email':{'status':'subscribed','statusChangedAt':imported_at}}}]
            payload['tags']=['source: square-loyalty']
            status,_=client.request('POST',json=payload)
            counts['created' if status==201 else 'upserted']+=1
        if sum(counts.values())%50==0:
            print(json.dumps(dict(counts)),flush=True)
    return dict(counts)


def load_fresh_profiles():
    from app import app, cache_set
    from square_api import fetch_loyalty_accounts, fetch_customer_directory
    with app.app_context():
        accounts=fetch_loyalty_accounts()
        customers=fetch_customer_directory()
        if not accounts.get('ok') or not customers.get('ok'):
            raise ValueError('Square refresh failed. No stale snapshot will be uploaded.')
        cache_set('loyalty_accounts_v4', accounts)
        cache_set('customer_directory_v1', customers)
        return prepare_profiles(accounts['accounts'], customers['customers'])


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name('.env'))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Write profiles. Default is read-only preview.')
    parser.add_argument('--automations-paused',action='store_true',help='Confirm all Omnisend workflows are disabled for this import.')
    parser.add_argument('--limit',type=int,default=None,help='Optional limit for a small initial test.')
    args=parser.parse_args()
    if args.apply and not args.automations_paused:
        parser.error('Disable Omnisend automations first, then pass --automations-paused. Contact changes can trigger emails.')
    if args.limit is not None and args.limit<1:
        parser.error('--limit must be positive')
    try:
        client=OmnisendClient(os.getenv('OMNISEND_API_KEY','').strip())
        print('Refreshing Square loyalty and customer snapshots…',flush=True)
        profiles,counts=load_fresh_profiles()
        print(json.dumps(counts,indent=2),flush=True)
        result=sync_profiles(profiles[:args.limit],client,args.apply)
        print(json.dumps(result,indent=2),flush=True)
        print('Profile sync finished. No custom events sent.' if args.apply else 'Preview finished. No Omnisend contacts changed.')
    except ValueError as exc:
        print(str(exc),flush=True)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
