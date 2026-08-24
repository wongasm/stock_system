"""
Diagnose the "member visits" count for THIS WEEK (Mon 00:00 UTC -> now).

Shows, per store: purchase-event count vs distinct members vs distinct
member-days, and lists Glen Waverley's raw purchase events with timestamps so
we can see if a single visit produces several events.

Run on PythonAnywhere:
    python3 loyalty_week_debug.py
"""
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from square.client import Client

load_dotenv()

STORES = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]

loc_to_store = {}
for s in STORES:
    loc = os.getenv(s.upper().replace(" ", "_") + "_LOCATION_ID")
    if loc:
        loc_to_store[loc] = s


def first_token():
    for s in STORES:
        t = os.getenv(s.upper().replace(" ", "_") + "_ACCESS_TOKEN")
        if t:
            return t
    return None


def event_store(ev):
    loc = ev.get("location_id")
    if loc and loc in loc_to_store:
        return loc_to_store[loc]
    if ev.get("type") == "ADJUST_POINTS":
        reason = (ev.get("adjust_points", {}) or {}).get("reason", "") or ""
        m = re.search(r"#(\w+)", reason)
        if m and m.group(1) in loc_to_store:
            return loc_to_store[m.group(1)]
    return None


def is_purchase(ev):
    if ev.get("type") == "ACCUMULATE_POINTS":
        return True
    if ev.get("type") == "ADJUST_POINTS":
        ap = ev.get("adjust_points", {}) or {}
        return "accumulate points" in (ap.get("reason", "") or "").lower() and (ap.get("points", 0) or 0) > 0
    return False


client = Client(access_token=first_token(), environment="production")

now = datetime.now(timezone.utc)
monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
start_at = monday.isoformat().replace("+00:00", "Z")
end_at = now.isoformat().replace("+00:00", "Z")
print(f"THIS-WEEK window (UTC): {start_at}  ->  {end_at}")
print(f"(That is Monday 00:00 UTC to now; note UTC vs Melbourne offset.)")

events = []
cursor = None
while True:
    body = {"query": {"filter": {"date_time_filter": {"created_at": {"start_at": start_at, "end_at": end_at}}}}, "limit": 30}
    if cursor:
        body["cursor"] = cursor
    res = client.loyalty.search_loyalty_events(body=body)
    if not res.is_success():
        print("EVENTS ERROR:", res.errors)
        break
    events.extend(res.body.get("events", []) or [])
    cursor = res.body.get("cursor")
    if not cursor:
        break

print(f"\nTotal loyalty events this week: {len(events)}")

visit_events = defaultdict(int)
distinct_members = defaultdict(set)
member_days = defaultdict(set)
gw_events = []

for ev in events:
    if not is_purchase(ev):
        continue
    store = event_store(ev)
    if not store:
        continue
    acct = ev.get("loyalty_account_id")
    day = (ev.get("created_at") or "")[:10]
    visit_events[store] += 1
    distinct_members[store].add(acct)
    member_days[store].add((acct, day))
    if store == "Glen Waverley":
        gw_events.append(ev)

print("\nPer store (this week):")
print(f"   {'STORE':15s} {'events':>7} {'distinct_members':>17} {'member-days':>12}")
for s in STORES:
    print(f"   {s:15s} {visit_events[s]:>7} {len(distinct_members[s]):>17} {len(member_days[s]):>12}")

print("\nGlen Waverley purchase events (up to 80), newest first-ish:")
gw_events.sort(key=lambda e: e.get("created_at") or "")
for ev in gw_events[:80]:
    ap = ev.get("accumulate_points") or ev.get("adjust_points") or {}
    acct = (ev.get("loyalty_account_id") or "")[:8]
    reason = (ap.get("reason") or ev.get("type") or "")[:38]
    print(f"   {ev.get('created_at')}  pts={ap.get('points')!s:>5}  acct={acct}  {reason}")

print("\nDone.")
