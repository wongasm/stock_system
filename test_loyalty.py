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


if __name__ == "__main__":
    targets = [sys.argv[1]] if len(sys.argv) > 1 else STORES
    for store in targets:
        check_store(store)
    print("\nDone.")
