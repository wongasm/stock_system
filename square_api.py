import os
from collections import defaultdict
from datetime import datetime, timezone
from square.client import Client
from dotenv import load_dotenv

load_dotenv()

LOYALTY_STORES = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]


def _first_loyalty_token():
    """Loyalty is merchant-wide, so any store token with LOYALTY_READ works."""
    for store in LOYALTY_STORES:
        env_key = store.upper().replace(" ", "_")
        token = os.getenv(f"{env_key}_ACCESS_TOKEN")
        if token:
            return token
    return None


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def fetch_loyalty_summary(start_at, end_at, week_start=None, verbose=False):
    """
    Summarise loyalty activity for a period.

    start_at / end_at : ISO-8601 'Z' strings bounding the loyalty EVENTS window.
    week_start        : a date; loyalty accounts created on/after it count as "new".

    Returns a dict that never raises — {"ok": False, ...} on any problem.
    """
    result = {
        "ok": False,
        "program_status": None,
        "points_name": "Points",
        "points_issued": 0,
        "rewards_redeemed": 0,
        "total_members": 0,
        "new_members": 0,
    }

    token = _first_loyalty_token()
    if not token:
        if verbose:
            print("⚠️ No Square access token available for loyalty.")
        return result

    client = Client(access_token=token, environment="production")

    try:
        # --- Program (for status + points terminology) --------------------
        prog = client.loyalty.retrieve_loyalty_program(program_id="main")
        if prog.is_success():
            program = prog.body.get("program", {}) or {}
            result["program_status"] = program.get("status")
            terminology = program.get("terminology", {}) or {}
            result["points_name"] = terminology.get("other") or "Points"

        # --- Events (points issued / rewards redeemed in window) ----------
        cursor = None
        while True:
            body = {
                "query": {
                    "filter": {
                        "date_time_filter": {
                            "created_at": {"start_at": start_at, "end_at": end_at}
                        }
                    }
                },
                "limit": 200,
            }
            if cursor:
                body["cursor"] = cursor

            events_res = client.loyalty.search_loyalty_events(body=body)
            if not events_res.is_success():
                if verbose:
                    print("❌ Loyalty events error:", events_res.errors)
                break

            events = events_res.body.get("events", []) or events_res.body.get("loyalty_events", []) or []
            for ev in events:
                etype = ev.get("type")
                if etype == "ACCUMULATE_POINTS":
                    result["points_issued"] += (ev.get("accumulate_points", {}) or {}).get("points", 0) or 0
                elif etype == "REDEEM_REWARD":
                    result["rewards_redeemed"] += 1

            cursor = events_res.body.get("cursor")
            if not cursor:
                break

        # --- Accounts (total members + new this week) ---------------------
        cursor = None
        while True:
            body = {"limit": 200}
            if cursor:
                body["cursor"] = cursor

            acc_res = client.loyalty.search_loyalty_accounts(body=body)
            if not acc_res.is_success():
                if verbose:
                    print("❌ Loyalty accounts error:", acc_res.errors)
                break

            accounts = acc_res.body.get("loyalty_accounts", []) or []
            result["total_members"] += len(accounts)
            if week_start:
                for acc in accounts:
                    created = _parse_iso(acc.get("created_at"))
                    if created and created.date() >= week_start:
                        result["new_members"] += 1

            cursor = acc_res.body.get("cursor")
            if not cursor:
                break

        result["ok"] = True
        return result

    except Exception as exc:
        if verbose:
            print("❌ Exception building loyalty summary:", exc)
        return result

def fetch_sales_for_store(store_name, start_date=None, end_date=None, verbose=True, page_limit=500):
    # Convert store name to ENV key format (e.g., "Glen Waverley" → "GLEN_WAVERLEY")
    env_key = store_name.upper().replace(" ", "_")

    access_token = os.getenv(f"{env_key}_ACCESS_TOKEN")
    location_id = os.getenv(f"{env_key}_LOCATION_ID")
    if verbose:
        print(f"🧪 ENV key: {env_key}")
        print(f"📍 Location: {location_id}")

    if not access_token or not location_id:
        print(f"⚠️ Missing access token or location ID for {store_name}")
        return []

    # Initialize Square client
    client = Client(
        access_token=access_token,
        environment="production"
    )

    # Handle date defaults
    if not start_date:
        start_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        start_date = start_dt.isoformat().replace('+00:00', 'Z')

    if not end_date:
        end_dt = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999).astimezone(timezone.utc)
        end_date = end_dt.isoformat().replace('+00:00', 'Z')

    if verbose:
        print(f"📅 Fetching orders for {store_name} from {start_date} to {end_date}")

    try:
        orders = []
        cursor = None
        page = 1

        while True:
            body = {
                "location_ids": [location_id],
                "query": {
                    "filter": {
                        "state_filter": {
                            "states": ["OPEN", "COMPLETED"]
                        },
                        "date_time_filter": {
                            "created_at": {
                                "start_at": start_date,
                                "end_at": end_date
                            }
                        }
                    },
                    "sort": {
                        "sort_field": "CREATED_AT",
                        "sort_order": "ASC"
                    }
                }
            }

            if page_limit:
                body["limit"] = page_limit
            if cursor:
                body["cursor"] = cursor

            result = client.orders.search_orders(body=body)

            if result.is_success():
                batch = result.body.get("orders", [])
                orders.extend(batch)
                cursor = result.body.get("cursor")
                if verbose:
                    print(f"✅ Page {page}: {len(batch)} orders fetched.")
                if not cursor:
                    break
                page += 1
            else:
                if verbose:
                    print(f"❌ Error fetching orders for {store_name}:", result.errors)
                break

        if verbose:
            print(f"✅ Total orders fetched: {len(orders)}")
        return orders

    except Exception as e:
        if verbose:
            print(f"❌ Exception fetching sales for {store_name}:", e)
        return []


def _store_location_map():
    """Map Square location_id -> store name, from the *_LOCATION_ID env vars."""
    mapping = {}
    for store in LOYALTY_STORES:
        env_key = store.upper().replace(" ", "_")
        loc = os.getenv(f"{env_key}_LOCATION_ID")
        if loc:
            mapping[loc] = store
    return mapping


def fetch_loyalty_report(start_at, end_at, range_start_date, range_end_date, verbose=False):
    """
    Rich loyalty report for a date range (used by the Loyalty dashboard page).

    start_at / end_at         : ISO-8601 'Z' strings bounding the range.
    range_start_date / _end   : date objects (for counting new enrollees by created_at).

    Never raises: returns {"ok": False, ...} on failure. Individual sections are
    wrapped so a partial failure still returns whatever succeeded.
    """
    report = {
        "ok": False,
        "program_status": None,
        "points_name": "Points",
        "new_enrollees": 0,
        "total_members": 0,
        "rewards_redeemed": 0,
        "prize_breakdown": [],
        "points_issued": 0,
        "points_redeemed": 0,
        "outstanding_points": 0,
        "total_visits": 0,
        "member_visits": 0,
        "regular_visits": 0,
        "member_visit_share": 0.0,
        "total_revenue": 0.0,
        "member_revenue": 0.0,
        "regular_revenue": 0.0,
        "loyalty_revenue_share": 0.0,
        "avg_member_spend": 0.0,
        "avg_regular_spend": 0.0,
        "by_store": [],
    }

    token = _first_loyalty_token()
    if not token:
        if verbose:
            print("⚠️ No Square token for loyalty report.")
        return report

    client = Client(access_token=token, environment="production")
    location_to_store = _store_location_map()

    try:
        # --- Program: status, points name, reward-tier names --------------
        tier_names = {}
        try:
            prog = client.loyalty.retrieve_loyalty_program(program_id="main")
            if prog.is_success():
                program = prog.body.get("program", {}) or {}
                report["program_status"] = program.get("status")
                terminology = program.get("terminology", {}) or {}
                report["points_name"] = terminology.get("other") or "Points"
                for tier in program.get("reward_tiers", []) or []:
                    tier_names[tier.get("id")] = tier.get("name") or "Reward"
        except Exception as exc:
            if verbose:
                print("ℹ️ Program retrieve failed:", exc)

        # --- Events: points issued, redemptions, member order ids ---------
        loyalty_order_ids = set()
        redemptions = []  # list of reward_id
        redemptions_by_store = defaultdict(int)
        member_visits_by_store = defaultdict(int)
        cursor = None
        while True:
            body = {
                "query": {"filter": {"date_time_filter": {"created_at": {"start_at": start_at, "end_at": end_at}}}},
                "limit": 200,
            }
            if cursor:
                body["cursor"] = cursor
            ev_res = client.loyalty.search_loyalty_events(body=body)
            if not ev_res.is_success():
                if verbose:
                    print("❌ Loyalty events error:", ev_res.errors)
                break
            events = ev_res.body.get("events", []) or ev_res.body.get("loyalty_events", []) or []
            for ev in events:
                etype = ev.get("type")
                store = location_to_store.get(ev.get("location_id"))
                if etype == "ACCUMULATE_POINTS":
                    ap = ev.get("accumulate_points", {}) or {}
                    report["points_issued"] += ap.get("points", 0) or 0
                    oid = ap.get("order_id")
                    if oid:
                        loyalty_order_ids.add(oid)
                    if store:
                        member_visits_by_store[store] += 1
                elif etype == "REDEEM_REWARD":
                    rr = ev.get("redeem_reward", {}) or {}
                    reward_id = rr.get("reward_id")
                    redemptions.append(reward_id)
                    report["rewards_redeemed"] += 1
                    if store:
                        redemptions_by_store[store] += 1
            cursor = ev_res.body.get("cursor")
            if not cursor:
                break

        # --- Resolve redeemed rewards -> prize names + points -------------
        reward_cache = {}
        prize_counts = defaultdict(lambda: {"count": 0, "points": 0})
        for reward_id in redemptions:
            if not reward_id:
                prize_counts["Unknown"]["count"] += 1
                continue
            if reward_id not in reward_cache:
                name, points = "Unknown", 0
                try:
                    rw = client.loyalty.retrieve_loyalty_reward(reward_id=reward_id)
                    if rw.is_success():
                        reward = rw.body.get("reward", {}) or {}
                        points = reward.get("points", 0) or 0
                        name = tier_names.get(reward.get("reward_tier_id"), "Reward")
                except Exception:
                    pass
                reward_cache[reward_id] = (name, points)
            name, points = reward_cache[reward_id]
            prize_counts[name]["count"] += 1
            prize_counts[name]["points"] += points
            report["points_redeemed"] += points

        report["prize_breakdown"] = sorted(
            [{"name": n, "count": v["count"], "points": v["points"]} for n, v in prize_counts.items()],
            key=lambda x: x["count"], reverse=True
        )

        # --- Accounts: total members, new enrollees, outstanding points ---
        cursor = None
        while True:
            body = {"limit": 200}
            if cursor:
                body["cursor"] = cursor
            acc_res = client.loyalty.search_loyalty_accounts(body=body)
            if not acc_res.is_success():
                if verbose:
                    print("❌ Loyalty accounts error:", acc_res.errors)
                break
            for acc in acc_res.body.get("loyalty_accounts", []) or []:
                report["total_members"] += 1
                report["outstanding_points"] += acc.get("balance", 0) or 0
                created = _parse_iso(acc.get("created_at"))
                if created and range_start_date <= created.date() <= range_end_date:
                    report["new_enrollees"] += 1
            cursor = acc_res.body.get("cursor")
            if not cursor:
                break

        # --- Orders: total visits + revenue, split member vs regular ------
        visits_by_store = defaultdict(int)
        for store in LOYALTY_STORES:
            orders = fetch_sales_for_store(store, start_date=start_at, end_date=end_at, verbose=False)
            for order in orders:
                total = (order.get("total_money", {}) or {}).get("amount", 0) / 100
                report["total_visits"] += 1
                report["total_revenue"] += total
                visits_by_store[store] += 1
                if order.get("id") in loyalty_order_ids:
                    report["member_visits"] += 1
                    report["member_revenue"] += total
                else:
                    report["regular_visits"] += 1
                    report["regular_revenue"] += total

        # --- Derived ratios ----------------------------------------------
        if report["total_visits"]:
            report["member_visit_share"] = round(report["member_visits"] / report["total_visits"] * 100, 1)
        if report["total_revenue"]:
            report["loyalty_revenue_share"] = round(report["member_revenue"] / report["total_revenue"] * 100, 1)
        if report["member_visits"]:
            report["avg_member_spend"] = round(report["member_revenue"] / report["member_visits"], 2)
        if report["regular_visits"]:
            report["avg_regular_spend"] = round(report["regular_revenue"] / report["regular_visits"], 2)

        report["by_store"] = [
            {
                "store": store,
                "visits": visits_by_store.get(store, 0),
                "member_visits": member_visits_by_store.get(store, 0),
                "redemptions": redemptions_by_store.get(store, 0),
            }
            for store in LOYALTY_STORES
        ]

        report["total_revenue"] = round(report["total_revenue"], 2)
        report["member_revenue"] = round(report["member_revenue"], 2)
        report["regular_revenue"] = round(report["regular_revenue"], 2)
        report["ok"] = True
        return report

    except Exception as exc:
        if verbose:
            print("❌ Exception building loyalty report:", exc)
        return report
