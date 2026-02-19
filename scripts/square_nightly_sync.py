from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import app, sync_square_orders
from models import User


def main():
    tz = ZoneInfo("Australia/Melbourne")
    now_local = datetime.now(tz)
    yesterday = (now_local - timedelta(days=1)).date()

    start_local = datetime.combine(yesterday, datetime.min.time(), tzinfo=tz)
    end_local = datetime.combine(yesterday, datetime.max.time(), tzinfo=tz)

    start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    with app.app_context():
        stores = User.query.filter_by(role="user").all()
        summary = []
        for store in stores:
            result = sync_square_orders(store.username, start_utc, end_utc, verbose=False)
            status = "SKIPPED" if result.get("skipped") else "OK"
            summary.append(
                f"{store.username}:{status} orders={result['orders_inserted']} lines={result['lines_inserted']} ledger={result['ledger_entries']}"
            )

        print(" | ".join(summary))


if __name__ == "__main__":
    main()
