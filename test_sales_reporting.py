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
