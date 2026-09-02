import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from square.client import Client
from dotenv import load_dotenv

load_dotenv()

LOYALTY_STORES = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]

# Suspicious-activity thresholds (tune to taste)
SUS_BURST_EVENTS = 8        # >= this many point grants...
SUS_BURST_WINDOW = 600      # ...within this many seconds (10 min) = a burst
SUS_HIGH_DAY_POINTS = 250   # one member earning >= this many points in a day (~$250)
SUS_MANY_REDEEMS = 3        # one member redeeming >= this many rewards in the period
SUS_MANY_MANUAL = 3         # one member getting >= this many manual adjustments
SUS_BIG_MANUAL = 200        # ...or a manual adjustment total this large (abs points)


def _max_events_in_window(times, window_seconds):
    """Largest number of timestamps falling within any window_seconds span,
    plus that window's start/end. `times` is a list of datetimes (or None)."""
    pts = sorted(t for t in times if t)
    if len(pts) < 2:
        return len(pts), None, None
    best, start, end = 1, None, None
    j = 0
    for i in range(len(pts)):
        while (pts[i] - pts[j]).total_seconds() > window_seconds:
            j += 1
        if i - j + 1 > best:
            best, start, end = i - j + 1, pts[j], pts[i]
    return best, start, end


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


def fetch_loyalty_program_details(verbose=False):
    """
    Return public-safe program details for the customer loyalty lookup page.

    Includes the point terminology and reward tiers, but no member data.
    """
    result = {
        "ok": False,
        "program_status": None,
        "points_name": "Points",
        "reward_tiers": [],
    }

    token = _first_loyalty_token()
    if not token:
        if verbose:
            print("⚠️ No Square access token available for loyalty program.")
        return result

    client = Client(access_token=token, environment="production")

    try:
        prog = client.loyalty.retrieve_loyalty_program(program_id="main")
        if not prog.is_success():
            if verbose:
                print("❌ Loyalty program error:", prog.errors)
            return result

        program = prog.body.get("program", {}) or {}
        terminology = program.get("terminology", {}) or {}
        tiers = []
        for tier in program.get("reward_tiers", []) or []:
            tiers.append({
                "id": tier.get("id"),
                "name": tier.get("name") or "Reward",
                "points": tier.get("points", 0) or 0,
            })
        tiers.sort(key=lambda t: t["points"])

        result.update({
            "ok": True,
            "program_status": program.get("status"),
            "points_name": terminology.get("other") or "Points",
            "reward_tiers": tiers,
        })
        return result

    except Exception as exc:
        if verbose:
            print("❌ Exception loading loyalty program:", exc)
        return result


def _loyalty_account_summary(acc):
    return {
        "id": acc.get("id"),
        "customer_id": acc.get("customer_id"),
        "balance": acc.get("balance", 0) or 0,
        "lifetime": acc.get("lifetime_points", 0) or 0,
        "created_at": acc.get("created_at"),
        "enrolled_at": acc.get("enrolled_at"),
        "updated_at": acc.get("updated_at"),   # ~ last activity/visit
        "phone": (acc.get("mapping", {}) or {}).get("phone_number"),
        "expiring_point_deadlines": acc.get("expiring_point_deadlines") or [],
    }


def _customer_summary(customer):
    given_name = customer.get("given_name")
    family_name = customer.get("family_name")
    name = " ".join(part for part in [given_name, family_name] if part).strip()
    return {
        "id": customer.get("id"),
        "email": customer.get("email_address"),
        "name": name or customer.get("nickname"),
        "given_name": given_name,
        "family_name": family_name,
        "phone": customer.get("phone_number"),
        "version": customer.get("version"),
    }


def fetch_loyalty_accounts_by_phone(phone_numbers, verbose=False):
    """
    Search Square loyalty accounts by E.164 phone-number mappings.

    Returns {"ok": bool, "accounts": [...]} and never raises.
    """
    result = {"ok": False, "accounts": []}
    token = _first_loyalty_token()
    if not token:
        return result

    mappings = []
    seen = set()
    for phone in phone_numbers or []:
        if not phone or phone in seen:
            continue
        seen.add(phone)
        mappings.append({"phone_number": phone})
    if not mappings:
        return result

    client = Client(access_token=token, environment="production")
    try:
        res = client.loyalty.search_loyalty_accounts(body={
            "query": {"mappings": mappings[:30]},
            "limit": min(max(len(mappings), 1), 200),
        })
        if not res.is_success():
            if verbose:
                print("❌ Loyalty phone lookup error:", res.errors)
            return result

        result["accounts"] = [
            _loyalty_account_summary(acc)
            for acc in res.body.get("loyalty_accounts", []) or []
        ]
        result["ok"] = True
        return result
    except Exception as exc:
        if verbose:
            print("❌ Exception searching loyalty account by phone:", exc)
        return result


def fetch_customer_profile(customer_id, verbose=False):
    """Retrieve one Square customer profile by ID."""
    result = {"ok": False, "customer": None, "errors": []}
    token = _first_loyalty_token()
    if not token or not customer_id:
        return result

    client = Client(access_token=token, environment="production")
    try:
        try:
            res = client.customers.retrieve_customer(customer_id=customer_id)
        except TypeError:
            res = client.customers.retrieve_customer(customer_id)

        if not res.is_success():
            if verbose:
                print("❌ Customer retrieve error:", res.errors)
            result["errors"] = res.errors or []
            return result

        result["customer"] = _customer_summary(res.body.get("customer", {}) or {})
        result["ok"] = True
        return result
    except Exception as exc:
        if verbose:
            print("❌ Exception retrieving customer:", exc)
        result["errors"] = [{"detail": str(exc)}]
        return result


def update_customer_profile(customer_id, given_name=None, family_name=None,
                            email_address=None, phone_number=None, version=None,
                            update_name=False, update_email=False,
                            verbose=False):
    """Update a Square customer profile linked to a loyalty account."""
    result = {"ok": False, "customer": None, "errors": []}
    token = _first_loyalty_token()
    if not token or not customer_id:
        return result

    body = {}
    if update_name:
        body["given_name"] = given_name or None
        body["family_name"] = family_name or None
    if update_email:
        body["email_address"] = email_address or None
    if phone_number:
        body["phone_number"] = phone_number
    if version:
        body["version"] = version

    client = Client(access_token=token, environment="production")
    try:
        try:
            res = client.customers.update_customer(customer_id=customer_id, body=body)
        except TypeError:
            res = client.customers.update_customer(customer_id, body)

        if not res.is_success():
            if verbose:
                print("❌ Customer update error:", res.errors)
            result["errors"] = res.errors or []
            return result

        result["customer"] = _customer_summary(res.body.get("customer", {}) or {})
        result["ok"] = True
        return result
    except Exception as exc:
        if verbose:
            print("❌ Exception updating customer:", exc)
        result["errors"] = [{"detail": str(exc)}]
        return result


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
                "limit": 30,
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
                elif etype == "ADJUST_POINTS":
                    # This merchant accrues points via ADJUST_POINTS ("Franchise
                    # accumulate points"); positive = points issued.
                    pts = (ev.get("adjust_points", {}) or {}).get("points", 0) or 0
                    if pts > 0:
                        result["points_issued"] += pts
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


def _event_store(ev, loc_to_store):
    """Attribute a loyalty event to a store.

    Square-native events carry a location_id. Franchise ADJUST_POINTS events
    have no location_id but encode the store's location_id in the reason text
    e.g. "Franchise accumulate points #LN6BD8KFFGFZZ".
    """
    loc = ev.get("location_id")
    if loc and loc in loc_to_store:
        return loc_to_store[loc]
    if ev.get("type") == "ADJUST_POINTS":
        reason = (ev.get("adjust_points", {}) or {}).get("reason", "") or ""
        m = re.search(r"#(\w+)", reason)
        if m and m.group(1) in loc_to_store:
            return loc_to_store[m.group(1)]
    return None


def fetch_loyalty_accounts(verbose=False):
    """
    Snapshot every loyalty account (the slow part: ~all members, paged 200 at a
    time). Not date-range specific, so it can be cached once and reused for any
    range. Returns {"ok": bool, "accounts": [{balance, lifetime, created_at, phone}]}.
    """
    result = {"ok": False, "accounts": []}
    token = _first_loyalty_token()
    if not token:
        return result

    client = Client(access_token=token, environment="production")
    accounts = []
    try:
        cursor = None
        while True:
            body = {"limit": 200}
            if cursor:
                body["cursor"] = cursor
            res = client.loyalty.search_loyalty_accounts(body=body)
            if not res.is_success():
                if verbose:
                    print("❌ Loyalty accounts error:", res.errors)
                return result
            for acc in res.body.get("loyalty_accounts", []) or []:
                accounts.append(_loyalty_account_summary(acc))
            cursor = res.body.get("cursor")
            if not cursor:
                break
        result["accounts"] = accounts
        result["ok"] = True
    except Exception as exc:
        if verbose:
            print("❌ Exception snapshotting loyalty accounts:", exc)
    return result


def fetch_customer_directory(verbose=False):
    """Map every Square customer_id -> {email, name, phone}, via ListCustomers
    (paged 100 at a time). Cached like the member snapshot. Needs CUSTOMERS_READ;
    returns {"ok": False, "customers": {}} if the token lacks it.
    """
    result = {"ok": False, "customers": {}}
    token = _first_loyalty_token()
    if not token:
        return result

    client = Client(access_token=token, environment="production")
    customers = {}
    try:
        cursor = None
        while True:
            res = client.customers.list_customers(cursor=cursor, limit=100)
            if not res.is_success():
                if verbose:
                    print("❌ Customers list error:", res.errors)
                return result
            for c in res.body.get("customers", []) or []:
                cid = c.get("id")
                if not cid:
                    continue
                customers[cid] = _customer_summary(c)
            cursor = res.body.get("cursor")
            if not cursor:
                break
        result["customers"] = customers
        result["ok"] = True
    except Exception as exc:
        if verbose:
            print("❌ Exception building customer directory:", exc)
    return result


def fetch_loyalty_report(start_at, end_at, range_start_date, range_end_date, accounts=None, verbose=False):
    """
    Rich loyalty report for a date range (used by the Loyalty dashboard page).

    start_at / end_at         : ISO-8601 'Z' strings bounding the range.
    accounts                  : optional pre-fetched account snapshot (list from
                                fetch_loyalty_accounts) so the slow member scan
                                can be cached and reused. Fetched live if None.
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
        "member_transactions": 0,   # accrual events ~ member visits
        "points_issued": 0,         # ~ member dollars spent (1 pt = $1)
        "avg_points_per_txn": 0.0,  # ~ avg member spend
        "rewards_redeemed": 0,
        "prize_breakdown": [],
        "points_redeemed": 0,
        "outstanding_points": 0,
        "members_can_redeem": 0,
        "min_tier_points": 0,
        "tier_reach": [],
        "top_members": [],
        "by_store": [],
        "suspicious": [],
    }

    token = _first_loyalty_token()
    if not token:
        if verbose:
            print("⚠️ No Square token for loyalty report.")
        return report

    client = Client(access_token=token, environment="production")

    try:
        # --- Program: status, points name, reward tiers -------------------
        tier_names = {}
        tiers = []  # [{"name", "points"}], cheapest first
        try:
            prog = client.loyalty.retrieve_loyalty_program(program_id="main")
            if prog.is_success():
                program = prog.body.get("program", {}) or {}
                report["program_status"] = program.get("status")
                terminology = program.get("terminology", {}) or {}
                report["points_name"] = terminology.get("other") or "Points"
                for tier in program.get("reward_tiers", []) or []:
                    tier_names[tier.get("id")] = tier.get("name") or "Reward"
                    tiers.append({"name": tier.get("name") or "Reward", "points": tier.get("points", 0) or 0})
                tiers.sort(key=lambda t: t["points"])
                if tiers:
                    report["min_tier_points"] = tiers[0]["points"]
        except Exception as exc:
            if verbose:
                print("ℹ️ Program retrieve failed:", exc)

        # --- Events: categorise into purchases vs redemptions, per store ---
        #   Purchase  = ACCUMULATE_POINTS, or ADJUST_POINTS "accumulate points"
        #   Redemption= REDEEM_REWARD (Square), or ADJUST_POINTS "create reward"
        #   Cancelled = ADJUST_POINTS "delete reward" (refund -> reverse)
        #   Manual    = ADJUST_POINTS "adjust points" (ignored for headline)
        loc_to_store = _store_location_map()
        points_to_tier = {t["points"]: t["name"] for t in tiers}

        # A "visit" = a distinct member on a distinct day. The franchise POS can
        # fire many point events per transaction, so counting raw events wildly
        # over-states visits — we de-duplicate by (loyalty_account_id, day).
        visit_days = set()                       # global {(account, day)}
        store_visit_days = defaultdict(set)      # store -> {(account, day)}
        store_points = defaultdict(int)
        store_rewards = defaultdict(int)
        prize_counts = defaultdict(lambda: {"count": 0, "points": 0})
        square_redemptions = []  # (reward_id, store)

        # Per-member trackers for suspicious-activity detection
        acct_accruals = defaultdict(list)   # id -> [(datetime, points)]
        acct_store = {}                      # id -> store seen
        acct_redeems = defaultdict(int)      # id -> reward redemptions
        acct_manual = defaultdict(lambda: {"count": 0, "pts": 0})  # manual adjusts

        def _prize_for(points):
            return points_to_tier.get(points, "Other reward")

        def _record_visit(ev, store, pts):
            acct = ev.get("loyalty_account_id")
            day = (ev.get("created_at") or "")[:10]
            key = (acct, day)
            visit_days.add(key)
            report["points_issued"] += pts
            if store:
                store_points[store] += pts
                store_visit_days[store].add(key)
            if acct:
                acct_accruals[acct].append((_parse_iso(ev.get("created_at")), pts))
                acct_store.setdefault(acct, store)

        cursor = None
        while True:
            body = {
                "query": {"filter": {"date_time_filter": {"created_at": {"start_at": start_at, "end_at": end_at}}}},
                "limit": 30,
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
                store = _event_store(ev, loc_to_store)
                acct = ev.get("loyalty_account_id")

                if etype == "ACCUMULATE_POINTS":
                    pts = (ev.get("accumulate_points", {}) or {}).get("points", 0) or 0
                    _record_visit(ev, store, pts)

                elif etype == "ADJUST_POINTS":
                    ap = ev.get("adjust_points", {}) or {}
                    pts = ap.get("points", 0) or 0
                    reason = (ap.get("reason", "") or "").lower()

                    if "accumulate points" in reason:          # a purchase
                        if pts > 0:
                            _record_visit(ev, store, pts)
                    elif "create reward" in reason:            # a redemption
                        cost = abs(pts)
                        report["rewards_redeemed"] += 1
                        report["points_redeemed"] += cost
                        prize_counts[_prize_for(cost)]["count"] += 1
                        prize_counts[_prize_for(cost)]["points"] += cost
                        if store:
                            store_rewards[store] += 1
                        if acct:
                            acct_redeems[acct] += 1
                            acct_store.setdefault(acct, store)
                    elif "delete reward" in reason:            # cancelled -> reverse
                        refund = abs(pts)
                        report["rewards_redeemed"] -= 1
                        report["points_redeemed"] -= refund
                        prize_counts[_prize_for(refund)]["count"] -= 1
                        prize_counts[_prize_for(refund)]["points"] -= refund
                        if store:
                            store_rewards[store] -= 1
                        if acct:
                            acct_redeems[acct] -= 1
                    else:                                       # manual "adjust points"
                        if acct:
                            acct_manual[acct]["count"] += 1
                            acct_manual[acct]["pts"] += pts
                            acct_store.setdefault(acct, store)

                elif etype == "REDEEM_REWARD":
                    square_redemptions.append(
                        ((ev.get("redeem_reward", {}) or {}).get("reward_id"), store)
                    )
                    if acct:
                        acct_redeems[acct] += 1
                        acct_store.setdefault(acct, store)
            cursor = ev_res.body.get("cursor")
            if not cursor:
                break

        # Resolve Square-native redemptions -> prize names + points
        reward_cache = {}
        for reward_id, store in square_redemptions:
            name, points = "Reward", 0
            if reward_id:
                if reward_id not in reward_cache:
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
            report["rewards_redeemed"] += 1
            report["points_redeemed"] += points
            prize_counts[name]["count"] += 1
            prize_counts[name]["points"] += points
            if store:
                store_rewards[store] += 1

        # Visits = distinct member-days (de-duplicated from raw point events)
        report["member_transactions"] = len(visit_days)

        # Clamp (deletes can momentarily push a bucket negative)
        report["rewards_redeemed"] = max(report["rewards_redeemed"], 0)
        report["points_redeemed"] = max(report["points_redeemed"], 0)
        if report["member_transactions"]:
            report["avg_points_per_txn"] = round(report["points_issued"] / report["member_transactions"], 1)

        report["prize_breakdown"] = sorted(
            [{"name": n, "count": v["count"], "points": v["points"]}
             for n, v in prize_counts.items() if v["count"] > 0],
            key=lambda x: x["count"], reverse=True
        )

        report["by_store"] = [
            {
                "store": s,
                "visits": len(store_visit_days.get(s, set())),
                "points_issued": store_points.get(s, 0),
                "rewards_redeemed": max(store_rewards.get(s, 0), 0),
            }
            for s in LOYALTY_STORES
        ]

        # --- Accounts: members, enrollees, liability, reward-reach, top ----
        # Uses a cached snapshot when supplied (the slow member scan), else
        # fetches it live.
        if accounts is None:
            accounts = fetch_loyalty_accounts(verbose=verbose).get("accounts", [])

        tier_reach_counts = {t["points"]: 0 for t in tiers}
        members = []  # (balance, lifetime, phone) for top-members list
        for acc in accounts:
            report["total_members"] += 1
            balance = acc.get("balance", 0) or 0
            report["outstanding_points"] += balance

            created = _parse_iso(acc.get("created_at"))
            if created and range_start_date <= created.date() <= range_end_date:
                report["new_enrollees"] += 1

            if report["min_tier_points"] and balance >= report["min_tier_points"]:
                report["members_can_redeem"] += 1
            for tp in tier_reach_counts:
                if balance >= tp:
                    tier_reach_counts[tp] += 1

            members.append((balance, acc.get("lifetime", 0) or 0, acc.get("phone")))

        total_members = report["total_members"] or 1
        report["tier_reach"] = [
            {
                "name": t["name"],
                "points": t["points"],
                "members_reached": tier_reach_counts.get(t["points"], 0),
                "pct": round(tier_reach_counts.get(t["points"], 0) / total_members * 100, 1),
            }
            for t in tiers
        ]

        members.sort(key=lambda m: m[0], reverse=True)
        report["top_members"] = [
            {"balance": b, "lifetime": lp, "phone": ph}
            for (b, lp, ph) in members[:8]
        ]

        # --- Suspicious activity (fraud / glitch detection) ---------------
        points_name = report["points_name"]
        phone_by_id = {a.get("id"): a.get("phone") for a in accounts if a.get("id")}

        def _who(acct):
            return phone_by_id.get(acct) or f"Account {(acct or '')[:8]}"

        suspicious = []
        for acct, accruals in acct_accruals.items():
            times = [dt for dt, _ in accruals]
            store_label = acct_store.get(acct) or "—"

            # 1) Point burst — many grants in a few minutes (one sale spammed)
            n, t0, t1 = _max_events_in_window(times, SUS_BURST_WINDOW)
            if n >= SUS_BURST_EVENTS and t0 and t1:
                span_min = max(1, round((t1 - t0).total_seconds() / 60))
                suspicious.append({
                    "kind": "Point burst",
                    "severity": "high",
                    "member": _who(acct),
                    "store": store_label,
                    "detail": f"{n} point grants in ~{span_min} min ({t0.strftime('%d %b %H:%M')})",
                })

            # 2) High single-day earnings (~big spend that may not be real)
            day_pts = defaultdict(int)
            for dt, pts in accruals:
                if dt:
                    day_pts[dt.date()] += pts
            for d, tot in day_pts.items():
                if tot >= SUS_HIGH_DAY_POINTS:
                    suspicious.append({
                        "kind": "High daily earnings",
                        "severity": "medium",
                        "member": _who(acct),
                        "store": store_label,
                        "detail": f"{tot:,} {points_name} (~${tot:,}) earned on {d.strftime('%d %b')}",
                    })

        # 3) Frequent redemptions by one member
        for acct, cnt in acct_redeems.items():
            if cnt >= SUS_MANY_REDEEMS:
                suspicious.append({
                    "kind": "Frequent redemptions",
                    "severity": "medium",
                    "member": _who(acct),
                    "store": acct_store.get(acct) or "—",
                    "detail": f"{cnt} rewards redeemed in this period",
                })

        # 4) Heavy manual point adjustments (staff overrides)
        for acct, m in acct_manual.items():
            if m["count"] >= SUS_MANY_MANUAL or abs(m["pts"]) >= SUS_BIG_MANUAL:
                suspicious.append({
                    "kind": "Manual point adjustments",
                    "severity": "medium",
                    "member": _who(acct),
                    "store": acct_store.get(acct) or "—",
                    "detail": f"{m['count']} manual adjustment(s), net {m['pts']:+,} {points_name}",
                })

        severity_rank = {"high": 0, "medium": 1, "low": 2}
        suspicious.sort(key=lambda x: severity_rank.get(x["severity"], 3))
        report["suspicious"] = suspicious[:50]

        report["ok"] = True
        return report

    except Exception as exc:
        if verbose:
            print("❌ Exception building loyalty report:", exc)
        return report
