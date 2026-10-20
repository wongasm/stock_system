"""Dashboard reads, incremental updates, and login without live Square access."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from werkzeug.security import generate_password_hash

from dashboard_data import saved_dashboard_sales
from models import ApiCache, StockOutRecord, User, db
from sales_reporting import ReportLine, ReportOrder, ReportSync, STORES, TZ, save_order


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'dashboard-test-secret')
    monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', 'sqlite://')
    monkeypatch.setenv('SESSION_COOKIE_SECURE', 'false')
    main = importlib.import_module('app')
    main.app.config.update(TESTING=True)
    def no_network(*args, **kwargs):
        pytest.fail('Dashboard/login must never request live Square data')
    monkeypatch.setattr('requests.sessions.Session.request', no_network)
    monkeypatch.setattr(main, 'fetch_sales_for_store', no_network)
    monkeypatch.setattr(main, 'fetch_loyalty_summary', no_network)
    monkeypatch.setattr(main, 'fetch_loyalty_accounts', no_network)
    monkeypatch.setattr(main, 'fetch_loyalty_report', no_network)
    with main.app.app_context():
        db.create_all()
        db.session.add_all([
            User(id=1, username='admin', role='admin', password_hash=generate_password_hash('test-password', method='pbkdf2:sha256:1000')),
            User(id=2, username='staff', role='user', password_hash='unused'),
        ])
        db.session.commit()
    yield main
    with main.app.app_context():
        db.session.remove()
        db.drop_all()


def admin_client(web):
    client = web.app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = '1'
        session['_fresh'] = True
    return client


def order(identifier='a', created='2026-10-18T13:30:00Z', cents=2500, state='COMPLETED', quantity='1.5'):
    return {'id': identifier, 'location_id': 'loc', 'state': state,
            'created_at': created, 'updated_at': '2026-10-19T00:00:00Z',
            'total_money': {'amount': cents, 'currency': 'AUD'},
            'line_items': [{'uid': 'line', 'name': 'Mango', 'quantity': quantity,
                            'total_money': {'amount': cents}}]}


def test_saved_totals_melbourne_dates_open_legacy_and_cancellations(web):
    now = datetime(2026, 10, 18, 14, tzinfo=timezone.utc)  # Monday in Melbourne
    with web.app.app_context():
        save_order('Doncaster', 'loc', order())
        save_order('Doncaster', 'loc', order('yesterday', '2026-10-18T12:30:00Z', cents=1000))
        save_order('Lonsdale', 'loc', order('open', cents=500, state='OPEN', quantity='2'))
        ReportLine.query.filter_by(order_id='open').delete()  # Legacy saved OPEN payload
        save_order('Clayton', 'loc', order('cancelled', cents=9000, state='CANCELED'))
        save_order('Clayton', 'loc', order('draft', cents=9000, state='DRAFT'))
        save_order('Clayton', 'loc', order('old', '2026-09-01T00:00:00Z', cents=8000))
        db.session.commit()
        result = saved_dashboard_sales(now)
        assert result['today'] == date(2026, 10, 19)
        assert result['today_revenue'] == 30
        assert result['today_orders'] == 2
        assert result['today_items'] == '3.5'
        assert result['avg_order_value'] == 15
        assert result['top_sellers'] == [{'name': 'Mango', 'qty': '3.5'}]
        assert sum(row['revenue'] for row in result['trend']) == 40
        assert len(result['sales_sync_warnings']) == 4


def test_incremental_sync_updates_dashboard_without_duplicates(web, monkeypatch):
    import sales_reporting
    monkeypatch.setenv('DONCASTER_LOCATION_ID', 'loc')
    current = order(created=datetime.now(timezone.utc).isoformat())
    with web.app.app_context():
        def first_page(*args):
            return {'orders': [current]}
        sales_reporting.sync_step('Doncaster', fetch=first_page)
        checkpoint = db.session.get(ReportSync, 'Doncaster').watermark
        changed = deepcopy(current)
        changed['updated_at'] = datetime.now(timezone.utc).isoformat()
        changed['total_money']['amount'] = 5000
        changed['line_items'][0]['quantity'] = '3'
        def next_page(store, start, end, cursor):
            assert start == checkpoint - timedelta(hours=1)
            assert cursor is None
            return {'orders': [changed, changed]}
        sales_reporting.sync_step('Doncaster', fetch=next_page)
        assert ReportOrder.query.count() == 1
        result = saved_dashboard_sales()
        assert result['today_revenue'] == 50
        assert result['today_items'] == '3'
        changed['state'] = 'CANCELED'
        changed['updated_at'] = datetime.now(timezone.utc).isoformat()
        save_order('Doncaster', 'loc', changed)
        db.session.commit()
        assert saved_dashboard_sales()['today_revenue'] == 0


def test_small_date_bounded_queries_and_sync_freshness(web):
    now = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)
    with web.app.app_context():
        for store in STORES:
            db.session.add(ReportSync(store=store, location_id='loc', watermark=now.replace(tzinfo=None), last_success=now.replace(tzinfo=None)))
        db.session.commit()
        queries = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                queries.append((statement, parameters))
        event.listen(db.engine, 'before_cursor_execute', capture)
        try:
            result = saved_dashboard_sales(now)
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
        assert len(queries) == 4
        assert 'payload' not in queries[0][0]
        assert 'GROUP BY' in queries[0][0]
        assert 'sales_report_line.business_date = ' in queries[1][0]
        assert 'sales_report_order.business_date = ' in queries[2][0]
        assert result['sales_sync_stores'] == []
        assert result['sales_sync_warnings'] == []
        assert result['sales_available'] is True  # A synced zero is a real zero.
        assert saved_dashboard_sales(now + timedelta(minutes=6))['sales_sync_stores'] == list(STORES)


@pytest.mark.parametrize('path', ['/dashboard', '/dashboard?refresh=1', '/dashboard/summary'])
def test_cold_dashboard_and_old_refresh_urls_never_fetch_square(web, path):
    response = admin_client(web).get(path)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'First import is incomplete' in response.text
    assert 'data-dashboard-refresh' in response.text
    assert 'Loyalty figures will appear after the scheduled update' in response.text
    assert '<div class="kpi-value">—</div>' in response.text
    assert ('<html' in response.text) == (path != '/dashboard/summary')


def test_old_loyalty_snapshot_retained_and_labeled(web, monkeypatch):
    old_week = (datetime.now(TZ).date() - timedelta(days=14)).isoformat()
    loyalty = {'ok': True, 'program_status': 'ACTIVE', 'points_name': 'Bucks',
               'points_issued': 20, 'rewards_redeemed': 2, 'total_members': 300, 'new_members': 4}
    with web.app.app_context():
        db.session.add(ApiCache(cache_key='dashboard_loyalty_v1',
            payload=json.dumps({'week_start': old_week, 'loyalty': loyalty}),
            fetched_at=datetime.utcnow() - timedelta(days=2)))
        db.session.commit()
        web.cache_set('unrelated', {'ok': True})
        assert web.cache_read('dashboard_loyalty_v1')[0]['loyalty'] == loyalty
        web.cache_set('loyalty_accounts_v4', {'ok': True, 'accounts': []})
        monkeypatch.setattr(web, 'fetch_loyalty_summary', lambda *a, **kw: {'ok': False})
        monkeypatch.setattr(web, 'fetch_loyalty_report', lambda *a, **kw: {'ok': False})
        monkeypatch.setattr(web, 'get_customer_directory', lambda **kw: {})
        assert web.warm_caches()['dashboard'] is False
        assert web.cache_read('dashboard_loyalty_v1')[0]['loyalty'] == loyalty
    response = admin_client(web).get('/dashboard')
    assert response.status_code == 200
    assert f'Week of {old_week}' in response.text
    assert "This week's figures have not been saved yet" in response.text


def test_background_warmer_reuses_even_an_empty_member_snapshot(web, monkeypatch):
    calls = []
    def summary(*args, **kwargs):
        assert kwargs['accounts'] == []
        calls.append('summary')
        return {'ok': True}
    def report(*args, **kwargs):
        assert kwargs['accounts'] == []
        calls.append('report')
        return {'ok': True}
    monkeypatch.setattr(web, 'fetch_loyalty_summary', summary)
    monkeypatch.setattr(web, 'fetch_loyalty_report', report)
    monkeypatch.setattr(web, 'get_customer_directory', lambda **kw: {})
    with web.app.app_context():
        web.cache_set('loyalty_accounts_v4', {'ok': True, 'accounts': []})
        assert web.warm_caches()['dashboard'] is True
        assert calls == ['summary', 'report']


@pytest.mark.parametrize('event_ok', [True, False])
def test_loyalty_summary_does_not_rescan_members_or_cache_partial_success(web, monkeypatch, event_ok):
    import square_api
    def result(body, ok=True):
        return SimpleNamespace(body=body, errors=[], is_success=lambda: ok)
    loyalty = SimpleNamespace(
        retrieve_loyalty_program=lambda **kw: result({'program': {'status': 'ACTIVE'}}),
        search_loyalty_events=lambda **kw: result({'events': [{'type': 'ACCUMULATE_POINTS', 'accumulate_points': {'points': 5}}]}, event_ok),
        search_loyalty_accounts=lambda **kw: pytest.fail('Must reuse saved accounts'),
    )
    monkeypatch.setattr(square_api, '_first_loyalty_token', lambda: 'test-token')
    monkeypatch.setattr(square_api, 'Client', lambda **kw: SimpleNamespace(loyalty=loyalty))
    data = square_api.fetch_loyalty_summary('start', 'end', week_start=date(2026, 10, 19),
        accounts=[{'created_at': '2026-10-18T13:30:00Z'}])
    assert data['ok'] is event_ok
    if event_ok:
        assert data['total_members'] == data['new_members'] == 1
        assert data['points_issued'] == 5


def test_dashboard_access_and_sync_csrf(web):
    client = web.app.test_client()
    assert client.get('/dashboard/summary').status_code == 302
    with client.session_transaction() as session:
        session['_user_id'] = '2'
    response = client.get('/dashboard/summary')
    assert response.status_code == 302
    assert 'Today’s Sales' not in response.text
    client = admin_client(web)
    client.get('/dashboard')
    assert client.post('/sales_report/sync', json={'store': 'Doncaster'}).status_code == 403


def test_login_shows_errors_and_success_renders_saved_dashboard(web):
    client = web.app.test_client()
    login = client.get('/login')
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', login.text).group(1)
    response = client.post('/login', data={'username': 'admin', 'password': 'wrong-password', 'csrf_token': token})
    assert 'Invalid username or password' in response.text
    response = client.post('/login', data={'username': 'admin', 'password': 'test-password'})
    assert 'CSRF token is missing' in response.text
    response = client.post('/login', data={'username': 'admin', 'password': 'test-password', 'csrf_token': token}, follow_redirects=True)
    assert response.status_code == 200
    assert response.request.path == '/dashboard'
    assert 'Saved sales' in response.text


def test_invoice_aggregates_preserve_totals(web):
    with web.app.app_context():
        for invoice, quantity, price, paid in [(10, '2', '5', False), (10, '1', '7', False), (11, '3', '4', True)]:
            db.session.add(StockOutRecord(invoice_no=invoice, date=datetime.now(TZ).date().isoformat(),
                store='Doncaster', item='Test', price=1, quantity=quantity, selling_price=price, paid=paid))
        db.session.commit()
    response = admin_client(web).get('/dashboard')
    assert '$18.70 unpaid' in response.text
    assert '1 invoice outstanding' in response.text
    assert '$31.90' in response.text


def test_invoice_aggregates_preserve_fractional_units_and_full_current_week(web):
    today = datetime.now(TZ).date()
    sunday = today + timedelta(days=6-today.weekday())
    with web.app.app_context():
        db.session.add(StockOutRecord(invoice_no=1, date=today.isoformat(), store='Doncaster',
            item='Fractional quantity', price=1, quantity='1.2', selling_price='1.23', paid=False))
        db.session.add(StockOutRecord(invoice_no=2, date=sunday.isoformat(), store='Doncaster',
            item='Later this week', price=1, quantity='2', selling_price='5', paid=True))
        db.session.commit()
    response = admin_client(web).get('/dashboard')
    assert '$1.62 unpaid' in response.text  # Round after GST, not before.
    assert '$12.62' in response.text


def test_scheduled_sync_gives_each_store_a_turn(web, monkeypatch):
    worker = importlib.import_module('refresh_cache')
    calls = []
    def sync(store):
        calls.append(store)
        return {'more': store == 'Doncaster'}
    monkeypatch.setattr(worker, 'sync_step', sync)
    monkeypatch.setattr(worker, 'warm_caches', lambda: {'dashboard': True})
    worker.run_once()
    assert calls[:4] == list(STORES)
    assert calls.count('Doncaster') == 10
    assert len(calls) == 13


@pytest.mark.parametrize('already_wide', [True, False])
def test_startup_only_widens_cache_column_when_needed(web, monkeypatch, already_wide):
    from sqlalchemy.dialects.mysql import LONGTEXT, TEXT
    with web.app.app_context(), monkeypatch.context() as patch:
        patch.setattr(db.engine.dialect, 'name', 'mysql')
        patch.setattr(web, 'inspect', lambda engine: SimpleNamespace(get_columns=lambda table: [
            {'name': 'payload', 'type': LONGTEXT() if already_wide else TEXT()},
        ]))
        statements = []
        patch.setattr(db.session, 'execute', lambda statement: statements.append(str(statement)))
        patch.setattr(db.session, 'commit', lambda: None)
        web.ensure_api_cache_schema()
        assert len(statements) == (0 if already_wide else 1)
