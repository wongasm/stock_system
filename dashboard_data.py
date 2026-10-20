"""Small, read-only dashboard queries over the durable sales report tables."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func

from models import db
from sales_reporting import ReportLine, ReportOrder, ReportSync, SALES_STATES, STORES, TZ

DASHBOARD_SYNC_SECONDS = 300


def saved_dashboard_sales(now=None):
    """Read saved sales only. Never contact Square, even on an empty database."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(TZ).date()
    week_start = today - timedelta(days=6)
    utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    daily = db.session.query(
        ReportOrder.business_date, ReportOrder.store,
        func.sum(ReportOrder.total), func.count(),
    ).filter(
        ReportOrder.store.in_(STORES), ReportOrder.state.in_(SALES_STATES),
        ReportOrder.business_date.between(week_start, today),
    ).group_by(ReportOrder.business_date, ReportOrder.store).all()

    revenue_by_day = {week_start + timedelta(days=i): 0 for i in range(7)}
    revenue_by_store = {store: 0 for store in STORES}
    today_cents = today_orders = 0
    for day, store, cents, count in daily:
        revenue_by_day[day] += cents
        if day == today:
            today_cents += cents
            today_orders += count
            revenue_by_store[store] += cents

    # Only today's product totals are needed. Never load a week's order payloads.
    quantities = defaultdict(Decimal)
    for name, quantity in db.session.query(ReportLine.name, func.sum(ReportLine.quantity)).join(
        ReportOrder, (ReportOrder.store == ReportLine.store) &
        (ReportOrder.order_id == ReportLine.order_id),
    ).filter(
        ReportLine.store.in_(STORES), ReportLine.business_date == today,
        ReportOrder.state.in_(SALES_STATES),
    ).group_by(ReportLine.name):
        quantities[name] += quantity

    # Older imports retained OPEN orders without projecting their product lines.
    has_lines = db.session.query(ReportLine.order_id).filter(
        ReportLine.store == ReportOrder.store,
        ReportLine.order_id == ReportOrder.order_id,
    ).exists()
    legacy = db.session.query(ReportOrder.payload).filter(
        ReportOrder.store.in_(STORES), ReportOrder.state == 'OPEN',
        ReportOrder.business_date == today, ~has_lines,
    )
    for (payload,) in legacy.yield_per(500):
        for line in payload.get('line_items', []):
            name = (line.get('name') or 'Unnamed item').strip() or 'Unnamed item'
            quantities[name] += Decimal(line.get('quantity') or '0')

    statuses = {row.store: row for row in ReportSync.query.filter(ReportSync.store.in_(STORES))}
    sync_stores, warnings, checked_through = [], [], []
    for store in STORES:
        row = statuses.get(store)
        watermark = row.watermark if row else None
        if watermark:
            checked_through.append(watermark)
        stale = not watermark or (utc_now - watermark).total_seconds() >= DASHBOARD_SYNC_SECONDS
        if stale or (row and (row.window_end is not None or row.error)):
            sync_stores.append(store)
        if not watermark:
            message = 'First import is incomplete; totals may be partial.'
        elif row.error:
            message = 'The last update failed; showing saved sales.'
        elif row.window_end is not None:
            message = 'An update is in progress; showing sales saved so far.'
        else:
            continue
        warnings.append({'store': store, 'message': message})

    last_checked = min(checked_through) if len(checked_through) == len(STORES) else None
    available = bool(daily or checked_through)
    # MySQL SUM returns Decimal; normalize only the rounded display values.
    money = lambda cents: float(round(cents / 100, 2))
    quantity_label = lambda quantity: format(quantity.normalize(), 'f')
    return {
        'today': today,
        'sales_available': available,
        'sales_checked_through': last_checked.replace(tzinfo=timezone.utc).astimezone(TZ).strftime('%d %b, %I:%M %p') if last_checked else None,
        'sales_sync_stores': sync_stores,
        'sales_sync_warnings': warnings,
        'today_revenue': money(today_cents),
        'today_orders': today_orders,
        'today_items': quantity_label(sum(quantities.values(), Decimal(0))),
        'avg_order_value': money(today_cents / today_orders) if today_orders else 0,
        'store_sales_today': sorted(
            [{'store': store, 'revenue': money(cents)} for store, cents in revenue_by_store.items()],
            key=lambda row: row['revenue'], reverse=True,
        ) if available else [],
        'trend': [{'label': day.strftime('%a'), 'date': day.strftime('%d/%m'), 'revenue': money(cents)}
                  for day, cents in revenue_by_day.items()] if available else [],
        'top_sellers': [{'name': name, 'qty': quantity_label(quantity)}
                        for name, quantity in sorted(quantities.items(), key=lambda item: (-item[1], item[0]))[:5]],
    }
