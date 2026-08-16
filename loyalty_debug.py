"""
Dump the raw Square Loyalty API data so we can see exactly what's available
and why some dashboard numbers are 0.

Run on PythonAnywhere:
    python3 loyalty_debug.py

Prints program details, event counts + raw samples (last 60 days), rewards,
and a few accounts. Never prints your access token.
"""
import os
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from square.client import Client

load_dotenv()

STORES = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]


def first_token():
    for s in STORES:
        t = os.getenv(s.upper().replace(" ", "_") + "_ACCESS_TOKEN")
        if t:
            return t, s
    return None, None


def pretty(obj, limit=2500):
    print(json.dumps(obj, indent=2, default=str)[:limit])


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


token, store = first_token()
if not token:
    print("No access token found.")
    raise SystemExit(1)

print(f"Using token from store: {store}")
client = Client(access_token=token, environment="production")

# ---------------------------------------------------------------- 1. PROGRAM
rule("LOYALTY PROGRAM")
prog_res = client.loyalty.retrieve_loyalty_program(program_id="main")
if prog_res.is_success():
    program = prog_res.body.get("program", {}) or {}
    print("status        :", program.get("status"))
    print("terminology   :", program.get("terminology"))
    print("location_ids  :", program.get("location_ids"))
    print("expiration    :", program.get("expiration_policy"))
    print("\nreward_tiers:")
    for t in program.get("reward_tiers", []) or []:
        print(f"   id={t.get('id')} | name={t.get('name')!r} | points={t.get('points')}")
    print("\naccrual_rules:")
    pretty(program.get("accrual_rules", []), 1800)
else:
    print("ERROR:", prog_res.errors)

# ---------------------------------------------------------------- 2. EVENTS
rule("EVENTS — last 60 days (count by type + one raw sample each)")
end = datetime.now(timezone.utc)
start = end - timedelta(days=60)
start_at = start.isoformat().replace("+00:00", "Z")
end_at = end.isoformat().replace("+00:00", "Z")

by_type = defaultdict(int)
samples = {}
total = 0
cursor = None
error = None
while True:
    body = {
        "query": {"filter": {"date_time_filter": {"created_at": {"start_at": start_at, "end_at": end_at}}}},
        "limit": 30,
    }
    if cursor:
        body["cursor"] = cursor
    res = client.loyalty.search_loyalty_events(body=body)
    if not res.is_success():
        error = res.errors
        break
    events = res.body.get("events", []) or res.body.get("loyalty_events", []) or []
    for e in events:
        total += 1
        by_type[e.get("type")] += 1
        samples.setdefault(e.get("type"), e)
    cursor = res.body.get("cursor")
    if not cursor:
        break

if error:
    print("EVENTS ERROR:", error)
else:
    print("total events in 60 days:", total)
    print("by type              :", dict(by_type))
    for t, e in samples.items():
        print(f"\n--- raw sample: {t} ---")
        pretty(e)

# ------------------------------------------------ 3. EVENTS (no filter, newest)
rule("EVENTS — newest 3, no date filter (sanity check)")
res = client.loyalty.search_loyalty_events(body={"limit": 3})
if res.is_success():
    for e in (res.body.get("events", []) or [])[:3]:
        pretty(e)
else:
    print("ERROR:", res.errors)

# ---------------------------------------------------------------- 4. ACCOUNTS
rule("ACCOUNTS — sample of 5 (check created_at + balance)")
res = client.loyalty.search_loyalty_accounts(body={"limit": 5})
if res.is_success():
    for a in (res.body.get("loyalty_accounts", []) or [])[:5]:
        print(f"id={a.get('id')} | created_at={a.get('created_at')} | "
              f"balance={a.get('balance')} | lifetime={a.get('lifetime_points')} | "
              f"enrolled_at={a.get('enrolled_at')}")
    print("\n--- one full raw account ---")
    accts = res.body.get("loyalty_accounts", []) or []
    if accts:
        pretty(accts[0])
else:
    print("ERROR:", res.errors)

print("\nDone.")
