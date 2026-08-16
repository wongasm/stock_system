"""
Background cache warmer for the dashboard.

Recomputes the expensive Square data (today's sales, trends, loyalty) and stores
it in the api_cache table, so a real login always reads pre-warmed data and
never waits on the Square API.

Usage on PythonAnywhere:

  # Run once (for a scheduled task that fires on an interval):
  python3 refresh_cache.py

  # Or loop forever every N seconds (for an "always-on task"):
  python3 refresh_cache.py --loop 300
"""
import sys
import time

from app import app, warm_caches


def run_once():
    with app.app_context():
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
