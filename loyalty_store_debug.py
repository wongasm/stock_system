"""
Answer two questions from the raw Square Loyalty data:
  1. Can loyalty activity be broken down BY STORE? (are events location-tagged,
     and is there more than one location?)
  2. What are the ADJUST_POINTS "reasons"? (to fix the member-visits count)

Run on PythonAnywhere:
    python3 loyalty_store_debug.py
"""
import os
import re
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
            return t
    return None


# --- Map each store's Square location_id (from env) --------------------------
print("=" * 72)
print("STORE  ->  LOCATION_ID  (from .env)")
print("=" * 72)
loc_to_store = {}
for s in STORES:
    loc = os.getenv(s.upper().replace(" ", "_") + "_LOCATION_ID")
    loc_to_store[loc] = s
    print(f"   {s:15s} {loc}")

client = Client(access_token=first_token(), environment="production")

# --- Program location(s) -----------------------------------------------------
prog = client.loyalty.retrieve_loyalty_program(program_id="main")
program_locs = []
if prog.is_success():
    program_locs = (prog.body.get("program", {}) or {}).get("location_ids", []) or []
print("\nProgram location_ids:", program_locs)
for lid in program_locs:
    print(f"   {lid}  ->  {loc_to_store.get(lid, '⚠️ NOT one of your 4 store location_ids')}")

# --- 60 days of events: which locations appear, per type ---------------------
end = datetime.now(timezone.utc)
start = end - timedelta(days=60)
start_at = start.isoformat().replace("+00:00", "Z")
end_at = end.isoformat().replace("+00:00", "Z")

type_loc = defaultdict(lambda: defaultdict(int))   # type -> location -> count
adjust_reasons = defaultdict(lambda: {"count": 0, "points": 0, "neg": 0})
cursor = None
while True:
    body = {
        "query": {"filter": {"date_time_filter": {"created_at": {"start_at": start_at, "end_at": end_at}}}},
        "limit": 30,
    }
    if cursor:
        body["cursor"] = cursor
    res = client.loyalty.search_loyalty_events(body=body)
    if not res.is_success():
        print("\nEVENTS ERROR:", res.errors)
        break
    for e in res.body.get("events", []) or []:
        etype = e.get("type")
        loc = e.get("location_id") or "(no location)"
        type_loc[etype][loc] += 1
        if etype == "ADJUST_POINTS":
            reason = (e.get("adjust_points", {}) or {}).get("reason", "") or "(none)"
            key = re.split(r"\s+#", reason)[0].strip()  # drop the "#code" suffix
            pts = (e.get("adjust_points", {}) or {}).get("points", 0) or 0
            adjust_reasons[key]["count"] += 1
            adjust_reasons[key]["points"] += pts
            if pts < 0:
                adjust_reasons[key]["neg"] += 1
    cursor = res.body.get("cursor")
    if not cursor:
        break

print("\n" + "=" * 72)
print("EVENTS BY TYPE  ->  LOCATION  (last 60 days)")
print("=" * 72)
for etype, locs in type_loc.items():
    print(f"\n{etype}:")
    for loc, n in sorted(locs.items(), key=lambda x: -x[1]):
        label = loc_to_store.get(loc, loc)
        print(f"   {n:5d}   {loc}   ({label})")

print("\n" + "=" * 72)
print("ADJUST_POINTS BY REASON  (last 60 days)")
print("=" * 72)
for reason, d in sorted(adjust_reasons.items(), key=lambda x: -x[1]["count"]):
    print(f"   {d['count']:5d} events | {d['points']:+8d} pts total | {d['neg']} negative | reason: {reason!r}")

print("\nDone.")
