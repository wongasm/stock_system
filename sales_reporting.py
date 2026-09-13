"""Durable Square reporting, independent of inventory deduction records."""
import os
import secrets
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import requests
from flask import abort, jsonify, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from models import db
from square_helpers import ITEM_CATEGORY_MAP

STORES = ('Doncaster', 'Lonsdale', 'Clayton', 'Glen Waverley')
TZ = ZoneInfo('Australia/Melbourne')
EPOCH = datetime(1970, 1, 1)

class ReportOrder(db.Model):
    __tablename__ = 'sales_report_order'
    store = db.Column(db.String(50), primary_key=True)
    order_id = db.Column(db.String(64), primary_key=True)
    location_id = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False)
    business_date = db.Column(db.Date, nullable=False, index=True)
    state = db.Column(db.String(32), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    total = db.Column(db.BigInteger, nullable=False)
    refunded = db.Column(db.BigInteger, nullable=False, default=0)
    payload = db.Column(db.JSON, nullable=False)
    __table_args__ = (db.Index('ix_report_store_date', 'store', 'business_date'),)

class ReportLine(db.Model):
    __tablename__ = 'sales_report_line'
    store = db.Column(db.String(50), primary_key=True)
    order_id = db.Column(db.String(64), primary_key=True)
    line_id = db.Column(db.String(64), primary_key=True)
    business_date = db.Column(db.Date, nullable=False, index=True)
    category = db.Column(db.String(80), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(16, 4), nullable=False)
    total = db.Column(db.BigInteger, nullable=False)
    __table_args__ = (db.Index('ix_report_line_store_date', 'store', 'business_date'),)

class ReportSync(db.Model):
    __tablename__ = 'sales_report_sync'
    store = db.Column(db.String(50), primary_key=True)
    location_id = db.Column(db.String(64), nullable=False)
    watermark = db.Column(db.DateTime)
    window_start = db.Column(db.DateTime)
    window_end = db.Column(db.DateTime)
    cursor = db.Column(db.Text)
    processed = db.Column(db.Integer, nullable=False, default=0)
    last_success = db.Column(db.DateTime)
    error = db.Column(db.String(255))
    lease = db.Column(db.String(64))
    lease_until = db.Column(db.DateTime)


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')


def fetch_page(store, start, end, cursor):
    key = store.upper().replace(' ', '_')
    token, location = os.getenv(key + '_ACCESS_TOKEN'), os.getenv(key + '_LOCATION_ID')
    if not token or not location:
        raise ValueError('Square credentials or location are missing for this store.')
    body = {'location_ids': [location], 'limit': 100, 'query': {
        'filter': {'date_time_filter': {'updated_at': {'start_at': iso(start), 'end_at': iso(end)}}},
        'sort': {'sort_field': 'UPDATED_AT', 'sort_order': 'ASC'}}}
    if cursor:
        body['cursor'] = cursor
    response = requests.post('https://connect.squareup.com/v2/orders/search', json=body,
        headers={'Authorization': 'Bearer ' + token, 'Square-Version': '2026-08-19'}, timeout=(10, 45))
    if response.status_code != 200:
        raise ValueError('Square sync failed (HTTP %s). Retry sync; saved data is retained.' % response.status_code)
    result = response.json()
    if result.get('errors'):
        raise ValueError('Square rejected this batch. Retry sync or rebuild to restart pagination.')
    return result


def category_for(name, variation=''):
    # Waffle variants from the reference workbook; preserve existing mappings first.
    if 'waffle' in variation.lower():
        return 'Waffles'
    return ITEM_CATEGORY_MAP.get(name, 'Waffles' if name in {
        'Pistachio Crunch', 'Chocolate Fudge', 'Apple Pie', 'Plain Jane', 'Strawberry Jam'
    } or 'waffle' in name.lower() else 'Uncategorized')


def save_order(store, location, order):
    if order.get('location_id') != location:
        raise ValueError('Square returned an unexpected location.')
    updated = parse_time(order['updated_at']).replace(tzinfo=None)
    row = db.session.get(ReportOrder, (store, order['id']))
    if row and row.updated_at > updated:
        return
    if row is None:
        row = ReportOrder(store=store, order_id=order['id'])
        db.session.add(row)
    row.location_id, row.updated_at = location, updated
    row.business_date = parse_time(order['created_at']).astimezone(TZ).date()
    row.state = order['state']
    row.currency = order.get('total_money', {}).get('currency', 'AUD')
    if row.currency != 'AUD':
        raise ValueError('Non-AUD order encountered; reporting requires a separate currency view.')
    row.total = int(order.get('total_money', {}).get('amount', 0))
    row.refunded = sum(int(r.get('amount_money', {}).get('amount', 0))
                       for r in order.get('refunds', []) if r.get('status') == 'COMPLETED')
    # Retain reporting detail without customer/contact/payment identifiers.
    row.payload = {k: order[k] for k in ('id', 'created_at', 'updated_at', 'state',
        'line_items', 'total_money', 'total_tax_money', 'total_discount_money', 'total_tip_money') if k in order}
    ReportLine.query.filter_by(store=store, order_id=row.order_id).delete()
    if row.state == 'COMPLETED':
        for n, line in enumerate(order.get('line_items', [])):
            name = line.get('name', 'Unnamed item').strip() or 'Unnamed item'
            db.session.add(ReportLine(store=store, order_id=row.order_id,
                line_id=line.get('uid') or str(n), business_date=row.business_date,
                category=category_for(name, line.get('variation_name', '')), name=name,
                quantity=Decimal(line.get('quantity', '0')),
                total=int(line.get('total_money', {}).get('amount', 0))))


def sync_step(store, rebuild=False, fetch=fetch_page):
    """One bounded page. Lease + rows + cursor commit protects retries/workers."""
    if store not in STORES:
        raise ValueError('Unknown store.')
    location = os.getenv(store.upper().replace(' ', '_') + '_LOCATION_ID', '')
    if not location:
        raise ValueError('Square location is missing for this store.')
    row = db.session.get(ReportSync, store)
    if row is None:
        db.session.add(ReportSync(store=store, location_id=location))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
    now, token = datetime.utcnow(), secrets.token_hex(16)
    acquired = ReportSync.query.filter(ReportSync.store == store,
        or_(ReportSync.lease_until == None, ReportSync.lease_until < now)).update(
        {'lease': token, 'lease_until': now + timedelta(minutes=3)}, synchronize_session=False)
    db.session.commit()
    if not acquired:
        return {'store': store, 'busy': True, 'more': True}
    try:
        row = db.session.get(ReportSync, store)
        db.session.refresh(row)
        if row.location_id != location:
            raise ValueError('Store location changed. Restore the original location before syncing.')
        starting = rebuild or row.window_end is None
        window_start = (EPOCH if rebuild else max(EPOCH, (row.watermark or EPOCH) - timedelta(hours=1))) if starting else row.window_start
        window_end = now if starting else row.window_end
        cursor = None if starting else row.cursor
        result = fetch(store, window_start, window_end, cursor)
        # Conditional update obtains a write lock, fencing workers with expired leases.
        owned = ReportSync.query.filter(ReportSync.store == store, ReportSync.lease == token,
            ReportSync.lease_until > datetime.utcnow()).update(
                {'lease_until': datetime.utcnow() + timedelta(minutes=3)}, synchronize_session=False)
        if not owned:
            raise ValueError('Sync lease expired. Retry this batch.')
        if starting:
            row.window_start, row.window_end, row.cursor, row.processed = window_start, window_end, None, 0
        for order in result.get('orders', []):
            save_order(store, location, order)
        row.processed += len(result.get('orders', []))
        row.cursor = result.get('cursor')
        more = bool(row.cursor)
        if not more:
            row.watermark = row.window_end
            row.window_start = row.window_end = None
            row.last_success = datetime.utcnow()
        row.error, row.lease, row.lease_until = None, None, None
        db.session.commit()
        return {'store': store, 'more': more, 'processed': row.processed}
    except Exception as exc:
        db.session.rollback()
        message = str(exc) if isinstance(exc, ValueError) else 'Sync interrupted. Retry sync; saved data is retained.'
        ReportSync.query.filter_by(store=store, lease=token).update(
            {'error': message[:255], 'lease': None, 'lease_until': None})
        db.session.commit()
        raise ValueError(message) from None


def report_data(start, end, stores):
    days = (end-start).days + 1
    previous = start - timedelta(days=days)
    orders = ReportOrder.query.filter(ReportOrder.store.in_(stores),
        ReportOrder.state == 'COMPLETED', ReportOrder.business_date.between(previous, end))
    daily = orders.with_entities(ReportOrder.business_date, ReportOrder.store,
        func.sum(ReportOrder.total), func.count(), func.sum(ReportOrder.refunded)).group_by(
        ReportOrder.business_date, ReportOrder.store).all()
    totals = {s: {'sales': 0, 'previous': 0, 'orders': 0, 'refunds': 0} for s in stores}
    series = {s: [0]*days for s in stores}
    day_rows = []
    day_values = {(d,s): amount for d,s,amount,count,refunded in daily}
    for d, s, amount, count, refunded in daily:
        if d < start:
            totals[s]['previous'] += amount
        else:
            totals[s]['sales'] += amount
            totals[s]['orders'] += count
            totals[s]['refunds'] += refunded
            series[s][(d-start).days] = amount / 100
            day_rows.append({'date': d, 'store': s, 'sales': amount/100, 'orders': count, 'average': amount/count/100, 'previous': day_values.get((d-timedelta(days=days),s),0)/100})
    lines = ReportLine.query.filter(ReportLine.store.in_(stores), ReportLine.business_date.between(start, end))
    products = lines.with_entities(ReportLine.category, ReportLine.name,
        func.sum(ReportLine.quantity), func.sum(ReportLine.total)).group_by(
        ReportLine.category, ReportLine.name).order_by(func.sum(ReportLine.total).desc()).all()
    daily_products = lines.with_entities(ReportLine.business_date, ReportLine.store,
        ReportLine.category, ReportLine.name, func.sum(ReportLine.quantity), func.sum(ReportLine.total)).group_by(
        ReportLine.business_date, ReportLine.store, ReportLine.category, ReportLine.name).all()
    mixes = {}
    for d, store, cat, name, qty, amount in daily_products:
        mix = mixes.setdefault((d,store), {'drinks': 0, 'waffles': 0, 'best': '', 'best_qty': 0})
        if cat == 'Drinks': mix['drinks'] += amount
        if cat == 'Waffles': mix['waffles'] += amount
        if cat == 'Bingsu' and qty > mix['best_qty']:
            mix['best'], mix['best_qty'] = name, qty
    for r in day_rows:
        r.update(mixes.get((r['date'],r['store']), {'drinks': 0, 'waffles': 0, 'best': '', 'best_qty': 0}))
    sales = sum(t['sales'] for t in totals.values())
    count = sum(t['orders'] for t in totals.values())
    prior = sum(t['previous'] for t in totals.values())
    categories = {}
    for cat, name, qty, amount in products:
        c = categories.setdefault(cat, {'sales': 0, 'quantity': Decimal(0)})
        c['sales'] += amount
        c['quantity'] += qty
    return dict(totals=totals, sales=sales, order_count=count, previous_sales=prior,
        average=sales/count if count else 0, categories=categories, products=products,
        item_count=sum((p[2] for p in products), Decimal(0)), day_rows=sorted(day_rows,key=lambda r:(r['date'],r['store'])),
        chart={'labels': [(start+timedelta(days=i)).isoformat() for i in range(days)], 'series': series},
        comparison_start=previous, comparison_end=start-timedelta(days=1))


def register_sales_reporting(app):
    def admin():
        if current_user.role != 'admin':
            abort(403)

    @login_required
    def page():
        admin()
        today = datetime.now(TZ).date()
        try:
            start = date.fromisoformat(request.args.get('start_date') or (today-timedelta(days=today.weekday())).isoformat())
            end = date.fromisoformat(request.args.get('end_date') or today.isoformat())
            if end < start or (end-start).days > 730:
                raise ValueError()
            page_num = max(1, int(request.args.get('page', 1)))
        except ValueError:
            abort(400, 'Choose valid dates in order, spanning no more than two years.')
        store = request.args.get('store_filter', '')
        if store and store not in STORES:
            abort(400, 'Unknown store.')
        selected = [store] if store else list(STORES)
        session.setdefault('sales_csrf', secrets.token_hex(32))
        data = report_data(start, end, selected)
        transactions = ReportOrder.query.filter(ReportOrder.store.in_(selected),
            ReportOrder.business_date.between(start,end)).order_by(
            ReportOrder.business_date.desc(),ReportOrder.store,ReportOrder.order_id).paginate(page=page_num,per_page=50,error_out=False)
        statuses = {r.store: r for r in ReportSync.query.all()}
        return render_template('sales_report.html', **data, start_date=start.isoformat(), end_date=end.isoformat(),
            store_filter=store, stores=STORES, statuses=statuses, transactions=transactions,
            csrf=session['sales_csrf'])

    # Preserve the existing endpoint and all navigation links.
    app.add_url_rule('/sales_report', endpoint='sales_report', view_func=page, methods=['GET'])

    @app.route('/sales_report/sync', methods=['POST'])
    @login_required
    def sales_report_sync():
        admin()
        if not session.get('sales_csrf') or not secrets.compare_digest(request.headers.get('X-Sales-CSRF', ''), session['sales_csrf']):
            abort(403)
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(sync_step(body.get('store'), rebuild=body.get('rebuild') is True))
        except ValueError as exc:
            return jsonify(error=str(exc)), 503
