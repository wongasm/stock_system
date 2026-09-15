"""Read-only Omnisend connection check and local loyalty-data readiness report."""
import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path

import requests


def check_connection():
    key = os.getenv('OMNISEND_API_KEY', '').strip()
    if not key:
        raise ValueError('OMNISEND_API_KEY is missing. Add it to stock_system/.env.')
    try:
        response = requests.get('https://api.omnisend.com/api/contacts',
            headers={'Authorization': 'Omnisend-API-Key ' + key,
                     'Omnisend-Version': '2026-03-15', 'Accept': 'application/json'},
            params={'limit': 1}, timeout=(10, 30), allow_redirects=False)
    except requests.RequestException:
        raise ValueError('Could not reach Omnisend. Check the server network and retry.') from None
    if response.status_code != 200:
        reasons = {401: 'Check the API key.', 403: 'Enable Contacts read permission.',
                   429: 'Rate limit reached; wait and retry.'}
        raise ValueError('Omnisend returned HTTP %s. %s' % (response.status_code,
            reasons.get(response.status_code, 'Check the account and API setup.')))
    # Do not print the response: it can contain customer contact details.
    return 'Connected to Omnisend. Contacts read permission confirmed.'


def summarize_profiles(accounts, customers):
    counts = Counter()
    emails = Counter()
    identities = set()
    for account in accounts:
        counts['loyalty_accounts'] += 1
        cid = account.get('customer_id')
        if not cid:
            counts['missing_customer_id'] += 1
            continue
        if cid in identities:
            counts['duplicate_customer_links'] += 1
            continue
        identities.add(cid)
        customer = customers.get(cid)
        if not customer:
            counts['missing_directory_profile'] += 1
            continue
        email = (customer.get('email') or '').strip().lower()
        if not email or '@' not in email or any(c.isspace() for c in email):
            counts['missing_or_invalid_email'] += 1
            continue
        emails[email] += 1
        counts['profiles_with_email'] += 1
        balance = account.get('balance')
        if isinstance(balance, int) and not isinstance(balance, bool) and balance >= 0:
            counts['profiles_with_points'] += 1
    counts['shared_email_addresses'] = sum(1 for count in emails.values() if count > 1)
    return dict(counts)


def preview_saved_profiles():
    # Use the deployed app configuration; don't refresh or export customer data.
    from app import app
    from models import ApiCache
    with app.app_context():
        snapshots = {}
        for key in ('loyalty_accounts_v4', 'customer_directory_v1'):
            row = ApiCache.query.filter_by(cache_key=key).first()
            if row is None or row.fetched_at is None:
                raise ValueError('Saved loyalty/customer cache is missing. Open the loyalty and lapsed-members pages first.')
            try:
                snapshot = json.loads(row.payload)
            except (ValueError, TypeError):
                raise ValueError('Saved cache could not be read; refresh the loyalty/customer cache.') from None
            if not isinstance(snapshot, dict) or not snapshot.get('ok'):
                raise ValueError('Saved cache is incomplete; refresh the loyalty/customer cache.')
            snapshots[key] = snapshot
            age = max(0, int((datetime.utcnow() - row.fetched_at).total_seconds() / 60))
            print('%s: snapshot %s minutes old%s' % (key, age, ' (stale)' if age > 360 else ''))
        counts = summarize_profiles(snapshots['loyalty_accounts_v4'].get('accounts', []),
                                    snapshots['customer_directory_v1'].get('customers', {}))
        print(json.dumps(counts, indent=2, sort_keys=True))
        print('Email permission: not recorded by the current loyalty signup. No contacts are marked subscribed.')
        print('Purchase tracking: saved sales need customer links before inactivity/return attribution can run.')


def main():
    from dotenv import load_dotenv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', action='store_true', help='Also summarize saved Square loyalty profiles locally.')
    args = parser.parse_args()
    load_dotenv(Path(__file__).with_name('.env'))
    try:
        print(check_connection())
        if args.preview:
            preview_saved_profiles()
    except ValueError as exc:
        print('Setup check: ' + str(exc))
        return 1
    print('No contacts uploaded. No events triggered. No emails sent.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
