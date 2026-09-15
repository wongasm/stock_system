from copy import deepcopy
from datetime import date, datetime, timedelta
import os
import pytest
from flask import Flask, g
from flask_login import LoginManager, UserMixin
from models import db
from sales_reporting import (ReportOrder, ReportLine, ReportSync, save_order,
    sync_step, report_data, register_sales_reporting)

@pytest.fixture
def app(monkeypatch):
    a=Flask(__name__)
    a.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',SECRET_KEY='test-only',TESTING=True)
    db.init_app(a)
    lm=LoginManager(a)
    class User(UserMixin):
        id='1'
        role='admin'
    @lm.user_loader
    def load(uid):
        u=User()
        if uid=='2': u.role='staff'
        return u
    import re
    from pathlib import Path
    for endpoint in set(re.findall(r"url_for\('([^']+)'", Path(__file__).with_name('templates').joinpath('navbar.html').read_text())) - {'static','sales_report'}:
        a.add_url_rule('/test/'+endpoint, endpoint, lambda: '')
    register_sales_reporting(a)
    monkeypatch.setenv('DONCASTER_LOCATION_ID','loc')
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()

@pytest.fixture
def order():
    return {'id':'order-1','location_id':'loc','state':'COMPLETED',
        'created_at':'2026-09-01T15:00:00Z','updated_at':'2026-09-02T01:00:00Z',
        'total_money':{'amount':2500,'currency':'AUD'},
        'line_items':[{'uid':'a','name':'Mango','quantity':'1.5','total_money':{'amount':2500}}]}

def test_idempotent_changed_canceled(app,order):
    save_order('Doncaster','loc',order); db.session.commit()
    save_order('Doncaster','loc',order); db.session.commit()
    assert ReportOrder.query.count()==ReportLine.query.count()==1
    assert ReportOrder.query.first().business_date==date(2026,9,2)
    order['updated_at']='2026-09-03T01:00:00Z'
    order['total_money']['amount']=3000
    order['line_items'][0]['total_money']['amount']=3000
    save_order('Doncaster','loc',order);db.session.commit()
    assert report_data(date(2026,9,2),date(2026,9,2),['Doncaster'])['sales']==3000
    order['state']='CANCELED';order['updated_at']='2026-09-04T01:00:00Z'
    save_order('Doncaster','loc',order);db.session.commit()
    assert ReportLine.query.count()==0
    assert report_data(date(2026,9,2),date(2026,9,2),['Doncaster'])['sales']==0

def test_cursor_failure_resume_incremental(app,order):
    calls=[]
    def fetch(s,start,end,cursor):
        calls.append((start,end,cursor))
        return {'orders':[order],'cursor':'next'}
    assert sync_step('Doncaster',fetch=fetch)['more']
    state=ReportSync.query.first()
    assert state.watermark is None and state.cursor=='next'
    def fail(*args): raise RuntimeError('private credential')
    with pytest.raises(ValueError,match='interrupted'):
        sync_step('Doncaster',fetch=fail)
    assert ReportOrder.query.count()==1
    assert ReportSync.query.first().cursor=='next'
    def finish(s,start,end,cursor):
        assert cursor=='next' and end==calls[0][1]
        return {'orders':[order]}
    assert not sync_step('Doncaster',fetch=finish)['more']
    watermark=ReportSync.query.first().watermark
    def incremental(s,start,end,cursor):
        assert start==watermark-timedelta(hours=1)
        assert cursor is None
        return {'orders':[]}
    sync_step('Doncaster',fetch=incremental)
    assert ReportOrder.query.count()==1

def test_rollback_whole_page(app,order):
    bad=deepcopy(order);bad['id']='bad';bad['location_id']='wrong'
    with pytest.raises(ValueError,match='unexpected location'):
        sync_step('Doncaster',fetch=lambda *a:{'orders':[order,bad]})
    assert ReportOrder.query.count()==0
    assert ReportSync.query.first().watermark is None

def test_rebuild_preserves_records_and_busy(app,order):
    sync_step('Doncaster',fetch=lambda *a:{'orders':[order]})
    sync_step('Doncaster',rebuild=True,fetch=lambda *a:{'orders':[],'cursor':'page2'})
    assert ReportOrder.query.count()==1
    row=ReportSync.query.first(); row.lease_until=datetime.utcnow()+timedelta(minutes=1);db.session.commit()
    assert sync_step('Doncaster',fetch=lambda *a:pytest.fail('must not fetch'))['busy']

def test_routes_access_validation_no_fetch(app,order):
    c=app.test_client()
    assert c.get('/sales_report').status_code==401
    with c.session_transaction() as s:s['_user_id']='2';s['_fresh']=True
    g.pop('_login_user', None)
    assert c.get('/sales_report').status_code==403
    with c.session_transaction() as s:s['_user_id']='1'
    g.pop('_login_user', None)
    assert c.get('/sales_report?start_date=bad').status_code==400
    assert c.get('/sales_report?start_date=2026-09-02&end_date=2026-09-01').status_code==400
    assert c.post('/sales_report/sync',json={'store':'Doncaster'}).status_code==403
    save_order('Doncaster','loc',order);db.session.commit()
    response=c.get('/sales_report?start_date=2026-09-02&end_date=2026-09-02')
    assert response.status_code==200
    assert b'$25.00' in response.data and b'Mango' in response.data

def test_previous_period_and_refunds(app,order):
    save_order('Doncaster','loc',order)
    previous=deepcopy(order);previous['id']='old';previous['created_at']='2026-09-01T01:00:00Z'
    previous['total_money']['amount']=1000
    save_order('Doncaster','loc',previous);db.session.commit()
    data=report_data(date(2026,9,2),date(2026,9,2),['Doncaster'])
    assert data['sales']==2500 and data['previous_sales']==1000
    assert data['item_count']==1.5


def test_expired_worker_cannot_commit(app,order):
    def stolen(*args):
        row=ReportSync.query.first()
        row.lease='new-owner'
        db.session.commit()
        return {'orders':[order]}
    with pytest.raises(ValueError,match='lease expired'):
        sync_step('Doncaster',fetch=stolen)
    assert ReportOrder.query.count()==0
    assert ReportSync.query.first().lease=='new-owner'


def test_stale_order_cannot_replace_newer(app,order):
    newer=deepcopy(order);newer['updated_at']='2026-09-05T00:00:00Z'
    newer['total_money']['amount']=4000
    save_order('Doncaster','loc',newer);db.session.commit()
    save_order('Doncaster','loc',order);db.session.commit()
    assert ReportOrder.query.first().total==4000


def test_square_query_and_failure(monkeypatch):
    import sales_reporting as sr
    monkeypatch.setenv('DONCASTER_ACCESS_TOKEN','test-secret')
    monkeypatch.setenv('DONCASTER_LOCATION_ID','loc')
    class Response:
        status_code=200
        def json(self):return {'orders':[]}
    def post(url,**kwargs):
        body=kwargs['json']
        assert body['query']['sort']['sort_field']=='UPDATED_AT'
        assert 'state_filter' not in body['query']['filter']
        assert body['cursor']=='next'
        assert body['limit']==100
        return Response()
    monkeypatch.setattr(sr.requests,'post',post)
    sr.fetch_page('Doncaster',datetime(2020,1,1),datetime(2026,1,1),'next')
    Response.status_code=429
    with pytest.raises(ValueError,match='HTTP 429'):
        sr.fetch_page('Doncaster',datetime(2020,1,1),datetime(2026,1,1),'next')


def test_cursor_bounds_survive_mysql_datetime_precision(app, order):
    bounds = []
    def fetch(store, start, end, cursor):
        bounds.append((start, end))
        return {'orders': [order], 'cursor': 'next'} if cursor is None else {'orders': []}
    sync_step('Doncaster', fetch=fetch)
    # Simulate round-tripping the default MySQL DATETIME columns.
    row = ReportSync.query.first()
    row.window_start = row.window_start.replace(microsecond=0)
    row.window_end = row.window_end.replace(microsecond=0)
    db.session.commit()
    sync_step('Doncaster', fetch=fetch)
    assert bounds[0] == bounds[1]
    assert ReportSync.query.first().watermark == bounds[0][1]


def test_store_totals_include_all_pages_and_obey_dates(app, order):
    from sales_reporting import saved_coverage
    for store, count in [('Doncaster', 251), ('Lonsdale', 163)]:
        for i in range(count):
            item = deepcopy(order)
            item['id'] = str(i)
            save_order(store, 'loc', item)
    outside = deepcopy(order)
    outside['id'] = 'outside'
    outside['created_at'] = '2026-09-03T14:00:00Z'
    save_order('Doncaster', 'loc', outside)
    opened = deepcopy(order)
    opened['id'] = 'open'
    opened['state'] = 'OPEN'
    save_order('Doncaster', 'loc', opened)
    db.session.commit()
    start = end = date(2026, 9, 2)
    data = report_data(start, end, ['Doncaster', 'Lonsdale'])
    assert data['totals']['Doncaster']['sales'] == 252 * 2500
    assert data['totals']['Lonsdale']['sales'] == 163 * 2500
    assert data['sales'] == (252 + 163) * 2500
    assert report_data(date(2026, 9, 4), date(2026, 9, 4), ['Doncaster'])['sales'] == 2500
    coverage = saved_coverage(start, end, ['Doncaster'])['Doncaster']
    assert coverage['count'] == 253
    assert coverage['first'] == date(2026, 9, 2)
    assert coverage['last'] == date(2026, 9, 4)
    assert {s['state']: s['count'] for s in coverage['states']} == {'COMPLETED': 251, 'OPEN': 1}


def test_open_sales_legacy_details_and_status_transitions(app, order):
    for state in ('OPEN', 'COMPLETED', 'DRAFT', 'CANCELED'):
        item = deepcopy(order)
        item.update(id=state, state=state)
        save_order('Doncaster', 'loc', item)
    prior = deepcopy(order)
    prior.update(id='prior', state='OPEN', created_at='2026-09-01T01:00:00Z')
    save_order('Doncaster', 'loc', prior)
    # Simulate an OPEN order saved before this change, with no projected lines.
    ReportLine.query.filter_by(store='Doncaster', order_id='OPEN').delete()
    db.session.commit()
    def check(expected_orders):
        data = report_data(date(2026, 9, 2), date(2026, 9, 2), ['Doncaster'])
        assert data['order_count'] == expected_orders
        assert data['sales'] == expected_orders * 2500
        assert data['previous_sales'] == 2500
        assert data['item_count'] == Decimal('1.5') * expected_orders
        assert data['products'][0][3] == expected_orders * 2500
        assert data['day_rows'][0]['best_qty'] == Decimal('1.5') * expected_orders
        assert data['chart']['series']['Doncaster'] == [expected_orders * 25]
    from decimal import Decimal
    check(2)
    item = deepcopy(order)
    item.update(id='OPEN', state='OPEN')
    save_order('Doncaster', 'loc', item)
    db.session.commit()
    check(2)  # Re-syncing projects lines without counting them twice.
    item.update(state='COMPLETED', updated_at='2026-09-03T00:00:00Z')
    save_order('Doncaster', 'loc', item)
    db.session.commit()
    check(2)
    item.update(state='DRAFT', updated_at='2026-09-04T00:00:00Z')
    save_order('Doncaster', 'loc', item)
    db.session.commit()
    check(1)


def test_page_explains_open_inclusion(app, order):
    order['state'] = 'OPEN'
    save_order('Doncaster', 'loc', order)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as session:
        session['_user_id'] = '1'
    response = c.get('/sales_report?start_date=2026-09-02&end_date=2026-09-02')
    assert response.status_code == 200
    assert b'OPEN: 1 orders' in response.data
    assert b'(excluded from sales)' not in response.data
    assert b'Open and completed orders are included' in response.data


def test_week_archive_lazy_details_and_boundaries(app, order, monkeypatch):
    import sales_reporting as sr
    for oid, created, state, store in [
        ('sunday','2026-09-06T13:59:59Z','OPEN','Doncaster'),
        ('monday','2026-09-06T14:00:00Z','COMPLETED','Doncaster'),
        ('draft','2026-09-06T14:00:00Z','DRAFT','Doncaster'),
        ('other','2026-09-06T14:00:00Z','OPEN','Lonsdale')]:
        item=deepcopy(order);item.update(id=oid,created_at=created,state=state)
        save_order(store,'loc',item)
    db.session.commit()
    weeks=sr.saved_weeks(['Doncaster'])
    assert [w['start'] for w in weeks]==[date(2026,9,7),date(2026,8,31)]
    assert [w['sales'] for w in weeks]==[2500,2500]
    c=app.test_client()
    assert c.get('/sales_report/week?week=2026-09-07').status_code==401
    with c.session_transaction() as s:s['_user_id']='2'
    g.pop('_login_user',None)
    assert c.get('/sales_report/week?week=2026-09-07').status_code==403
    with c.session_transaction() as s:s['_user_id']='1'
    g.pop('_login_user',None)
    for query in ['week=bad','week=2026-09-08','week=2026-09-07&store_filter=bad','week=9999-12-27']:
        assert c.get('/sales_report/week?'+query).status_code==400
    monkeypatch.setattr(sr.requests,'post',lambda *a,**kw:pytest.fail('Saved reports must not fetch Square'))
    response=c.get('/sales_report/week?week=2026-09-07&store_filter=Doncaster')
    assert response.status_code==200
    assert b'Mon 07 Sep' in response.data and b'Sun 13 Sep' in response.data
    assert b'Mango' in response.data and b'$25.00' in response.data
    assert b'Lonsdale' not in response.data
    assert b'No saved orders' in response.data
    assert response.headers['Cache-Control']=='no-store'
    calls=[]
    real=sr.report_data
    def track(start,end,stores):
        calls.append((start,end));return real(start,end,stores)
    monkeypatch.setattr(sr,'report_data',track)
    response=c.get('/sales_report?start_date=2026-09-07&end_date=2026-09-07&store_filter=Doncaster')
    assert response.status_code==200
    assert b'Transactions' not in response.data and b'Transaction pages' not in response.data
    assert b'31 Aug 2026' in response.data  # Archive includes history outside top date filter.
    assert b'Weekly product breakdown' not in response.data  # Details only fetched on expansion.
    assert len(calls)==1
