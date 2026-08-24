"""
Diagnostic: can my stored Square tokens read Loyalty data?

Run on PythonAnywhere (where .env + the Square SDK exist):
    python3 test_loyalty.py            # checks all stores
    python3 test_loyalty.py "Clayton"  # checks one store

It prints, per store:
  1. The token's SCOPES (so you can see if LOYALTY_READ is present)
  2. A real Loyalty API call (retrieve the "main" loyalty program)
  3. A quick loyalty-accounts read

It NEVER prints the access token itself.
"""
import os
import sys
from dotenv import load_dotenv
from square.client import Client

load_dotenv()

STORES = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]


def check_store(store_name):
    env_key = store_name.upper().replace(" ", "_")
    access_token = os.getenv(f"{env_key}_ACCESS_TOKEN")

    print("\n" + "=" * 60)
    print(f"STORE: {store_name}   (env: {env_key}_ACCESS_TOKEN)")
    print("=" * 60)

    if not access_token:
        print("⚠️  No access token found in .env — skipping.")
        return

    client = Client(access_token=access_token, environment="production")

    # 1) What scopes does this token actually have? -----------------------
    try:
        status = client.o_auth.retrieve_token_status()
        if status.is_success():
            scopes = status.body.get("scopes", [])
            print(f"🔑 Token scopes ({len(scopes)}):")
            for s in scopes:
                mark = "  ✅" if s.startswith("LOYALTY") else "   -"
                print(f"{mark} {s}")
            if not any(s.startswith("LOYALTY_READ") for s in scopes):
                print("   ⚠️  LOYALTY_READ is NOT in the scope list above.")
        else:
            print("ℹ️  Couldn't read token status:", status.errors)
    except Exception as e:
        print("ℹ️  Token-status check not available on this SDK version:", e)

    # 2) Real Loyalty call: retrieve the seller's loyalty program ---------
    try:
        result = client.loyalty.retrieve_loyalty_program(program_id="main")
        if result.is_success():
            program = result.body.get("program", {})
            print("\n✅ LOYALTY ACCESS WORKS — program retrieved:")
            print("   Program ID :", program.get("id"))
            print("   Status     :", program.get("status"))
            terminology = program.get("terminology", {})
            print("   Points name:", terminology.get("one"), "/", terminology.get("other"))
            print("   Locations  :", len(program.get("location_ids", []) or []))
        else:
            print("\n❌ Loyalty program call FAILED:")
            for err in result.errors or []:
                print(f"   {err.get('category')} / {err.get('code')}: {err.get('detail')}")
    except Exception as e:
        print("\n❌ Exception calling Loyalty API:", e)

    # 3) Can we read loyalty accounts? ------------------------------------
    try:
        acc = client.loyalty.search_loyalty_accounts(body={"limit": 1})
        if acc.is_success():
            n = len(acc.body.get("loyalty_accounts", []) or [])
            print(f"✅ Loyalty accounts readable (sample returned: {n}).")
        else:
            codes = ", ".join(e.get("code", "?") for e in (acc.errors or []))
            print(f"❌ Loyalty accounts read failed: {codes}")
    except Exception as e:
        print("❌ Exception reading loyalty accounts:", e)


def print_loyalty_numbers():
    """Print the real loyalty summary the dashboard widget will show."""
    from datetime import date, timedelta
    from square_api import fetch_loyalty_summary

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    start_at = f"{monday.isoformat()}T00:00:00Z"
    end_at = f"{today.isoformat()}T23:59:59Z"

    print("\n" + "=" * 60)
    print(f"LOYALTY NUMBERS  (week of {monday.strftime('%d %b %Y')} → today)")
    print("=" * 60)
    s = fetch_loyalty_summary(start_at, end_at, week_start=monday, verbose=True)
    if not s["ok"]:
        print("❌ Could not build loyalty summary (see messages above).")
        return
    print(f"Program status     : {s['program_status']}")
    print(f"Total members      : {s['total_members']:,}")
    print(f"New members (wk)    : +{s['new_members']}")
    print(f"{s['points_name']} issued (wk) : {s['points_issued']:,}")
    print(f"Rewards redeemed(wk): {s['rewards_redeemed']}")


def print_loyalty_report():
    """Exercise the full Loyalty-dashboard report for this week and print it."""
    from datetime import date, timedelta
    from square_api import fetch_loyalty_report

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    start_at = f"{monday.isoformat()}T00:00:00Z"
    end_at = f"{today.isoformat()}T23:59:59Z"

    print("\n" + "=" * 60)
    print(f"LOYALTY REPORT  (week of {monday.strftime('%d %b %Y')} → today)")
    print("=" * 60)
    r = fetch_loyalty_report(start_at, end_at, monday, today, verbose=True)
    if not r["ok"]:
        print("❌ Report failed — see messages above.")
        return
    print(f"New enrollees        : {r['new_enrollees']}")
    print(f"Total members        : {r['total_members']:,}")
    print(f"Member transactions  : {r['member_transactions']:,}")
    print(f"{r['points_name']} issued : {r['points_issued']:,}  (avg/txn {r['avg_points_per_txn']})")
    print(f"Rewards redeemed     : {r['rewards_redeemed']}")
    for p in r["prize_breakdown"]:
        print(f"    - {p['name']}: {p['count']} ({p['points']} pts)")
    print(f"Points redeemed      : {r['points_redeemed']:,}")
    print(f"Outstanding points   : {r['outstanding_points']:,}")
    print(f"Members can redeem   : {r['members_can_redeem']} (min tier {r['min_tier_points']})")
    for t in r["tier_reach"]:
        print(f"    {t['name']} ({t['points']}): {t['members_reached']} members ({t['pct']}%)")
    print("By store:")
    for s in r.get("by_store", []):
        print(f"    {s['store']:15s} visits={s['visits']:<6} issued={s['points_issued']:<8} rewards={s['rewards_redeemed']}")
    sus = r.get("suspicious", [])
    print(f"Suspicious activity: {len(sus)}")
    for s in sus:
        print(f"    [{s['severity']}] {s['kind']} — {s['member']} @ {s['store']}: {s['detail']}")


def print_customers():
    """Verify the customer directory loads (needs CUSTOMERS_READ) and how many
    members have an email on file."""
    from square_api import fetch_customer_directory, fetch_loyalty_accounts
    print("\nFetching customer directory (ListCustomers)...")
    d = fetch_customer_directory(verbose=True)
    if not d["ok"]:
        print("❌ Could not load customers — token likely missing CUSTOMERS_READ.")
        return
    custs = d["customers"]
    with_email = sum(1 for c in custs.values() if c.get("email"))
    with_name = sum(1 for c in custs.values() if c.get("name"))
    print(f"✅ Customers: {len(custs):,} | with email: {with_email:,} | with name: {with_name:,}")

    accts = fetch_loyalty_accounts().get("accounts", [])
    linked = sum(1 for a in accts if a.get("customer_id") in custs)
    linked_email = sum(1 for a in accts if custs.get(a.get("customer_id"), {}).get("email"))
    print(f"Loyalty members: {len(accts):,} | linked to a customer: {linked:,} | with email: {linked_email:,}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        print_loyalty_report()
    elif len(sys.argv) > 1 and sys.argv[1] == "--customers":
        print_customers()
    elif len(sys.argv) > 1 and sys.argv[1] == "--numbers":
        print_loyalty_numbers()
    else:
        targets = [sys.argv[1]] if len(sys.argv) > 1 else STORES
        for store in targets:
            check_store(store)
        print_loyalty_numbers()
    print("\nDone.")
