"""
Background sales sync and loyalty cache warmer.

Saves new/changed Square orders to the durable sales report tables and loyalty
snapshots to api_cache. Dashboard page loads read these saved records only.

Usage on PythonAnywhere:

  # Run once (for a scheduled task that fires on an interval):
  python3 refresh_cache.py

  # Or loop forever every N seconds (for an "always-on task"):
  python3 refresh_cache.py --loop 300
"""
import sys
import time

from app import app, warm_caches
from sales_reporting import STORES, sync_step


def run_once():
    with app.app_context():
        # Bounded batches resume automatically on the next scheduled invocation.
        pending = list(STORES)
        for _ in range(10):
            next_round = []
            for store in pending:
                try:
                    progress = sync_step(store)
                    if progress.get('more') and not progress.get('busy'):
                        next_round.append(store)
                except ValueError as exc:
                    print(f"Sales sync {store}: {exc}")
            pending = next_round
            if not pending:
                break
        status = warm_caches()
        print("✅ Cache warmed:", status)
    return status


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--loop":
        interval = int(sys.argv[2]) if len(sys.argv) >= 3 else 300
        print(f"🔁 Warming caches every {interval}s. Ctrl-C to stop.")
        while True:
            try:
                run_once()
            except Exception as exc:  # keep the loop alive across transient errors
                print("⚠️ Refresh error:", exc)
            time.sleep(interval)
    else:
        run_once()
