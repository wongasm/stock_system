import os
from datetime import datetime, timezone
from square.client import Client
from dotenv import load_dotenv

load_dotenv()

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
