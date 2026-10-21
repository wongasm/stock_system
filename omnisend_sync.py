"""Sync loyalty profiles to Omnisend. Preview by default; no marketing events."""
import argparse
from contextlib import contextmanager
import fcntl
from collections import Counter
from datetime import datetime, timezone
import json
import hashlib
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


class SyncError(ValueError):
    """Locally authored error text safe to include in task logs."""


class OmnisendClient:
    def __init__(self, key):
        if not key:
            raise SyncError('OMNISEND_API_KEY is missing.')
        self.key = key

    def request(self, method, path='', **kwargs):
        time.sleep(.2)  # <=300 requests/minute; endpoint limit is 400.
        try:
            result = requests.request(method, 'https://api.omnisend.com/api/contacts' + path,
                headers={'Authorization':'Omnisend-API-Key ' + self.key,
                         'Omnisend-Version':'2026-03-15'},
                timeout=(10,30), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SyncError('Network interruption. The last write outcome may be unknown; no automatic retry was made.') from None
        if result.status_code not in (200,201):
            raise SyncError('Omnisend HTTP %s. Stopped; previously completed updates remain saved.' % result.status_code)
        try:
            body = result.json()
        except ValueError:
            raise SyncError('Unexpected Omnisend response. Stopped without retrying.') from None
        return result.status_code, body

    def lookup(self, email):
        _, result = self.request('GET', params={'email':email,'limit':2})
        contacts = result.get('contacts')
        if not isinstance(contacts,list) or len(contacts)>1:
            raise SyncError('Unexpected or ambiguous contact lookup. No write made for this profile.')
        if not contacts:
            return None
        contact=contacts[0]
        emails=[i for i in contact.get('identifiers',[]) if i.get('type')=='email'
                and i.get('id','').lower()==email]
        if len(emails)!=1 or not contact.get('id'):
            raise SyncError('Contact identity could not be verified. No write made for this profile.')
        contact['_email_status']=emails[0].get('channels',{}).get('email',{}).get('status')
        return contact


def sync_profiles(profiles, client, apply=False, scheduled=False, new_only=False):
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
        if new_only and existing:
            counts['skipped_existing']+=1
            continue
        if scheduled:
            if not existing:
                counts['skipped_not_imported']+=1
                continue
            properties=existing.get('customProperties') or {}
            if properties.get('bc_square_customer_id') != profile['customProperties']['bc_square_customer_id']:
                counts['skipped_unlinked']+=1
                continue
            account_id=profile['customProperties'].get('bc_loyalty_account_id')
            if not account_id or properties.get('bc_loyalty_account_id') != account_id:
                counts['skipped_account_conflict']+=1
                continue
            points=profile['customProperties']['bc_loyalty_points']
            if properties.get('bc_loyalty_points') == points:
                counts['unchanged']+=1
                continue
            if apply:
                # Only the points property changes; never write channel status,
                # names, consent, tags or purchase-tracking flags in hourly runs.
                client.request('PATCH','/'+quote(existing['id'],safe=''),
                               json={'customProperties':{'bc_loyalty_points':points}})
            counts['updated' if apply else 'would_update']+=1
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


def import_new_profiles(profiles, client, checkpoint_path, apply=False):
    """Resume an interrupted new-only import; retain no plain-text contact data."""
    path = Path(checkpoint_path)
    done = set()
    if apply and path.exists():
        try:
            saved = json.loads(path.read_text())
            if not isinstance(saved, list) or not all(isinstance(x, str) for x in saved):
                raise ValueError()
            done = set(saved)
        except (ValueError, OSError):
            raise SyncError('Import checkpoint could not be read. No contacts processed.') from None
    counts = Counter()
    for profile in profiles:
        identity = [profile['email'].lower(),
                    profile['customProperties'].get('bc_square_customer_id'),
                    profile['customProperties'].get('bc_loyalty_account_id')]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if key in done:
            counts['skipped_already_processed'] += 1
        else:
            counts.update(sync_profiles([profile], client, apply=apply, new_only=True))
            if apply:
                # Mark only after a confirmed result. An ambiguous write is looked
                # up again on the next run, never blindly repeated.
                done.add(key)
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps(sorted(done)))
                temp.replace(path)
        if sum(counts.values()) % 50 == 0:
            print(json.dumps(dict(counts)), flush=True)
    if apply and path.exists():
        path.unlink()  # Completed campaign: next import starts a fresh check.
    return dict(counts)


def load_fresh_profiles():
    from app import app, cache_set
    from square_api import fetch_loyalty_accounts, fetch_customer_directory
    with app.app_context():
        snapshots = []
        for stage, fetch in [('loyalty_accounts', fetch_loyalty_accounts),
                             ('customer_directory', fetch_customer_directory)]:
            log_status('refreshing_square', stage=stage)
            snapshot = fetch()
            if not snapshot.get('ok'):
                code = snapshot.get('error_code', 'UNKNOWN')
                # Only locally defined categories/HTTP status codes reach logs.
                if not isinstance(code, str) or not re.fullmatch(
                        r'(?:HTTP_[1-5][0-9]{2}|MISSING_TOKEN|API_ERROR|NETWORK_ERROR|SDK_ERROR)', code):
                    code = 'UNKNOWN'
                raise SyncError('Square refresh failed: %s (%s). No Omnisend contacts changed by this run.' % (stage, code))
            snapshots.append(snapshot)
        accounts, customers = snapshots
        cache_set('loyalty_accounts_v4', accounts)
        cache_set('customer_directory_v1', customers)
        return prepare_profiles(accounts['accounts'], customers['customers'])


@contextmanager
def sync_lock(path):
    # Keep the file in place: unlinking it could let another process lock a
    # different inode. The OS releases flock even if this process crashes.
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def log_status(status, **fields):
    print(json.dumps({'time':datetime.now(timezone.utc).isoformat(),
                      'status':status, **fields}), flush=True)


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name('.env'))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Write profiles. Default is read-only preview.')
    parser.add_argument('--automations-paused',action='store_true',help='Confirm all Omnisend workflows are disabled for this import.')
    parser.add_argument('--new-only', action='store_true', help='Import missing customers only; resume interrupted live imports.')
    parser.add_argument('--scheduled',action='store_true',help='Update changed points only for existing linked subscribers. Enabled segment workflows may trigger.')
    parser.add_argument('--limit',type=int,default=None,help='Optional limit for a small initial test.')
    args=parser.parse_args()
    if args.new_only and (args.scheduled or args.limit is not None):
        parser.error('--new-only cannot be combined with --scheduled or --limit')
    if args.apply and not (args.automations_paused or args.scheduled):
        parser.error('Disable Omnisend automations first, then pass --automations-paused. Contact changes can trigger emails.')
    if args.limit is not None and args.limit<1:
        parser.error('--limit must be positive')
    try:
        with sync_lock(Path(__file__).with_name('.omnisend-sync.lock')) as acquired:
            if not acquired:
                log_status('skipped_already_running')
                return 0
            log_status('started', mode='scheduled' if args.scheduled else 'import', apply=args.apply)
            client=OmnisendClient(os.getenv('OMNISEND_API_KEY','').strip())
            print('Refreshing Square loyalty and customer snapshots…',flush=True)
            profiles,counts=load_fresh_profiles()
            print(json.dumps(counts,indent=2),flush=True)
            if args.new_only:
                result=import_new_profiles(profiles, client,
                    Path(__file__).with_name('.omnisend-new-import.json'), apply=args.apply)
            else:
                result=sync_profiles(profiles[:args.limit],client,args.apply,scheduled=args.scheduled)
            log_status('finished', counts=result)
            print('Profile sync finished. No custom events sent.' if args.apply else 'Preview finished. No Omnisend contacts changed.')
    except SyncError as exc:
        log_status('failed', error_type=type(exc).__name__, reason=str(exc))
        return 1
    except ValueError as exc:
        # Log the exception class rather than arbitrary upstream response text,
        # which could include contact data or credentials.
        log_status('failed', error_type=type(exc).__name__)
        return 1
    except Exception as exc:
        log_status('failed', error_type=type(exc).__name__)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
