from models import db, Ingredient, Supplier, Category, StockOutRecord, Recipe, RecipeIngredient, User, Stocktake, StoreThreshold, StoreWeeklyItem, MonthlyStocktake, Invoice, WeeklyStocktake, StockInRecord, StoreInventory, SquareOrder, SquareOrderLine, SquareItemRecipe, InventoryLedger, SalesRecipe, SalesRecipeIngredient, SquareItemSalesRecipe
from datetime import datetime, timedelta, timezone
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify, send_file, session
import json
from fpdf import FPDF
import os
from math import ceil
from decimal import Decimal, InvalidOperation
import csv
from io import BytesIO, StringIO
from itertools import groupby
from collections import defaultdict
from sqlalchemy.sql import text, func
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from forms import LoginForm
from flask_migrate import Migrate
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.exc import SQLAlchemyError, OperationalError, IntegrityError
from sqlalchemy import cast, Numeric, and_, inspect
from flask_mail import Mail, Message
from dotenv import load_dotenv
from square_api import fetch_sales_for_store, fetch_loyalty_summary, fetch_loyalty_report
from pathlib import Path
from square_helpers import ITEM_CATEGORY_MAP
from freezer_pack_helpers import calculate_ingredients_for_freezer_pack
import traceback
import re
from difflib import SequenceMatcher

# ⛑ Force .env from current file directory
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

load_dotenv()

print("✅ TEST: .env ACCESS")
print("DONCASTER_ACCESS_TOKEN:", os.getenv("DONCASTER_ACCESS_TOKEN"))
print("GLEN_WAVERLEY_ACCESS_TOKEN:", os.getenv("GLEN_WAVERLEY_ACCESS_TOKEN"))

app = Flask(__name__)
app.secret_key = "hellohello.1"

# Database Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = "mysql://wongasm2:Hellohello.1@wongasm2.mysql.pythonanywhere-services.com/wongasm2$stock_system"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 280
}


# Initialize database
db.init_app(app)
migrate = Migrate(app, db)

app.config["MAIL_SERVER"] = "smtp.gmail.com"  # Change for other providers (Outlook, Yahoo, etc.)
app.config["MAIL_PORT"] = 587  # Common for TLS
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USE_SSL"] = False
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME", os.getenv("SMTP_USERNAME", "binginvoice@gmail.com"))  # Prefer MAIL_* but support legacy SMTP_*
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD", os.getenv("SMTP_PASSWORD"))  # Prefer MAIL_* but support legacy SMTP_*
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", app.config["MAIL_USERNAME"])  # Default "From" email

mail = Mail(app)

print(f"🔍 Flask Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def parse_square_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None

def normalize_label(value):
    if not value:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(value).lower())
    return " ".join(cleaned.split())

def best_recipe_match(item_name, recipe_choices):
    needle = normalize_label(item_name)
    if not needle:
        return None, 0.0
    best_id = None
    best_ratio = 0.0
    for recipe_id, recipe_norm in recipe_choices:
        if not recipe_norm:
            continue
        ratio = SequenceMatcher(None, needle, recipe_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = recipe_id
    return best_id, best_ratio


def ensure_store_weekly_item_schema():
    column_names = {
        column["name"]
        for column in inspect(db.engine).get_columns("store_weekly_item")
    }

    if "section_name" not in column_names:
        try:
            db.session.execute(
                text("ALTER TABLE store_weekly_item ADD COLUMN section_name VARCHAR(255) NULL")
            )
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()


def ensure_ingredient_schema():
    column_names = {
        column["name"]
        for column in inspect(db.engine).get_columns("ingredient")
    }

    if "daily_section_name" not in column_names:
        try:
            db.session.execute(
                text("ALTER TABLE ingredient ADD COLUMN daily_section_name VARCHAR(255) NULL")
            )
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()

    if "weekly_section_name" not in column_names:
        try:
            db.session.execute(
                text("ALTER TABLE ingredient ADD COLUMN weekly_section_name VARCHAR(255) NULL")
            )
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()


def build_weekly_section_entries(store_items, ingredient_map):
    entries = []
    last_section = None

    for store_item in store_items:
        ingredient = ingredient_map.get(store_item.ingredient_id)
        if not ingredient:
            continue

        section_name = (store_item.section_name or "").strip() or None
        if section_name and section_name != last_section:
            entries.append({"type": "section", "name": section_name})

        entries.append({
            "type": "ingredient",
            "ingredient": ingredient,
            "section_name": section_name
        })
        last_section = section_name

    return entries


def build_default_weekly_section_entries(ingredients):
    entries = []
    last_section = None

    for ingredient in ingredients:
        section_name = (ingredient.weekly_section_name or "").strip() or None
        if section_name and section_name != last_section:
            entries.append({"type": "section", "name": section_name})

        entries.append({
            "type": "ingredient",
            "ingredient": ingredient,
            "section_name": section_name
        })
        last_section = section_name

    return entries


def build_default_daily_section_entries(ingredients):
    entries = []
    last_section = None

    for ingredient in ingredients:
        section_name = (ingredient.daily_section_name or "").strip() or None
        if section_name and section_name != last_section:
            entries.append({"type": "section", "name": section_name})

        entries.append({
            "type": "ingredient",
            "ingredient": ingredient,
            "section_name": section_name
        })
        last_section = section_name

    return entries


def parse_weekly_enabled_entries(enabled_entries, order_data):
    enabled_ids = []
    section_by_ingredient = {}

    if enabled_entries is not None:
        current_section = None
        seen_enabled = set()

        for entry in enabled_entries:
            if not isinstance(entry, dict):
                continue

            entry_type = entry.get("type")
            if entry_type == "section":
                raw_name = (entry.get("name") or "").strip()
                current_section = raw_name or None
                continue

            if entry_type != "ingredient":
                continue

            try:
                ingredient_id = int(entry.get("id"))
            except (TypeError, ValueError):
                continue

            if ingredient_id in seen_enabled:
                continue

            seen_enabled.add(ingredient_id)
            enabled_ids.append(ingredient_id)
            section_by_ingredient[ingredient_id] = current_section
    else:
        enabled_ids = [int(item_id) for item_id in (order_data or [])]
        section_by_ingredient = {
            ingredient_id: None
            for ingredient_id in enabled_ids
        }

    return enabled_ids, section_by_ingredient


def get_square_line_item_revenue(item):
    total_money = item.get("total_money", {}) or {}
    total_amount = total_money.get("amount")
    if total_amount is not None:
        return float(total_amount) / 100

    quantity = Decimal(str(item.get("quantity", 0) or 0))
    base_amount = Decimal(str((item.get("base_price_money", {}) or {}).get("amount", 0) or 0))
    modifier_amount = Decimal("0")

    for modifier in item.get("modifiers", []) or []:
        modifier_total = (modifier.get("total_price_money", {}) or {}).get("amount")
        if modifier_total is not None:
            modifier_amount += Decimal(str(modifier_total))
            continue

        modifier_base = (modifier.get("base_price_money", {}) or {}).get("amount", 0) or 0
        modifier_quantity = Decimal(str(modifier.get("quantity", quantity) or quantity))
        modifier_amount += Decimal(str(modifier_base)) * modifier_quantity

    return float((base_amount * quantity + modifier_amount) / Decimal("100"))

def apply_square_mappings(store_name, start_dt=None, end_dt=None):
    lock_name = f"square_apply_{store_name}"
    lock_ok = db.session.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}).scalar()
    if lock_ok != 1:
        return {"created": 0, "unmapped": [], "skipped": True}

    query = SquareOrderLine.query.filter_by(store_name=store_name)
    if start_dt:
        query = query.filter(SquareOrderLine.created_at >= start_dt)
    if end_dt:
        query = query.filter(SquareOrderLine.created_at <= end_dt)

    lines = query.all()
    created = 0
    unmapped = set()

    store_user = User.query.filter_by(username=store_name, role="user").first()
    if not store_user:
        return {"created": 0, "unmapped": []}

    sales_recipe_cache = {}
    recipe_cache = {}
    ingredient_cache = {}
    seen_ledger = set()
    ledger_rows = []

    try:
        with db.session.no_autoflush:
            for line in lines:
                if not line.catalog_object_id:
                    continue

                source_id = f"{store_name}:{line.line_uid}"
                mapping = SquareItemSalesRecipe.query.filter_by(
                    store_name=store_name,
                    catalog_object_id=line.catalog_object_id,
                    active=True
                ).first()
                recipe_ingredients = []
                multiplier = Decimal("1")

                if mapping:
                    sales_recipe_id = mapping.sales_recipe_id
                    if sales_recipe_id in sales_recipe_cache:
                        recipe_ingredients = sales_recipe_cache[sales_recipe_id]
                    else:
                        recipe_ingredients = SalesRecipeIngredient.query.filter_by(
                            sales_recipe_id=sales_recipe_id
                        ).all()
                        sales_recipe_cache[sales_recipe_id] = recipe_ingredients
                    multiplier = Decimal(str(mapping.multiplier or 1))
                else:
                    legacy_mapping = SquareItemRecipe.query.filter_by(
                        store_name=store_name,
                        catalog_object_id=line.catalog_object_id,
                        active=True
                    ).first()
                    if legacy_mapping:
                        recipe_id = legacy_mapping.recipe_id
                        if recipe_id in recipe_cache:
                            recipe_ingredients = recipe_cache[recipe_id]
                        else:
                            recipe_ingredients = RecipeIngredient.query.filter_by(
                                recipe_id=recipe_id
                            ).all()
                            recipe_cache[recipe_id] = recipe_ingredients
                        multiplier = Decimal(str(legacy_mapping.multiplier or 1))

                if not recipe_ingredients:
                    unmapped.add(line.catalog_object_id)
                    continue

                quantity = Decimal(str(line.quantity or 0))
                if quantity == 0:
                    continue

                sign = Decimal("1") if line.is_return else Decimal("-1")

                # Aggregate grams per ingredient (prevents duplicates)
                aggregated = {}
                for ri in recipe_ingredients:
                    try:
                        grams_used = Decimal(str(ri.grams_used or 0))
                    except (InvalidOperation, ValueError):
                        continue
                    aggregated[ri.ingredient_id] = aggregated.get(ri.ingredient_id, Decimal("0")) + grams_used

                for ingredient_id, grams_used in aggregated.items():
                    if grams_used == 0:
                        continue

                    ingredient = ingredient_cache.get(ingredient_id)
                    if not ingredient:
                        ingredient = Ingredient.query.get(ingredient_id)
                        ingredient_cache[ingredient_id] = ingredient
                    if not ingredient:
                        continue

                    grams_per_unit = Decimal(str(ingredient.grams_per_unit or 0))
                    if grams_per_unit == 0:
                        continue

                    ledger_key = (source_id, ingredient.id)
                    if ledger_key in seen_ledger:
                        continue
                    seen_ledger.add(ledger_key)

                    grams_needed = grams_used * quantity * multiplier
                    units_needed = grams_needed / grams_per_unit
                    qty_delta = sign * units_needed

                    ledger_rows.append({
                        "store_id": store_user.id,
                        "ingredient_id": ingredient.id,
                        "qty_delta": qty_delta,
                        "reason": "REFUND" if line.is_return else "SALE",
                        "source_type": "SQUARE_LINE",
                        "source_id": source_id,
                        "occurred_at": line.created_at or datetime.utcnow(),
                        "created_at": datetime.utcnow()
                    })

        if ledger_rows:
            result = db.session.execute(
                text(
                    "INSERT IGNORE INTO inventory_ledger "
                    "(store_id, ingredient_id, qty_delta, reason, source_type, source_id, occurred_at, created_at) "
                    "VALUES (:store_id, :ingredient_id, :qty_delta, :reason, :source_type, :source_id, :occurred_at, :created_at)"
                ),
                ledger_rows
            )
            created = result.rowcount or 0

        db.session.commit()
        return {"created": created, "unmapped": sorted(list(unmapped))}
    except OperationalError as exc:
        db.session.rollback()
        if "Lock wait timeout exceeded" in str(exc):
            return {"created": 0, "unmapped": sorted(list(unmapped)), "error": "Database is busy. Please try again."}
        return {"created": 0, "unmapped": sorted(list(unmapped)), "error": "Database error. Please try again."}
    except SQLAlchemyError:
        db.session.rollback()
        return {"created": 0, "unmapped": sorted(list(unmapped)), "error": "Database error. Please try again."}
    finally:
        try:
            if not db.session.is_active:
                db.session.rollback()
            with db.engine.connect() as conn:
                conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
        except SQLAlchemyError:
            pass

def sync_square_orders(store_name, start_utc, end_utc, verbose=False):
    lock_name = f"square_sync_{store_name}"
    lock_ok = db.session.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}).scalar()
    if lock_ok != 1:
        return {
            "orders_fetched": 0,
            "orders_inserted": 0,
            "lines_inserted": 0,
            "ledger_entries": 0,
            "unmapped_items": [],
            "skipped": True
        }

    orders = fetch_sales_for_store(store_name, start_date=start_utc, end_date=end_utc, verbose=verbose)
    orders_inserted = 0
    lines_inserted = 0
    total_orders = len(orders)
    unmapped = set()

    for order in orders:
        square_order_id = order.get("id")
        if not square_order_id:
            continue

        existing_order = SquareOrder.query.filter_by(square_order_id=square_order_id).first()
        if not existing_order:
            order_created = parse_square_datetime(order.get("created_at"))
            order_updated = parse_square_datetime(order.get("updated_at"))
            total_amount = Decimal(str(order.get("total_money", {}).get("amount", 0))) / Decimal("100")

            new_order = SquareOrder(
                square_order_id=square_order_id,
                store_name=store_name,
                location_id=order.get("location_id"),
                state=order.get("state"),
                created_at=order_created,
                updated_at=order_updated,
                total_amount=total_amount
            )
            db.session.add(new_order)
            orders_inserted += 1

        order_created_at = parse_square_datetime(order.get("created_at"))

        # Regular line items (sales)
        for line in order.get("line_items", []) or []:
            line_uid = line.get("uid")
            if not line_uid:
                continue

            exists = SquareOrderLine.query.filter_by(
                square_order_id=square_order_id,
                line_uid=line_uid
            ).first()
            if exists:
                continue

            qty = Decimal(str(line.get("quantity", "0") or "0"))
            new_line = SquareOrderLine(
                square_order_id=square_order_id,
                store_name=store_name,
                line_uid=line_uid,
                item_name=(line.get("name") or "").strip() or None,
                variation_name=(line.get("variation_name") or "").strip() or None,
                catalog_object_id=line.get("catalog_object_id"),
                item_type=line.get("item_type"),
                quantity=qty,
                is_return=False,
                created_at=order_created_at
            )
            db.session.add(new_line)
            lines_inserted += 1

        # Return line items (refunds)
        for ret in order.get("returns", []) or []:
            for rline in ret.get("return_line_items", []) or []:
                line_uid = rline.get("uid")
                if not line_uid:
                    continue

                exists = SquareOrderLine.query.filter_by(
                    square_order_id=square_order_id,
                    line_uid=line_uid
                ).first()
                if exists:
                    continue

                qty = Decimal(str(rline.get("quantity", "0") or "0"))
                new_line = SquareOrderLine(
                    square_order_id=square_order_id,
                    store_name=store_name,
                    line_uid=line_uid,
                    source_line_uid=rline.get("source_line_item_uid"),
                    item_name=(rline.get("name") or "").strip() or None,
                    variation_name=(rline.get("variation_name") or "").strip() or None,
                    catalog_object_id=rline.get("catalog_object_id"),
                    item_type=rline.get("item_type"),
                    quantity=qty,
                    is_return=True,
                    created_at=order_created_at
                )
                db.session.add(new_line)
                lines_inserted += 1

    try:
        db.session.commit()

        apply_result = apply_square_mappings(store_name, start_dt=None, end_dt=None)
        unmapped.update(apply_result["unmapped"])

        return {
            "orders_fetched": total_orders,
            "orders_inserted": orders_inserted,
            "lines_inserted": lines_inserted,
            "ledger_entries": apply_result["created"],
            "unmapped_items": sorted(list(unmapped)),
            "skipped": False
        }
    finally:
        db.session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})

# Create tables
with app.app_context():
    db.create_all()
    ensure_store_weekly_item_schema()
    ensure_ingredient_schema()

@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    """Admin landing page: at-a-glance widgets (today's sales, trends, alerts)."""
    if current_user.role != "admin":
        return redirect(url_for("blank_page"))

    stores = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]
    today = datetime.utcnow().date()
    week_start = today - timedelta(days=6)

    start_utc = datetime.strptime(week_start.strftime("%Y-%m-%d") + " 00:00:00", "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = datetime.strptime(today.strftime("%Y-%m-%d") + " 23:59:59", "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    # ---- Live Square metrics (best-effort: never break the page) ----------
    square_ok = True
    today_revenue = 0.0
    today_orders = 0
    today_items = 0
    store_today = {store: 0.0 for store in stores}
    daily_revenue = {(week_start + timedelta(days=i)): 0.0 for i in range(7)}
    top_items = defaultdict(int)

    try:
        for store in stores:
            orders = fetch_sales_for_store(store, start_date=start_utc, end_date=end_utc)
            for order in orders:
                order_dt = parse_square_datetime(order.get("created_at"))
                if not order_dt:
                    continue
                order_date = order_dt.date()
                amount = (order.get("total_money", {}) or {}).get("amount", 0) / 100

                if order_date in daily_revenue:
                    daily_revenue[order_date] += amount

                if order_date == today:
                    today_revenue += amount
                    today_orders += 1
                    store_today[store] += amount
                    for item in order.get("line_items", []) or []:
                        try:
                            qty = int(float(item.get("quantity", 0) or 0))
                        except (ValueError, TypeError):
                            qty = 0
                        today_items += qty
                        name = (item.get("name") or "").strip()
                        if name:
                            top_items[name] += qty
    except Exception as exc:
        square_ok = False
        print("⚠️ Dashboard Square fetch failed:", exc)

    avg_order_value = (today_revenue / today_orders) if today_orders else 0.0

    # Build ordered structures for the template
    store_sales_today = sorted(
        [{"store": s, "revenue": round(store_today[s], 2)} for s in stores],
        key=lambda x: x["revenue"], reverse=True
    )
    max_store_rev = max((row["revenue"] for row in store_sales_today), default=0) or 1

    trend = [
        {"label": d.strftime("%a"), "date": d.strftime("%d/%m"), "revenue": round(daily_revenue[d], 2)}
        for d in sorted(daily_revenue.keys())
    ]
    max_trend_rev = max((row["revenue"] for row in trend), default=0) or 1

    top_sellers = sorted(
        [{"name": name, "qty": qty} for name, qty in top_items.items()],
        key=lambda x: x["qty"], reverse=True
    )[:5]

    # ---- Instant local-DB metrics ----------------------------------------
    ingredients = Ingredient.query.filter(Ingredient.is_archived == False).all()

    total_stock_value = sum(
        Decimal(str(i.quantity or 0)) * Decimal(str(i.price_per_unit or 0))
        for i in ingredients
    )

    low_stock_items = []
    for i in ingredients:
        threshold = Decimal(str(i.threshold or 0))
        quantity = Decimal(str(i.quantity or 0))
        deficit = threshold - quantity
        if threshold > 0 and deficit > 0:
            low_stock_items.append({"name": i.name, "need": deficit, "supplier": i.supplier})
    low_stock_items.sort(key=lambda x: x["need"], reverse=True)

    unpaid_records = StockOutRecord.query.filter_by(paid=False).all()
    unpaid_ex_gst = sum(
        Decimal(str(r.selling_price or 0)) * Decimal(str(r.quantity or 0))
        for r in unpaid_records
    )
    unpaid_inc_gst = (unpaid_ex_gst * Decimal("1.10")).quantize(Decimal("0.01"))
    unpaid_invoice_count = len({r.invoice_no for r in unpaid_records})

    # ---- Central Kitchen weekly invoiced totals (last 6 weeks, inc GST) ---
    WEEKS = 6
    current_monday = today - timedelta(days=today.weekday())
    week_starts = [current_monday - timedelta(weeks=(WEEKS - 1 - i)) for i in range(WEEKS)]
    earliest_str = week_starts[0].strftime("%Y-%m-%d")

    ck_records = StockOutRecord.query.filter(StockOutRecord.date >= earliest_str).all()
    ck_totals = {ws: Decimal("0") for ws in week_starts}
    for r in ck_records:
        try:
            record_date = datetime.strptime(str(r.date), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        week_key = record_date - timedelta(days=record_date.weekday())
        if week_key in ck_totals:
            ck_totals[week_key] += Decimal(str(r.selling_price or 0)) * Decimal(str(r.quantity or 0))

    ck_weeks = []
    for ws in week_starts:
        inc_gst = (ck_totals[ws] * Decimal("1.10")).quantize(Decimal("0.01"))
        week_end = ws + timedelta(days=6)
        ck_weeks.append({
            "label": ws.strftime("%d %b"),
            "range": f"{ws.strftime('%d %b')} – {week_end.strftime('%d %b')}",
            "total": float(inc_gst)
        })
    max_ck = max((w["total"] for w in ck_weeks), default=0) or 1
    for w in ck_weeks:
        w["pct"] = round(w["total"] / max_ck * 100, 1)
    ck_this_week = ck_weeks[-1]["total"] if ck_weeks else 0.0

    # ---- Loyalty summary (this week, merchant-wide, best-effort) ----------
    loyalty_start_utc = datetime.strptime(current_monday.strftime("%Y-%m-%d") + " 00:00:00", "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    loyalty = fetch_loyalty_summary(loyalty_start_utc, end_utc, week_start=current_monday)

    return render_template(
        "dashboard.html",
        square_ok=square_ok,
        today=today.strftime("%A, %d %B %Y"),
        today_revenue=round(today_revenue, 2),
        today_orders=today_orders,
        today_items=today_items,
        avg_order_value=round(avg_order_value, 2),
        store_sales_today=store_sales_today,
        max_store_rev=max_store_rev,
        trend=trend,
        max_trend_rev=max_trend_rev,
        top_sellers=top_sellers,
        total_stock_value=total_stock_value.quantize(Decimal("0.01")),
        low_stock_items=low_stock_items,
        low_stock_count=len(low_stock_items),
        unpaid_inc_gst=unpaid_inc_gst,
        unpaid_invoice_count=unpaid_invoice_count,
        ck_weeks=ck_weeks,
        ck_this_week=ck_this_week,
        loyalty=loyalty
    )

@app.route("/loyalty_dashboard", methods=["GET"])
@login_required
def loyalty_dashboard():
    """Dedicated loyalty analytics page with weekly default + custom date range."""
    if current_user.role != "admin":
        return redirect(url_for("blank_page"))

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    today = datetime.utcnow().date()

    if not start_date or not end_date:
        # Default: current week starting Monday
        monday = today - timedelta(days=today.weekday())
        start_date = monday.strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        flash("Invalid date range.", "danger")
        return redirect(url_for("loyalty_dashboard"))

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
        start_date, end_date = end_date, start_date

    start_at = datetime.strptime(start_date + " 00:00:00", "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end_at = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    report = fetch_loyalty_report(start_at, end_at, start_dt, end_dt)

    days_span = (end_dt - start_dt).days + 1

    return render_template(
        "loyalty_dashboard.html",
        report=report,
        start_date=start_date,
        end_date=end_date,
        days_span=days_span,
        this_week_start=(today - timedelta(days=today.weekday())).strftime("%Y-%m-%d"),
        today_str=today.strftime("%Y-%m-%d")
    )

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    filter_by = request.args.get("filter_by")
    filter_value = request.args.get("filter_value")

    # ✅ EXCLUDE ARCHIVED INGREDIENTS BY DEFAULT
    query = Ingredient.query.filter(Ingredient.is_archived == False)

    # ✅ Apply filters
    if filter_by == "supplier" and filter_value:
        query = query.filter(Ingredient.supplier == filter_value)
    elif filter_by == "category" and filter_value:
        query = query.filter(Ingredient.category == filter_value)

    ingredients = query.all()

    suppliers = Supplier.query.all()
    categories = Category.query.all()

    # 🔢 Total stock value (active ingredients only)
    total_stock_value = sum(
        Decimal(ingredient.quantity or 0) * Decimal(ingredient.price_per_unit or 0)
        for ingredient in ingredients
    )

    return render_template(
        "index.html",
        ingredients=ingredients,
        suppliers=suppliers,
        categories=categories,
        total_stock_value=round(total_stock_value, 2)
    )


# 🟢 Route: Add an ingredient (POST request)
@app.route("/add", methods=["POST"])
@login_required
def add_ingredient():
    name = request.form.get("name")
    quantity = request.form.get("quantity")
    unit = request.form.get("unit")
    supplier = request.form.get("supplier")
    category = request.form.get("category")
    grams_per_unit = request.form.get("grams_per_unit")
    threshold = request.form.get("threshold")
    price_per_unit = request.form.get("price_per_unit")
    selling_price = request.form.get("selling_price")
    daily_stocktake = request.form.get("daily_stocktake")
    measurement_type = request.form.get("measurement_type", "numeric")

    if measurement_type not in {"numeric", "binary"}:
        measurement_type = "numeric"


    daily_stocktake = True if daily_stocktake == "1" else False
    weekly_stocktake = True if request.form.get("weekly_stocktake") == "1" else False  # ✅ Capture weekly stocktake


    if name and quantity and unit:
        new_ingredient = Ingredient(
            name=name,
            quantity=int(quantity),
            unit=unit,
            supplier=supplier,
            category=category,
            grams_per_unit=float(grams_per_unit) if grams_per_unit else None,
            threshold=int(threshold) if threshold else None,
            price_per_unit=float(price_per_unit) if price_per_unit else None,
            selling_price=float(selling_price) if selling_price else None,
            daily_stocktake=daily_stocktake,
            weekly_stocktake=weekly_stocktake,
            measurement_type=measurement_type
        )
        db.session.add(new_ingredient)
        db.session.commit()

    return redirect(url_for("index"))

@app.route("/archive/<int:id>", methods=["POST"])
@login_required
def archive_ingredient(id):
    ingredient = db.session.get(Ingredient, id)

    if not ingredient:
        flash("Ingredient not found.", "danger")
        return redirect(url_for("index"))

    try:
        ingredient.is_archived = True
        db.session.commit()

        print(f"📦 ARCHIVED INGREDIENT → ID={ingredient.id}, Name={ingredient.name}")

        flash(f"'{ingredient.name}' archived successfully.", "success")

    except Exception as e:
        db.session.rollback()
        print("❌ Error archiving ingredient:", e)
        flash(f"Error archiving ingredient: {str(e)}", "danger")

    return redirect(url_for("index"))

# 🟢 Route: Show the Add Ingredient Page (GET request)
@app.route("/add_ingredient", methods=["GET", "POST"])
@login_required
def add_ingredient_page():
    suppliers = Supplier.query.all()  # ✅ Fetch suppliers from the database
    categories = Category.query.all()  # Fetch categories
    return render_template("add_ingredient.html", suppliers=suppliers, categories=categories, ingredient=None)

# 🟢 Route: Edit an ingredient (Display Edit Form)
@app.route("/edit/<int:id>", methods=["GET"])
@login_required
def edit_ingredient(id):
    ingredient = Ingredient.query.get(id)
    suppliers = Supplier.query.all()  # ✅ Fetch suppliers for dropdown
    categories = Category.query.all()  # Fetch categories
    return render_template("edit.html", ingredient=ingredient, suppliers=suppliers, categories=categories)

@app.route("/update/<int:id>", methods=["POST"])
@login_required
def update_ingredient(id):
    from decimal import Decimal

    # 🔧 Load object into the correct session
    ingredient = db.session.merge(Ingredient.query.get(id))

    if not ingredient:
        flash("❌ Ingredient not found.", "danger")
        return redirect(url_for("index"))

    try:
        # ✅ Form parsing and type conversion
        ingredient.name = request.form.get("name")
        ingredient.quantity = float(request.form.get("quantity", 0))
        ingredient.unit = request.form.get("unit")
        ingredient.supplier = request.form.get("supplier")
        ingredient.category = request.form.get("category")
        ingredient.grams_per_unit = float(request.form.get("grams_per_unit", 0))
        ingredient.threshold = float(request.form.get("threshold", 0))
        ingredient.price_per_unit = float(request.form.get("price_per_unit", 0))
        ingredient.selling_price = float(request.form.get("selling_price", 0))
        ingredient.measurement_type = request.form.get("measurement_type", "numeric")
        if ingredient.measurement_type not in {"numeric", "binary"}:
            ingredient.measurement_type = "numeric"

        # ✅ Booleans
        ingredient.daily_stocktake = request.form.get("daily_stocktake") == "1"
        ingredient.weekly_stocktake = request.form.get("weekly_stocktake") == "1"

        # ✅ Save and commit
        db.session.add(ingredient)
        db.session.commit()
        flash("✅ Ingredient updated successfully!", "success")

    except Exception as e:
        db.session.rollback()
        print("❌ Error updating ingredient:", str(e))
        flash("❌ Failed to update ingredient.", "danger")

    return redirect(url_for("index"))

# 🟢 Route: Manage Suppliers
@app.route("/suppliers", methods=["GET", "POST"])
@login_required
def manage_suppliers():
    if request.method == "POST":
        name = request.form.get("name")
        if name:
            new_supplier = Supplier(name=name)
            db.session.add(new_supplier)
            db.session.commit()

    suppliers = Supplier.query.all()  # Retrieve all suppliers
    return render_template("suppliers.html", suppliers=suppliers)

@app.route("/edit_supplier/<int:id>", methods=["GET", "POST"])
@login_required
def edit_supplier(id):
    supplier = Supplier.query.get(id)

    if request.method == "POST":
        new_name = request.form.get("name")
        if supplier and new_name:
            supplier.name = new_name
            db.session.commit()
            return redirect(url_for("manage_suppliers"))

    return render_template("edit_supplier.html", supplier=supplier)

@app.route("/delete_supplier/<int:id>", methods=["GET"])
@login_required
def delete_supplier(id):
    supplier = Supplier.query.get(id)
    if supplier:
        db.session.delete(supplier)
        db.session.commit()

    return redirect(url_for("manage_suppliers"))

@app.route("/categories", methods=["GET", "POST"])
@login_required
def manage_categories():
    if request.method == "POST":
        name = request.form.get("name")
        if name:
            new_category = Category(name=name)
            db.session.add(new_category)
            db.session.commit()
            return redirect(url_for("manage_categories"))

    categories = Category.query.all()  # Fetch all categories
    return render_template("categories.html", categories=categories)

@app.route("/edit_category/<int:id>", methods=["GET", "POST"])
@login_required
def edit_category(id):
    category = Category.query.get(id)

    if request.method == "POST":
        new_name = request.form.get("name")
        if category and new_name:
            category.name = new_name
            db.session.commit()
            return redirect(url_for("manage_categories"))

    return render_template("edit_category.html", category=category)

@app.route("/delete_category/<int:id>", methods=["GET"])
@login_required
def delete_category(id):
    category = Category.query.get(id)
    if category:
        db.session.delete(category)
        db.session.commit()
    return redirect(url_for("manage_categories"))

@app.route("/stock_in", methods=["GET", "POST"])
@login_required
def stock_in():
    try:
        is_store_user = current_user.role == "user"
        supplier_filter = request.args.get("supplier")

        if is_store_user:
            ingredient_query = db.session.query(
                Ingredient,
                StoreInventory.quantity.label("store_quantity")
            ).outerjoin(
                StoreInventory,
                and_(
                    StoreInventory.ingredient_id == Ingredient.id,
                    StoreInventory.store_id == current_user.id
                )
            ).filter(Ingredient.is_archived == False)

            if supplier_filter:
                ingredient_query = ingredient_query.filter(Ingredient.supplier == supplier_filter)

            rows = ingredient_query.order_by(Ingredient.name).all()
            ingredients = [
                {
                    "id": ingredient.id,
                    "name": ingredient.name,
                    "quantity": Decimal(str(store_qty)) if store_qty is not None else Decimal("0"),
                    "unit": ingredient.unit,
                    "supplier": ingredient.supplier
                }
                for ingredient, store_qty in rows
            ]
        else:
            if supplier_filter:
                ingredients = Ingredient.query.filter(Ingredient.is_archived == False, Ingredient.supplier == supplier_filter).order_by(Ingredient.name).all()
            else:
                ingredients = Ingredient.query.filter(Ingredient.is_archived == False).order_by(Ingredient.name).all()

        suppliers = Supplier.query.order_by(Supplier.name).all()

        if request.method == "POST":
            ingredient_id = request.form.get("ingredient_id")
            stock_added_raw = request.form.get("stock_added")

            ingredient = Ingredient.query.get(ingredient_id)
            if not ingredient:
                return jsonify({"success": False, "error": "Ingredient not found."})

            # ✅ Convert input to Decimal
            stock_added = Decimal(str(stock_added_raw))

            if is_store_user:
                store_inventory = StoreInventory.query.filter_by(
                    store_id=current_user.id,
                    ingredient_id=ingredient.id
                ).first()

                if not store_inventory:
                    store_inventory = StoreInventory(
                        store_id=current_user.id,
                        ingredient_id=ingredient.id,
                        quantity=Decimal("0")
                    )
                    db.session.add(store_inventory)

                store_inventory.quantity = Decimal(str(store_inventory.quantity or 0)) + stock_added
                new_quantity = store_inventory.quantity
            else:
                # ✅ Update ingredient quantity
                ingredient.quantity += stock_added
                db.session.merge(ingredient)
                new_quantity = ingredient.quantity

            # ✅ Calculate price and total cost (safely using Decimal)
            raw_price = ingredient.price_per_unit or 0
            price = Decimal(str(raw_price))
            total_cost = price * stock_added

            # ✅ Create StockInRecord entry
            record = StockInRecord(
                date=datetime.utcnow(),
                supplier=ingredient.supplier,
                item=ingredient.name,
                price=price,
                quantity=stock_added,
                total_cost=total_cost
            )

            db.session.add(record)
            if is_store_user:
                db.session.flush()
                ledger = InventoryLedger(
                    store_id=current_user.id,
                    ingredient_id=ingredient.id,
                    qty_delta=stock_added,
                    reason="STOCK_IN",
                    source_type="STOCK_IN",
                    source_id=f"stockin:{record.id}",
                    occurred_at=record.date
                )
                db.session.add(ledger)
            db.session.commit()

            return jsonify({"success": True, "new_quantity": float(new_quantity)})

        template_name = "stock_in_user.html" if is_store_user else "stock_in.html"
        return render_template(
            template_name,
            ingredients=ingredients,
            suppliers=suppliers,
            supplier_filter=supplier_filter
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": "Unexpected error occurred."})

@app.route("/stock_out", methods=["GET", "POST"])
@login_required
def stock_out():
    supplier_filter = request.args.get("supplier")

    # ✅ Get filtered ingredients (alphabetically ordered, excluding archived)
    if supplier_filter:
        ingredients = Ingredient.query.filter(Ingredient.is_archived == False, Ingredient.supplier == supplier_filter).order_by(Ingredient.name).all()
    else:
        ingredients = Ingredient.query.filter(Ingredient.is_archived == False).order_by(Ingredient.name).all()

    suppliers = Supplier.query.all()

    # ✅ Live Stock Dictionary for JS
    current_stock = {str(ing.id): float(ing.quantity) for ing in ingredients}

    # ✅ Load weekly stocktake items from session (if present)
    prefilled_stockout_items = session.pop("prefilled_stock_out", None)
    if prefilled_stockout_items:
        print("📦 Prefilled items from session:", prefilled_stockout_items)

    if request.method == "POST":
        if request.is_json:
            data = request.get_json()
            stock_out_items = data.get("stockOutItems", [])

            print("📥 Received Stock Out POST:", stock_out_items)

            if not stock_out_items:
                print("⚠️ No items submitted in stockOutItems.")
                return jsonify({"success": False, "message": "No items to process."})

            # ✅ Generate next invoice number
            last_invoice = db.session.query(db.func.max(StockOutRecord.invoice_no)).scalar()
            invoice_no = last_invoice + 1 if last_invoice else 1000

            for item in stock_out_items:
                try:
                    ingredient_id = item.get("ingredientId")
                    stock_removed = Decimal(item.get("stockRemoved", "0"))

                    if not ingredient_id:
                        return jsonify({"success": False, "message": "Missing ingredient ID in item."})

                    ingredient = Ingredient.query.get(ingredient_id)
                    if not ingredient:
                        return jsonify({"success": False, "message": f"Ingredient not found for ID {ingredient_id}."})

                    # Merge ingredient into session
                    ingredient = db.session.merge(ingredient)

                    if ingredient.quantity < stock_removed:
                        return jsonify({"success": False, "message": f"Not enough stock for {ingredient.name}."})

                    ingredient.quantity -= stock_removed

                    record = StockOutRecord(
                        invoice_no=invoice_no,
                        date=item.get("stockDate"),
                        store=item.get("store"),
                        item=ingredient.name,
                        price=ingredient.price_per_unit or ingredient.price or Decimal(0),
                        selling_price=ingredient.selling_price or Decimal(0),
                        quantity=stock_removed
                    )
                    db.session.add(record)

                except Exception as e:
                    print(f"❌ Error processing item {item}: {e}")
                    db.session.rollback()
                    return jsonify({"success": False, "message": f"Error processing item: {e}"})

            db.session.commit()

            # ✅ Return updated stock
            updated_stock = {
                str(item["ingredientId"]): float(Ingredient.query.get(item["ingredientId"]).quantity)
                for item in stock_out_items
            }

            return jsonify({
                "success": True,
                "invoice_no": invoice_no,
                "updatedStock": updated_stock
            })

        print("❌ Invalid request type — expected JSON.")
        return jsonify({"success": False, "message": "Invalid request."})

    return render_template(
        "stock_out.html",
        ingredients=ingredients,
        suppliers=suppliers,
        supplier_filter=supplier_filter,
        current_stock=current_stock,
        prefilled_stockout_items=prefilled_stockout_items  # ✅ used in frontend JS
    )

@app.route("/stock_out_history", methods=["GET"])
@login_required
def stock_out_history():
    store = request.args.get("store")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    query = StockOutRecord.query

    if store:
        query = query.filter(StockOutRecord.store == store)
    if start_date:
        query = query.filter(StockOutRecord.date >= start_date)
    if end_date:
        query = query.filter(StockOutRecord.date <= end_date)

    stock_out_records = query.order_by(StockOutRecord.invoice_no.desc()).all()

    from decimal import Decimal

    def safe_decimal(v):
        try:
            return Decimal(str(v or 0))
        except:
            return Decimal(0)

    # -----------------------------
    # ✅ GROUP BY INVOICE NUMBER
    # -----------------------------
    invoices = {}

    for r in stock_out_records:
        invoice_no = r.invoice_no

        if invoice_no not in invoices:
            invoices[invoice_no] = {
                "invoice_no": invoice_no,
                "date": r.date,
                "store": r.store,
                "items": [],
                "total_cost": Decimal(0),
                "total_selling": Decimal(0),
            }

        item_cost = safe_decimal(r.price) * safe_decimal(r.quantity)
        item_selling = safe_decimal(r.selling_price) * safe_decimal(r.quantity)

        # Add line item
        invoices[invoice_no]["items"].append({
            "item": r.item,
            "quantity": r.quantity,
            "price": r.price,
            "selling_price": r.selling_price,
            "total_cost": item_cost,
            "total_selling": item_selling,
        })

        # Update totals
        invoices[invoice_no]["total_cost"] += item_cost
        invoices[invoice_no]["total_selling"] += item_selling

    # Convert dict → list sorted by invoice_no descending
    invoice_list = sorted(
        invoices.values(),
        key=lambda x: x["invoice_no"],
        reverse=True
    )

    # Overall totals
    total_cost_value = sum(inv["total_cost"] for inv in invoice_list)
    total_selling_value = sum(inv["total_selling"] for inv in invoice_list)

    return render_template(
        "stock_out_history.html",
        invoices=invoice_list,              # ← NEW DATA
        total_cost_value=round(total_cost_value, 2),
        total_selling_value=round(total_selling_value, 2),
        selected_store=store,
        start_date=start_date,
        end_date=end_date
    )

@app.route("/invoices")
@login_required
def invoice_page():
    """Display buttons for each store."""
    return render_template("invoices.html")

@app.route("/invoices/<store>")
@login_required
def invoices_by_store(store):

    # -------------------------------
    # Fetch unpaid invoices
    # -------------------------------
    unpaid_invoices = (
        db.session.query(
            StockOutRecord.invoice_no,
            StockOutRecord.date,
            StockOutRecord.paid
        )
        .filter_by(store=store, paid=False)
        .distinct()
        .all()
    )

    # -------------------------------
    # Fetch paid invoices
    # -------------------------------
    paid_invoices = (
        db.session.query(
            StockOutRecord.invoice_no,
            StockOutRecord.date,
            StockOutRecord.paid
        )
        .filter_by(store=store, paid=True)
        .distinct()
        .all()
    )

    # -------------------------------
    # Helper: safe Decimal conversion
    # -------------------------------
    def safe_decimal(val):
        try:
            return Decimal(str(val or 0))
        except:
            return Decimal("0")

    GST_RATE = Decimal("0.10")

    # -------------------------------
    # Collect invoice numbers
    # -------------------------------
    all_invoice_nos = (
        [row[0] for row in unpaid_invoices] +
        [row[0] for row in paid_invoices]
    )

    # -------------------------------
    # Fetch all line items
    # -------------------------------
    records = (
        StockOutRecord.query
        .filter(StockOutRecord.invoice_no.in_(all_invoice_nos))
        .order_by(StockOutRecord.invoice_no)
        .all()
    )

    # -------------------------------
    # Group items by invoice
    # -------------------------------
    items_by_invoice = {}

    for r in records:
        r.total_cost = safe_decimal(r.price) * safe_decimal(r.quantity)

        # ✅ Selling price is GST-exclusive
        r.total_selling_ex_gst = safe_decimal(r.selling_price) * safe_decimal(r.quantity)

        # ✅ GST calculated AFTER
        r.gst_amount = (r.total_selling_ex_gst * GST_RATE).quantize(Decimal("0.01"))

        # ✅ Final total
        r.total_selling_inc_gst = (r.total_selling_ex_gst + r.gst_amount).quantize(Decimal("0.01"))

        items_by_invoice.setdefault(r.invoice_no, []).append(r)

    # -------------------------------
    # ✅ TOTAL UNPAID (GST INCLUSIVE)
    # -------------------------------
    total_unpaid_amount = Decimal("0")

    for inv in unpaid_invoices:
        invoice_no = inv[0]
        items = items_by_invoice.get(invoice_no, [])

        invoice_ex_gst = sum(i.total_selling_ex_gst for i in items)
        invoice_gst = (invoice_ex_gst * GST_RATE).quantize(Decimal("0.01"))
        invoice_inc_gst = invoice_ex_gst + invoice_gst

        total_unpaid_amount += invoice_inc_gst

    total_unpaid_amount = total_unpaid_amount.quantize(Decimal("0.01"))

    # -------------------------------
    # Render
    # -------------------------------
    return render_template(
        "invoices_list.html",
        store=store,
        unpaid_invoices=unpaid_invoices,
        paid_invoices=paid_invoices,
        items_by_invoice=items_by_invoice,
        total_unpaid_amount=total_unpaid_amount
    )



@app.route("/export_invoice/<int:invoice_no>")
@login_required
def export_invoice(invoice_no):
    """Generate and download a formatted PDF invoice (GST exclusive + GST added)."""

    def _latin1_safe(value):
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        # Normalize common Unicode punctuation to ASCII equivalents
        value = (
            value.replace("（", "(")
            .replace("）", ")")
            .replace("–", "-")
            .replace("—", "-")
            .replace("’", "'")
            .replace("“", '"')
            .replace("”", '"')
        )
        return value.encode("latin-1", "replace").decode("latin-1")

    records = StockOutRecord.query.filter_by(invoice_no=invoice_no).all()

    if not records:
        return "Invoice not found.", 404

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # ================= COMPANY INFO =================
    pdf.set_font("Arial", "B", 16)
    pdf.cell(200, 10, "Bing Chillin", ln=True, align="C")

    pdf.set_font("Arial", "", 12)
    pdf.cell(200, 6, "Victoria, Australia", ln=True, align="C")
    pdf.cell(200, 6, "ABN 35662088717", ln=True, align="C")
    pdf.cell(200, 6, "0401546788 | mail@bingchillin.com.au", ln=True, align="C")
    pdf.ln(10)

    # ================= INVOICE HEADER =================
    pdf.set_font("Arial", "B", 18)
    pdf.cell(200, 10, "INVOICE", ln=True, align="C")
    pdf.ln(5)

    # ================= INVOICE DETAILS =================
    pdf.set_font("Arial", "B", 12)
    pdf.cell(40, 8, "Invoice #:", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(150, 8, str(invoice_no), border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(40, 8, "Bill To:", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(150, 8, _latin1_safe(records[0].store), border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(40, 8, "Invoice Date:", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(150, 8, _latin1_safe(records[0].date), border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(40, 8, "Terms:", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(150, 8, "Due on Receipt", border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(40, 8, "Due Date:", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(150, 8, _latin1_safe(records[0].date), border=0, ln=True)

    pdf.ln(10)

    # ================= TABLE HEADER =================
    pdf.set_font("Arial", "B", 12)
    pdf.cell(80, 10, "Item & Description", border=1)
    pdf.cell(30, 10, "Qty", border=1, align="C")
    pdf.cell(40, 10, "Selling Price (ex GST)", border=1, align="C")
    pdf.cell(40, 10, "Line Total", border=1, align="C", ln=True)

    # ================= INGREDIENT PRICES =================
    ingredient_prices = {
        i.name: i.selling_price if i.selling_price else i.price_per_unit
        for i in Ingredient.query.all()
    }

    # ================= TABLE DATA =================
    pdf.set_font("Arial", "", 12)

    subtotal = 0.0

    for record in records:
        selling_price = ingredient_prices.get(record.item, record.price)

        line_total = float(selling_price) * float(record.quantity)
        subtotal += line_total

        pdf.cell(80, 10, _latin1_safe(record.item), border=1)
        pdf.cell(30, 10, f"{record.quantity:.1f}", border=1, align="C")
        pdf.cell(40, 10, f"${selling_price:.2f}", border=1, align="C")
        pdf.cell(40, 10, f"${line_total:.2f}", border=1, align="C", ln=True)

    # ================= TOTALS =================
    GST_RATE = 0.10
    gst_amount = subtotal * GST_RATE
    grand_total = subtotal + gst_amount

    pdf.ln(5)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(80, 8, "Subtotal (ex GST):", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(80, 8, f"${subtotal:.2f}", border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(80, 8, "GST (10%):", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(80, 8, f"${gst_amount:.2f}", border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(80, 8, "Total (inc GST):", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(80, 8, f"${grand_total:.2f}", border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(80, 8, "Payment Made (-):", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(80, 8, "$0.00", border=0, ln=True)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(80, 8, "Balance Due:", border=0)
    pdf.set_font("Arial", "", 12)
    pdf.cell(80, 8, f"${grand_total:.2f}", border=0, ln=True)

    pdf.ln(15)

    # ================= NOTES =================
    pdf.set_font("Arial", "I", 12)
    pdf.cell(200, 8, "Thanks for your business.", ln=True, align="C")

    # ================= SAVE FILE =================
    invoices_dir = os.path.join(os.getcwd(), "static", "invoices")
    if not os.path.exists(invoices_dir):
        os.makedirs(invoices_dir)

    store_name = records[0].store.replace(" ", "_")
    pdf_filename = os.path.join(
        invoices_dir,
        f"Invoice_{invoice_no}_{store_name}.pdf"
    )

    pdf.output(pdf_filename)

    return send_file(pdf_filename, as_attachment=True)

@app.route("/mark_paid/<string:invoice_no>", methods=["POST"])
@login_required
def mark_invoice_paid(invoice_no):
    """Marks an invoice as paid and ensures the commit is properly applied."""
    try:
        print(f"📝 Marking Invoice {invoice_no} as Paid...")

        # ✅ Fetch the records associated with this invoice number
        records = StockOutRecord.query.filter_by(invoice_no=invoice_no).all()

        if not records:
            print(f"❌ Invoice {invoice_no} not found in StockOutRecord.")
            return jsonify({"success": False, "message": "Invoice not found."})

        # ✅ Debug: Print before update
        for record in records:
            print(f"🔍 BEFORE UPDATE - Invoice No: {record.invoice_no}, Paid: {record.paid}")

        # ✅ ORM Update (Not working, but keeping for reference)
        for record in records:
            record.paid = 1  # ✅ Force setting paid to 1

        db.session.commit()  # ✅ Try committing ORM changes (may fail)

        # ✅ Force update using raw SQL query (ensures database is updated)
        db.session.execute(
            text("UPDATE stock_out_record SET paid = 1 WHERE invoice_no = :invoice_no"),
            {"invoice_no": invoice_no}
        )
        db.session.commit()  # ✅ Second forced commit

        # ✅ Debug: Print after update
        updated_records = db.session.execute(
            text("SELECT invoice_no, paid FROM stock_out_record WHERE invoice_no = :invoice_no"),
            {"invoice_no": invoice_no}
        ).fetchall()

        print(f"📝 DATABASE CHECK AFTER COMMIT: {updated_records}")  # ✅ Logs database results

        return jsonify({"success": True, "message": f"Invoice {invoice_no} marked as Paid!", "invoice_no": invoice_no})

    except Exception as e:
        db.session.rollback()  # ✅ Rollback on failure
        print(f"❌ Error marking invoice as paid: {e}")
        return jsonify({"success": False, "message": "Error updating invoice status."})

@app.route("/recipes", methods=["GET", "POST"])
@login_required
def manage_recipes():
    if request.method == "POST":
        recipe_id = request.form.get("recipe_id")  # ✅ Check if editing an existing recipe
        output_item_id = request.form.get("output_item")
        ingredient_ids = request.form.getlist("ingredient_id[]")
        grams_used_list = request.form.getlist("grams_used[]")

        # ✅ Validate input
        if not output_item_id:
            return jsonify({"success": False, "message": "Please select an output item."})
        if not ingredient_ids or not grams_used_list:
            return jsonify({"success": False, "message": "Please add at least one ingredient."})

        try:
            valid_ingredients = [
                (int(ingredient_ids[i]), float(grams_used_list[i]))
                for i in range(len(ingredient_ids))
                if float(grams_used_list[i]) > 0
            ]
        except ValueError:
            return jsonify({"success": False, "message": "Invalid ingredient quantity entered."})

        # ✅ Editing an Existing Recipe
        if recipe_id:
            recipe = Recipe.query.get(recipe_id)
            if not recipe:
                return jsonify({"success": False, "message": "Recipe not found."})

            # ✅ Delete existing ingredients before updating
            RecipeIngredient.query.filter_by(recipe_id=recipe_id).delete()

        else:
            # ✅ If not editing, create a new recipe
            recipe = Recipe(output_item_id=output_item_id)
            db.session.add(recipe)
            db.session.commit()  # Commit to generate the recipe ID

        # ✅ Add Ingredients to the Recipe
        for ingredient_id, grams_used in valid_ingredients:
            recipe_ingredient = RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=ingredient_id,
                grams_used=grams_used
            )
            db.session.add(recipe_ingredient)

        db.session.commit()
        return jsonify({"success": True, "message": "Recipe saved successfully!"})

    # ✅ Load all recipes with their ingredients (optimized)
    recipes = Recipe.query.all()
    recipes_data = {}

    if recipes:
        recipe_ids = [r.id for r in recipes]
        output_item_ids = {r.output_item_id for r in recipes if r.output_item_id}
        output_items = Ingredient.query.filter(Ingredient.id.in_(output_item_ids)).all()
        output_map = {i.id: i for i in output_items}

        recipe_ingredients = RecipeIngredient.query.filter(
            RecipeIngredient.recipe_id.in_(recipe_ids)
        ).all()
        ingredient_ids = {ri.ingredient_id for ri in recipe_ingredients}
        ingredient_map = {
            i.id: i for i in Ingredient.query.filter(Ingredient.id.in_(ingredient_ids)).all()
        }

        ingredients_by_recipe = defaultdict(list)
        for ri in recipe_ingredients:
            ingredients_by_recipe[ri.recipe_id].append(ri)

        for recipe in recipes:
            output_item = output_map.get(recipe.output_item_id)
            if not output_item:
                print(f"⚠️ Warning: Recipe {recipe.id} has a missing output item. Skipping.")
                continue

            recipes_data[recipe.id] = {
                "id": recipe.id,
                "output_item_name": output_item.name,
                "ingredients": [
                    {
                        "id": ri.ingredient_id,
                        "name": ingredient_map.get(ri.ingredient_id).name,
                        "quantity": ri.grams_used
                    }
                    for ri in ingredients_by_recipe.get(recipe.id, [])
                    if ingredient_map.get(ri.ingredient_id)
                ]
            }

    ingredients = Ingredient.query.filter(Ingredient.is_archived == False).order_by(Ingredient.name.asc()).all()
    return render_template("recipes.html", recipes=recipes_data, ingredients=ingredients)

@app.route("/sales_recipes", methods=["GET", "POST"])
@login_required
def manage_sales_recipes():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        sales_recipe_id = request.form.get("sales_recipe_id")
        name = (request.form.get("recipe_name") or "").strip()
        ingredient_ids = request.form.getlist("ingredient_id[]")
        grams_used_list = request.form.getlist("grams_used[]")

        if not name:
            return jsonify({"success": False, "message": "Please enter a recipe name."})
        if not ingredient_ids or not grams_used_list:
            return jsonify({"success": False, "message": "Please add at least one ingredient."})

        try:
            valid_ingredients = [
                (int(ingredient_ids[i]), float(grams_used_list[i]))
                for i in range(len(ingredient_ids))
                if float(grams_used_list[i]) > 0
            ]
        except ValueError:
            return jsonify({"success": False, "message": "Invalid ingredient quantity entered."})

        if not valid_ingredients:
            return jsonify({"success": False, "message": "Please add at least one valid ingredient."})

        try:
            if sales_recipe_id:
                recipe = SalesRecipe.query.get(sales_recipe_id)
                if not recipe:
                    return jsonify({"success": False, "message": "Sales recipe not found."})
                existing = SalesRecipe.query.filter(SalesRecipe.name == name, SalesRecipe.id != recipe.id).first()
                if existing:
                    return jsonify({"success": False, "message": "Recipe name already exists."})
                recipe.name = name
                SalesRecipeIngredient.query.filter_by(
                    sales_recipe_id=sales_recipe_id
                ).delete(synchronize_session=False)
            else:
                existing = SalesRecipe.query.filter_by(name=name).first()
                if existing:
                    return jsonify({"success": False, "message": "Recipe name already exists."})
                recipe = SalesRecipe(name=name)
                db.session.add(recipe)
                db.session.flush()

            for ingredient_id, grams_used in valid_ingredients:
                entry = SalesRecipeIngredient(
                    sales_recipe_id=recipe.id,
                    ingredient_id=ingredient_id,
                    grams_used=grams_used
                )
                db.session.add(entry)

            db.session.commit()
            return jsonify({"success": True, "message": "Sales recipe saved successfully!"})
        except OperationalError as exc:
            db.session.rollback()
            if "Lock wait timeout exceeded" in str(exc):
                return jsonify({"success": False, "message": "Database is busy. Please try again."})
            return jsonify({"success": False, "message": "Database error. Please try again."})
        except SQLAlchemyError:
            db.session.rollback()
            return jsonify({"success": False, "message": "Database error. Please try again."})

    recipes = SalesRecipe.query.order_by(SalesRecipe.name.asc()).all()
    recipes_data = {}

    if recipes:
        recipe_ids = [r.id for r in recipes]
        recipe_ingredients = SalesRecipeIngredient.query.filter(
            SalesRecipeIngredient.sales_recipe_id.in_(recipe_ids)
        ).all()
        ingredient_ids = {ri.ingredient_id for ri in recipe_ingredients}
        ingredient_map = {
            i.id: i for i in Ingredient.query.filter(Ingredient.id.in_(ingredient_ids)).all()
        }

        ingredients_by_recipe = defaultdict(list)
        for ri in recipe_ingredients:
            ingredients_by_recipe[ri.sales_recipe_id].append(ri)

        for recipe in recipes:
            recipes_data[recipe.id] = {
                "id": recipe.id,
                "name": recipe.name,
                "ingredients": [
                    {
                        "id": ri.ingredient_id,
                        "name": ingredient_map.get(ri.ingredient_id).name if ingredient_map.get(ri.ingredient_id) else "Unknown",
                        "quantity": ri.grams_used
                    }
                    for ri in ingredients_by_recipe.get(recipe.id, [])
                ]
            }

    ingredients = Ingredient.query.filter(Ingredient.is_archived == False).order_by(Ingredient.name.asc()).all()
    return render_template("sales_recipes.html", recipes=recipes_data, ingredients=ingredients)

@app.route("/get_sales_recipe/<int:recipe_id>", methods=["GET"])
@login_required
def get_sales_recipe(recipe_id):
    if current_user.role != "admin":
        return jsonify({"success": False, "message": "Access denied."})

    recipe = SalesRecipe.query.get(recipe_id)
    if not recipe:
        return jsonify({"success": False, "message": "Recipe not found."})

    recipe_ingredients = SalesRecipeIngredient.query.filter_by(sales_recipe_id=recipe.id).all()
    ingredients_data = [
        {"id": ri.ingredient_id, "quantity": float(ri.grams_used)}
        for ri in recipe_ingredients
    ]
    return jsonify({"success": True, "name": recipe.name, "ingredients": ingredients_data})

@app.route("/delete_sales_recipe/<int:recipe_id>", methods=["POST"])
@login_required
def delete_sales_recipe(recipe_id):
    if current_user.role != "admin":
        return jsonify({"success": False, "message": "Access denied."})

    recipe_exists = db.session.query(SalesRecipe.id).filter_by(id=recipe_id).first()
    if not recipe_exists:
        return jsonify({"success": False, "message": "Recipe not found."})

    try:
        mappings_deleted = SquareItemSalesRecipe.query.filter_by(
            sales_recipe_id=recipe_id
        ).delete(synchronize_session=False)
        SalesRecipeIngredient.query.filter_by(
            sales_recipe_id=recipe_id
        ).delete(synchronize_session=False)
        recipes_deleted = SalesRecipe.query.filter_by(
            id=recipe_id
        ).delete(synchronize_session=False)
        db.session.commit()
        if not recipes_deleted:
            return jsonify({"success": False, "message": "Recipe not found."})
        message = "Sales recipe deleted."
        if mappings_deleted:
            message += f" Removed {mappings_deleted} Square mapping(s)."
        return jsonify({"success": True, "message": message})
    except OperationalError as exc:
        db.session.rollback()
        if "Lock wait timeout exceeded" in str(exc):
            return jsonify({"success": False, "message": "Database is busy. Please try again."})
        return jsonify({"success": False, "message": "Database error. Please try again."})
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({"success": False, "message": "Database error. Please try again."})

GST_RATE = Decimal("0.10")  # 🔹 Used ONLY for revenue display, NOT cost


@app.route("/reports", methods=["GET"])
@login_required
def reporting_page():
    report_type = request.args.get("report_type", "gross_profit")
    selected_range = request.args.get("date_range", "weekly")
    selected_store = request.args.get("store", "")

    # 🗓️ Manual date override
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")

    today = datetime.utcnow().date()

    if start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        selected_range = "custom"
    else:
        if selected_range == "weekly":
            start_date = today - timedelta(days=7)
        elif selected_range == "monthly":
            start_date = today.replace(day=1)
        elif selected_range == "quarterly":
            quarter_start_month = ((today.month - 1) // 3) * 3 + 1
            start_date = today.replace(month=quarter_start_month, day=1)
        else:
            start_date = today - timedelta(days=7)

        end_date = today

    # 🧾 Fetch sales
    query = StockOutRecord.query.filter(
        StockOutRecord.date.between(start_date, end_date)
    )

    if selected_store:
        query = query.filter(StockOutRecord.store == selected_store)

    sales = query.all()

    # 🔗 Ingredient lookup (by name)
    ingredients = {i.name: i for i in Ingredient.query.all()}

    # ================= ITEM GP =================
    report_rows = defaultdict(lambda: {
        "item": "",
        "quantity": Decimal("0"),
        "revenue_ex": Decimal("0"),
        "cost": Decimal("0"),            # 🔹 PURE COST (NO GST)
        "gross_profit": Decimal("0"),
    })

    # ================= CATEGORY GP =================
    category_rows = defaultdict(lambda: {
        "category": "",
        "revenue_ex": Decimal("0"),
        "cost": Decimal("0"),
        "gross_profit": Decimal("0"),
    })

    for s in sales:
        ingredient = ingredients.get(s.item)
        if not ingredient:
            continue

        qty = Decimal(str(s.quantity))
        sell_price = Decimal(str(ingredient.selling_price or 0))
        cost_price = Decimal(str(ingredient.price_per_unit or 0))
        category = ingredient.category or "Uncategorised"

        revenue = sell_price * qty
        cost = cost_price * qty
        gp = revenue - cost

        # Item aggregation
        item = report_rows[s.item]
        item["item"] = s.item
        item["quantity"] += qty
        item["revenue_ex"] += revenue
        item["cost"] += cost
        item["gross_profit"] += gp

        # Category aggregation
        cat = category_rows[category]
        cat["category"] = category
        cat["revenue_ex"] += revenue
        cat["cost"] += cost
        cat["gross_profit"] += gp

    # ================= TOTALS =================
    total_revenue_ex = sum(r["revenue_ex"] for r in report_rows.values())
    total_cost = sum(r["cost"] for r in report_rows.values())
    total_gp = total_revenue_ex - total_cost

    # 🔹 Revenue GST only (optional display)
    total_revenue_gst = total_revenue_ex * GST_RATE
    total_revenue_inc = total_revenue_ex + total_revenue_gst

    # 📈 Margin
    gp_margin = (
        (total_gp / total_revenue_ex) * 100
        if total_revenue_ex > 0 else Decimal("0")
    )

    stores = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]

    return render_template(
        "reports.html",
        report_type=report_type,
        selected_range=selected_range,
        selected_store=selected_store,

        # Dates
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),

        stores=stores,

        # Item GP
        rows=report_rows.values(),

        # Category GP
        category_rows=category_rows.values(),

        # Totals
        total_revenue_ex=total_revenue_ex,
        total_cost=total_cost,
        total_gp=total_gp,

        # Revenue GST (display only)
        total_revenue_gst=total_revenue_gst,
        total_revenue_inc=total_revenue_inc,

        gp_margin=gp_margin
    )


@app.route("/admin/export_data", methods=["GET", "POST"])
@login_required
def export_data():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    stores = [
        row[0] for row in db.session.query(StockOutRecord.store)
        .distinct()
        .filter(StockOutRecord.store.isnot(None))
        .order_by(StockOutRecord.store.asc())
        .all()
    ]
    categories = Category.query.order_by(Category.name.asc()).all()
    suppliers = Supplier.query.order_by(Supplier.name.asc()).all()

    column_options = [
        ("date", "Date"),
        ("store", "Store"),
        ("item_name", "Item Name"),
        ("quantity", "Quantity"),
        ("unit", "Unit"),
        ("supplier", "Supplier"),
        ("category", "Category"),
        ("price", "Cost Price"),
        ("selling_price", "Selling Price"),
        ("total_cost", "Total Cost"),
        ("total_revenue", "Total Revenue")
    ]

    if request.method == "POST":
        selected_columns = request.form.getlist("columns")
        selected_store = request.form.get("store") or ""
        selected_category = request.form.get("category") or ""
        selected_supplier = request.form.get("supplier") or ""
        start_date = request.form.get("start_date") or ""
        end_date = request.form.get("end_date") or ""

        if not selected_columns:
            flash("Please select at least one column to export.", "warning")
            return redirect(url_for("export_data"))

        query = db.session.query(StockOutRecord, Ingredient).outerjoin(
            Ingredient, Ingredient.name == StockOutRecord.item
        )

        if selected_store:
            query = query.filter(StockOutRecord.store == selected_store)

        if start_date and end_date:
            query = query.filter(StockOutRecord.date.between(start_date, end_date))

        if selected_category:
            query = query.filter(Ingredient.category == selected_category)

        if selected_supplier:
            query = query.filter(Ingredient.supplier == selected_supplier)

        rows = query.order_by(StockOutRecord.date.desc()).all()

        def fmt(value, places=2):
            if value is None:
                return ""
            try:
                return f"{Decimal(str(value)):.{places}f}"
            except (InvalidOperation, ValueError):
                return str(value)

        output = StringIO()
        writer = csv.writer(output)

        header_labels = {key: label for key, label in column_options}
        writer.writerow([header_labels.get(col, col) for col in selected_columns])

        for record, ingredient in rows:
            quantity = Decimal(str(record.quantity or 0))
            price = Decimal(str(record.price or 0))
            selling_price = Decimal(str(record.selling_price or 0))

            data = {
                "date": record.date,
                "store": record.store,
                "item_name": record.item,
                "quantity": fmt(quantity, 2),
                "unit": ingredient.unit if ingredient else "",
                "supplier": ingredient.supplier if ingredient else "",
                "category": ingredient.category if ingredient else "",
                "price": fmt(price, 2),
                "selling_price": fmt(selling_price, 2),
                "total_cost": fmt(quantity * price, 2),
                "total_revenue": fmt(quantity * selling_price, 2)
            }

            writer.writerow([data.get(col, "") for col in selected_columns])

        csv_bytes = output.getvalue().encode("utf-8")
        buffer = BytesIO(csv_bytes)
        buffer.seek(0)

        filename = f"stock_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return send_file(
            buffer,
            mimetype="text/csv",
            as_attachment=True,
            download_name=filename
        )

    return render_template(
        "export_data.html",
        stores=stores,
        categories=categories,
        suppliers=suppliers,
        column_options=column_options
    )

@app.route("/admin/variance_report", methods=["GET"])
@login_required
def variance_report():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    stores = User.query.filter_by(role="user").order_by(User.username.asc()).all()
    selected_store = request.args.get("store")
    if not selected_store and stores:
        selected_store = str(stores[0].id)

    report_rows = []
    total_variance_value = Decimal("0")
    weekly_customized = False

    if selected_store:
        try:
            store_id = int(selected_store)
        except ValueError:
            store_id = None

        if store_id:
            store_items = StoreWeeklyItem.query.filter_by(
                store_id=store_id,
                enabled=True
            ).order_by(StoreWeeklyItem.order_position.asc()).all()

            ingredient_ids = []
            if store_items:
                weekly_customized = True
                ingredient_ids = [item.ingredient_id for item in store_items]
            else:
                ingredient_ids = [
                    ingredient.id
                    for ingredient in Ingredient.query.filter_by(weekly_stocktake=True)
                    .order_by(Ingredient.weekly_order_position.asc())
                    .all()
                ]

            ingredients = Ingredient.query.filter(Ingredient.id.in_(ingredient_ids)).all()
            ingredient_map = {ingredient.id: ingredient for ingredient in ingredients}

            for ingredient_id in ingredient_ids:
                ingredient = ingredient_map.get(ingredient_id)
                if not ingredient:
                    continue
                if ingredient.measurement_type == "binary":
                    continue

                latest_stocktake = Stocktake.query.filter_by(
                    user_id=store_id,
                    ingredient_id=ingredient.id,
                    stocktake_type="weekly"
                ).order_by(Stocktake.date_recorded.desc()).first()

                baseline_qty = None
                baseline_date = None
                if latest_stocktake:
                    baseline_date = latest_stocktake.date_recorded
                    try:
                        baseline_qty = Decimal(str(latest_stocktake.quantity_on_hand))
                    except (InvalidOperation, ValueError):
                        baseline_qty = None

                ledger_sum = Decimal("0")
                if baseline_qty is not None:
                    ledger_sum = db.session.query(
                        func.coalesce(func.sum(InventoryLedger.qty_delta), 0)
                    ).filter(
                        InventoryLedger.store_id == store_id,
                        InventoryLedger.ingredient_id == ingredient.id,
                        InventoryLedger.occurred_at >= baseline_date
                    ).scalar() or Decimal("0")

                store_inventory = StoreInventory.query.filter_by(
                    store_id=store_id,
                    ingredient_id=ingredient.id
                ).first()

                actual_qty = Decimal(str(store_inventory.quantity)) if store_inventory and store_inventory.quantity is not None else Decimal("0")

                theoretical_qty = None
                variance_qty = None
                variance_value = None

                if baseline_qty is not None:
                    theoretical_qty = baseline_qty + ledger_sum
                    variance_qty = actual_qty - theoretical_qty
                    price = Decimal(str(ingredient.price_per_unit or 0))
                    variance_value = variance_qty * price
                    total_variance_value += variance_value

                report_rows.append({
                    "ingredient": ingredient.name,
                    "actual_qty": actual_qty,
                    "theoretical_qty": theoretical_qty,
                    "variance_qty": variance_qty,
                    "variance_value": variance_value,
                    "baseline_date": baseline_date
                })

    return render_template(
        "variance_report.html",
        stores=stores,
        selected_store=selected_store,
        rows=report_rows,
        weekly_customized=weekly_customized,
        total_variance_value=total_variance_value
    )

@app.route("/admin/stock_usage", methods=["GET"])
@login_required
def stock_usage_report():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    stores = User.query.filter_by(role="user").all()
    selected_store = request.args.get("store", "all")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    if not start_date or not end_date:
        today = datetime.utcnow().date()
        start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
    except ValueError:
        flash("Invalid date range.", "danger")
        return redirect(url_for("stock_usage_report"))

    query = db.session.query(
        User.username.label("store_name"),
        Ingredient.id.label("ingredient_id"),
        Ingredient.name.label("ingredient_name"),
        Ingredient.unit,
        Ingredient.grams_per_unit,
        Ingredient.supplier,
        Ingredient.price_per_unit,
        Ingredient.selling_price,
        func.coalesce(func.sum(InventoryLedger.qty_delta), 0).label("qty_delta")
    ).join(User, InventoryLedger.store_id == User.id) \
     .join(Ingredient, InventoryLedger.ingredient_id == Ingredient.id) \
     .filter(InventoryLedger.reason.in_(["SALE", "REFUND"])) \
     .filter(InventoryLedger.occurred_at >= start_dt) \
     .filter(InventoryLedger.occurred_at <= end_dt)

    if selected_store != "all":
        query = query.filter(User.id == selected_store)

    query = query.group_by(
        User.username,
        Ingredient.id,
        Ingredient.name,
        Ingredient.unit,
        Ingredient.grams_per_unit,
        Ingredient.supplier,
        Ingredient.price_per_unit,
        Ingredient.selling_price
    )
    raw_rows = query.order_by(User.username.asc(), Ingredient.name.asc()).all()

    rows = []
    totals_by_store = {}

    for row in raw_rows:
        qty_delta = Decimal(str(row.qty_delta or 0))
        units_used = -qty_delta  # sales are negative, refunds positive
        if units_used <= 0:
            continue

        grams_per_unit = Decimal(str(row.grams_per_unit or 0))
        grams_used = units_used * grams_per_unit if grams_per_unit else None

        unit_price = None
        if selected_store == "all":
            unit_price = Decimal(str(row.selling_price or 0))
        else:
            unit_price = Decimal(str(row.price_per_unit or 0))

        total_cost = units_used * unit_price if unit_price is not None else None

        rows.append({
            "store_name": row.store_name,
            "ingredient_name": row.ingredient_name,
            "unit": row.unit,
            "supplier": row.supplier,
            "units_used": units_used,
            "grams_used": grams_used,
            "unit_price": unit_price,
            "total_cost": total_cost
        })

        totals_by_store.setdefault(row.store_name, Decimal("0"))
        totals_by_store[row.store_name] += units_used

    return render_template(
        "stock_usage_report.html",
        stores=stores,
        selected_store=selected_store,
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        totals_by_store=totals_by_store
    )


@app.route("/quantity_made", methods=["GET", "POST"])
@login_required
def quantity_made():
    if request.method == "POST":
        try:
            recipe_id = request.form.get("recipe_id")
            quantity_made_input = request.form.get("quantity_made")

            if not recipe_id or not quantity_made_input:
                return jsonify({"success": False, "message": "Please select a recipe and enter a quantity."})

            try:
                quantity_made = Decimal(quantity_made_input)
                if quantity_made <= 0:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                return jsonify({"success": False, "message": "Invalid quantity format."})

            recipe = db.session.get(Recipe, recipe_id)
            if not recipe:
                return jsonify({"success": False, "message": "Recipe not found."})

            recipe_ingredients = RecipeIngredient.query.filter_by(recipe_id=recipe.id).all()

            # ✅ First pass — check availability
            for ri in recipe_ingredients:
                stock_ingredient = db.session.get(Ingredient, ri.ingredient_id)
                if not stock_ingredient:
                    return jsonify({"success": False, "message": f"Ingredient ID {ri.ingredient_id} not found."})

                grams_needed = Decimal(str(ri.grams_used)) * quantity_made
                grams_per_unit = Decimal(str(stock_ingredient.grams_per_unit or 1))
                stock_to_deduct = grams_needed / grams_per_unit

                if Decimal(str(stock_ingredient.quantity)) < stock_to_deduct:
                    return jsonify({"success": False, "message": f"Not enough stock for {stock_ingredient.name}."})

            # ✅ Second pass — deduct stock
            for ri in recipe_ingredients:
                stock_ingredient = db.session.get(Ingredient, ri.ingredient_id)
                grams_needed = Decimal(str(ri.grams_used)) * quantity_made
                grams_per_unit = Decimal(str(stock_ingredient.grams_per_unit or 1))
                stock_to_deduct = grams_needed / grams_per_unit
                stock_ingredient.quantity = Decimal(str(stock_ingredient.quantity)) - stock_to_deduct
                db.session.merge(stock_ingredient)

            # ✅ Increase output item
            output_item = db.session.get(Ingredient, recipe.output_item_id)
            if not output_item:
                return jsonify({"success": False, "message": "Output item not found."})
            output_item.quantity = Decimal(str(output_item.quantity)) + quantity_made
            db.session.merge(output_item)

            db.session.commit()
            return jsonify({"success": True, "message": "✅ Quantity made successfully recorded!"})

        except Exception as e:
            db.session.rollback()
            print("❌ Quantity Made Error:", str(e))
            traceback.print_exc()
            return jsonify({"success": False, "message": "An unexpected error occurred."})

    # GET method
    recipes = Recipe.query.all()
    ingredients = Ingredient.query.filter(Ingredient.is_archived == False).all()
    return render_template("quantity_made.html", recipes=recipes, ingredients=ingredients)

@app.route("/edit_recipe/<int:recipe_id>", methods=["GET", "POST"])
@login_required
def edit_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)  # Fetch the recipe or return 404 if not found

    if request.method == "POST":
        output_item_id = request.form.get("output_item")
        ingredient_ids = request.form.getlist("ingredient_id[]")
        grams_used_list = request.form.getlist("grams_used[]")

        # ✅ Validate inputs
        if not output_item_id or not ingredient_ids or not grams_used_list:
            flash("Output item and at least one ingredient are required.", "danger")
            return redirect(url_for("edit_recipe", recipe_id=recipe_id))

        try:
            valid_ingredients = [
                (int(ingredient_ids[i]), float(grams_used_list[i]))
                for i in range(len(ingredient_ids))
                if ingredient_ids[i] and grams_used_list[i] and float(grams_used_list[i]) > 0
            ]
        except ValueError:
            flash("Invalid ingredient quantity entered.", "danger")
            return redirect(url_for("edit_recipe", recipe_id=recipe_id))

        # ✅ Update Recipe Output Item (Prevent Duplication)
        if recipe.output_item_id != int(output_item_id):
            recipe.output_item_id = int(output_item_id)

        # ✅ Remove ingredients that are no longer part of the recipe
        RecipeIngredient.query.filter(
            RecipeIngredient.recipe_id == recipe.id,
            RecipeIngredient.ingredient_id.notin_([i[0] for i in valid_ingredients])
        ).delete()

        # ✅ Update existing ingredients or add new ones
        for ingredient_id, grams_used in valid_ingredients:
            existing_entry = RecipeIngredient.query.filter_by(
                recipe_id=recipe.id, ingredient_id=ingredient_id
            ).first()

            if existing_entry:
                existing_entry.grams_used = grams_used  # ✅ Update grams used
            else:
                new_ingredient = RecipeIngredient(
                    recipe_id=recipe.id, ingredient_id=ingredient_id, grams_used=grams_used
                )
                db.session.add(new_ingredient)

        db.session.commit()
        flash("Recipe updated successfully!", "success")
        return redirect(url_for("manage_recipes"))

    # ✅ Load Recipe Ingredients for Editing
    recipe_ingredients = RecipeIngredient.query.filter_by(recipe_id=recipe.id).all()
    ingredients = Ingredient.query.filter(Ingredient.is_archived == False).all()

    return render_template("edit_recipe.html", recipe=recipe, recipe_ingredients=recipe_ingredients, ingredients=ingredients)

@app.route("/delete_recipe/<int:recipe_id>", methods=["POST"])
@login_required
def delete_recipe(recipe_id):
    recipe = db.session.query(Recipe).get(recipe_id)  # ✅ Query explicitly to avoid session conflict

    if not recipe:
        print(f"❌ Recipe ID {recipe_id} not found.")
        return jsonify({"success": False, "message": "Recipe not found."})

    try:
        # ✅ Log recipe details
        print(f"📝 Deleting Recipe ID {recipe_id} with output item ID {recipe.output_item_id}")

        # ✅ First, delete associated recipe ingredients
        deleted_ingredients = db.session.query(RecipeIngredient).filter_by(recipe_id=recipe_id).delete()
        print(f"📝 Deleted {deleted_ingredients} related ingredients.")

        # ✅ Remove recipe from old session if needed
        if db.session.object_session(recipe) is not None:
            db.session.expunge(recipe)  # ✅ Expunge recipe from the old session

        # ✅ Re-add the recipe to the new session before deleting
        db.session.add(recipe)
        db.session.delete(recipe)

        # ✅ Commit changes and refresh the session
        db.session.commit()
        db.session.flush()
        db.session.expire_all()
        db.session.close()

        print(f"✅ Recipe ID {recipe_id} and its ingredients deleted successfully.")
        return jsonify({"success": True, "message": "Recipe deleted successfully!"})

    except Exception as e:
        db.session.rollback()  # Rollback in case of failure
        print(f"❌ Database error: {e}")  # Log error in Flask logs
        return jsonify({"success": False, "message": "Error deleting recipe. Please try again."})

@app.route("/need_to_buy", methods=["GET"])
@login_required
def need_to_buy():
    # ✅ Get selected supplier from request parameters
    selected_supplier = request.args.get("supplier", "")

    # ✅ Query ingredients using the correct supplier field (supplier ID)
    query = """
        SELECT i.name, i.quantity, i.threshold, i.supplier
        FROM ingredient i
    """
    result = db.session.execute(query).fetchall()

    # ✅ Create a list to store items that need to be purchased
    items_to_buy = []
    suppliers = set()  # ✅ Store unique supplier IDs for filtering

    for row in result:
        name, quantity, threshold, supplier = row
        threshold = threshold if threshold is not None else 0
        quantity = quantity if quantity is not None else 0
        supplier = supplier if supplier is not None else "Unknown"  # ✅ Ensure a fallback name

        stock_difference = threshold - quantity

        # ✅ Add supplier to the dropdown list (use supplier ID)
        if supplier and supplier != "Unknown":
            suppliers.add(str(supplier))  # Store supplier ID as string

        # ✅ Only show items that need to be bought
        if stock_difference > 0:
            if selected_supplier and selected_supplier != "All Suppliers" and str(supplier) != selected_supplier:
                continue  # ✅ Skip items that don’t match the selected supplier

            items_to_buy.append({
                "name": name,
                "current_stock": quantity,
                "threshold": threshold,
                "need_to_buy": stock_difference,
                "supplier": supplier  # ✅ Show supplier ID
            })

    return render_template(
        "need_to_buy.html",
        items_to_buy=items_to_buy,
        suppliers=sorted(suppliers),
        selected_supplier=selected_supplier
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            if user.role == 'admin':
                return redirect(url_for('dashboard'))  # Full access
            else:
                return redirect(url_for('blank_page'))  # Restricted users go to a blank page
        else:
            flash('Invalid username or password', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/blank_page')
@login_required
def blank_page():
    if current_user.role != "user":
        return redirect(url_for("index"))

    user_id = current_user.id

    daily_rows = db.session.query(
        Ingredient,
        StoreInventory.quantity.label("store_quantity")
    ).outerjoin(
        StoreInventory,
        and_(
            StoreInventory.ingredient_id == Ingredient.id,
            StoreInventory.store_id == user_id
        )
    ).filter(
        Ingredient.daily_stocktake == True
    ).order_by(Ingredient.order_position.asc()).all()

    weekly_customized = False
    weekly_rows = []

    store_items = StoreWeeklyItem.query.filter_by(
        store_id=user_id,
        enabled=True
    ).order_by(StoreWeeklyItem.order_position.asc()).all()

    if store_items:
        weekly_customized = True
        ingredient_ids = [item.ingredient_id for item in store_items]
        ingredient_rows = db.session.query(
            Ingredient,
            StoreInventory.quantity.label("store_quantity")
        ).outerjoin(
            StoreInventory,
            and_(
                StoreInventory.ingredient_id == Ingredient.id,
                StoreInventory.store_id == user_id
            )
        ).filter(
            Ingredient.id.in_(ingredient_ids)
        ).all()

        ingredient_map = {ingredient.id: (ingredient, store_qty) for ingredient, store_qty in ingredient_rows}
        for ingredient_id in ingredient_ids:
            row = ingredient_map.get(ingredient_id)
            if row:
                weekly_rows.append(row)
    else:
        weekly_rows = db.session.query(
            Ingredient,
            StoreInventory.quantity.label("store_quantity")
        ).outerjoin(
            StoreInventory,
            and_(
                StoreInventory.ingredient_id == Ingredient.id,
                StoreInventory.store_id == user_id
            )
        ).filter(
            Ingredient.weekly_stocktake == True
        ).order_by(Ingredient.weekly_order_position.asc()).all()

        weekly_rows = weekly_rows

    daily_map = {}
    for ingredient, store_qty in daily_rows:
        daily_map[ingredient.id] = {
            "id": ingredient.id,
            "name": ingredient.name,
            "unit": ingredient.unit,
            "category": ingredient.category,
            "supplier": ingredient.supplier,
            "quantity": store_qty,
            "daily": True,
            "weekly": False
        }

    combined = []
    for ingredient_id, item in daily_map.items():
        combined.append(item)

    for ingredient, store_qty in weekly_rows:
        existing = daily_map.get(ingredient.id)
        if existing:
            existing["weekly"] = True
            if existing["quantity"] is None:
                existing["quantity"] = store_qty
        else:
            combined.append({
                "id": ingredient.id,
                "name": ingredient.name,
                "unit": ingredient.unit,
                "category": ingredient.category,
                "supplier": ingredient.supplier,
                "quantity": store_qty,
                "daily": False,
                "weekly": True
            })

    return render_template(
        "blank.html",
        items=combined,
        weekly_customized=weekly_customized
    )

@app.route("/stocktake/<stocktake_type>", methods=["GET", "POST"])
@login_required
def stocktake(stocktake_type):
    """Handles daily and weekly stocktake submissions for users."""

    # ✅ Ensure only users (stores) can access stocktake
    if current_user.role != "user":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    user_id = current_user.id
    stocktake_entries = []

    # ✅ Filter ingredients based on stocktake type & enforce custom ordering
    if stocktake_type == "daily":
        ingredients = Ingredient.query.filter_by(daily_stocktake=True).order_by(Ingredient.order_position.asc()).all()
        stocktake_entries = build_default_daily_section_entries(ingredients)
    elif stocktake_type == "weekly":
        store_items = StoreWeeklyItem.query.filter_by(
            store_id=user_id,
            enabled=True
        ).order_by(StoreWeeklyItem.order_position.asc()).all()

        if store_items:
            ingredient_ids = [item.ingredient_id for item in store_items]
            ingredient_rows = Ingredient.query.filter(Ingredient.id.in_(ingredient_ids)).all()
            ingredient_map = {ingredient.id: ingredient for ingredient in ingredient_rows}
            ingredients = [
                ingredient_map[item_id]
                for item_id in ingredient_ids
                if item_id in ingredient_map
            ]
            stocktake_entries = build_weekly_section_entries(store_items, ingredient_map)
        else:
            ingredients = Ingredient.query.filter_by(weekly_stocktake=True).order_by(Ingredient.weekly_order_position.asc()).all()
            stocktake_entries = build_default_weekly_section_entries(ingredients)
    else:
        flash("Invalid stocktake type.", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        updated_stocktakes = []  # ✅ Store updated entries

        for ingredient in ingredients:
            quantity = request.form.get(f"quantity_{ingredient.id}")

            # ✅ Handle binary stocktake (dropdown selection)
            if ingredient.measurement_type == "binary":
                if quantity == "Enough in store":
                    recorded_value = "Enough in store"
                elif quantity == "Not enough in store":
                    recorded_value = "Not enough in store"
                else:
                    recorded_value = None  # User didn't select an option
            else:
                # ✅ Handle numeric stocktake
                try:
                    recorded_value = float(quantity)
                except (ValueError, TypeError):
                    flash(f"Invalid quantity for {ingredient.name}.", "danger")
                    return redirect(url_for("stocktake", stocktake_type=stocktake_type))

            if recorded_value is not None:
                # ✅ Check if a stocktake entry for today already exists
                today = datetime.utcnow().date()
                existing_stocktake = Stocktake.query.filter(
                    Stocktake.user_id == user_id,
                    Stocktake.ingredient_id == ingredient.id,
                    Stocktake.stocktake_type == stocktake_type
                ).order_by(Stocktake.date_recorded.desc()).first()

                if existing_stocktake and existing_stocktake.date_recorded.date() == today:
                    # ✅ Update existing entry instead of inserting a new one
                    existing_stocktake.quantity_on_hand = recorded_value
                    updated_stocktakes.append(existing_stocktake)
                else:
                    # ✅ Create a new stocktake entry
                    new_stock = Stocktake(
                        user_id=user_id,
                        ingredient_id=ingredient.id,
                        quantity_on_hand=recorded_value,
                        stocktake_type=stocktake_type,
                        date_recorded=datetime.utcnow()
                    )
                    db.session.add(new_stock)

                # ✅ For weekly stocktake, update store inventory (numeric items only)
                if stocktake_type == "weekly" and ingredient.measurement_type != "binary":
                    new_qty = Decimal(str(recorded_value))
                    store_inventory = StoreInventory.query.filter_by(
                        store_id=user_id,
                        ingredient_id=ingredient.id
                    ).first()

                    if not store_inventory:
                        store_inventory = StoreInventory(
                            store_id=user_id,
                            ingredient_id=ingredient.id,
                            quantity=new_qty
                        )
                        db.session.add(store_inventory)
                    else:
                        store_inventory.quantity = new_qty

        # ✅ Commit changes to the database
        db.session.commit()
        flash(f"{stocktake_type.capitalize()} Stocktake Recorded!", "success")

        return redirect(url_for("stocktake", stocktake_type=stocktake_type))

    return render_template(
        "stocktake.html",
        ingredients=ingredients,
        stocktake_entries=stocktake_entries,
        stocktake_type=stocktake_type
    )

@app.route("/admin/stocktake", methods=["GET"])
@login_required
def admin_stocktake():
    # ✅ Ensure only admins can access
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    # ✅ Get all stores for the dropdown
    stores = User.query.filter_by(role="user").all()

    # ✅ Get filter values from request
    selected_date = request.args.get("date")
    selected_store = request.args.get("store")
    stocktake_type = request.args.get("stocktake_type", "daily")  # ✅ Default to daily

    # ✅ If no date is selected, default to today
    if not selected_date:
        selected_date = datetime.utcnow().strftime('%Y-%m-%d')

    query = db.session.query(
        Stocktake.id,
        User.username.label("store_name"),
        Ingredient.name.label("ingredient_name"),
        Ingredient.measurement_type,
        Stocktake.quantity_on_hand,
        Stocktake.stocktake_type,
        Stocktake.date_recorded
    ).join(User, Stocktake.user_id == User.id) \
     .join(Ingredient, Stocktake.ingredient_id == Ingredient.id) \
     .filter(db.func.date(Stocktake.date_recorded) == selected_date)

    # ✅ Apply stocktake type filter (daily or weekly)
    if stocktake_type == "daily":
        query = query.filter(Stocktake.stocktake_type == "daily") \
                     .order_by(Ingredient.order_position.asc())  # ✅ Use the same ordering as the user side
    elif stocktake_type == "weekly":
        query = query.filter(Stocktake.stocktake_type == "weekly")

        if selected_store and selected_store != "all":
            query = query.outerjoin(
                StoreWeeklyItem,
                and_(
                    StoreWeeklyItem.store_id == Stocktake.user_id,
                    StoreWeeklyItem.ingredient_id == Stocktake.ingredient_id
                )
            ).order_by(func.coalesce(StoreWeeklyItem.order_position, 9999))
        else:
            query = query.order_by(Ingredient.weekly_order_position.asc())  # ✅ Use weekly order position

    # ✅ Apply store filter if selected
    if selected_store and selected_store != "all":
        query = query.filter(Stocktake.user_id == selected_store)

    # ✅ Order by latest stocktake records
    stocktakes = query.all()

    return render_template("admin_stocktake.html",
                           stocktakes=stocktakes,
                           stores=stores,
                           selected_date=selected_date,
                           selected_store=selected_store,
                           stocktake_type=stocktake_type)  # ✅ Pass stocktake type to template

@app.route("/admin/weekly_stocktake", methods=["GET"])
@login_required
def admin_weekly_stocktake():
    """Admin view for weekly stocktake, ordered correctly and filtered properly."""

    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    stores = User.query.filter_by(role="user").all()
    selected_date = request.args.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
    selected_store = request.args.get("store", "all")

    try:
        selected_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except ValueError:
        flash("Invalid date format.", "danger")
        return redirect(url_for("admin_weekly_stocktake"))

    # ✅ Build base query
    query = db.session.query(
        Stocktake.id,
        User.username.label("store_name"),
        Ingredient.name.label("ingredient_name"),
        Stocktake.quantity_on_hand,
        StoreThreshold.threshold,
        Stocktake.date_recorded,
        Ingredient.weekly_order_position,
        StoreWeeklyItem.order_position.label("store_weekly_order")
    ).join(User, Stocktake.user_id == User.id) \
     .join(Ingredient, Stocktake.ingredient_id == Ingredient.id) \
     .outerjoin(StoreWeeklyItem, (StoreWeeklyItem.store_id == Stocktake.user_id) & (StoreWeeklyItem.ingredient_id == Stocktake.ingredient_id)) \
     .outerjoin(StoreThreshold, (StoreThreshold.store_id == Stocktake.user_id) & (StoreThreshold.ingredient_id == Stocktake.ingredient_id)) \
     .filter(Stocktake.stocktake_type == "weekly") \
     .filter(func.date(Stocktake.date_recorded) == selected_date_obj)

    if selected_store != "all":
        query = query.filter(Stocktake.user_id == selected_store)

    if selected_store != "all":
        query = query.order_by(func.coalesce(StoreWeeklyItem.order_position, Ingredient.weekly_order_position, 9999))
    else:
        query = query.order_by(Ingredient.weekly_order_position.asc())
    raw_stocktakes = query.limit(100).all()

    # ✅ Format stocktakes
    stocktakes = []
    for stock in raw_stocktakes:
        raw_quantity = stock.quantity_on_hand
        threshold = stock.threshold if stock.threshold is not None else 0

        # Try parsing quantity intelligently
        try:
            # Check if it's a numeric string
            if isinstance(raw_quantity, str) and raw_quantity.replace(".", "", 1).isdigit():
                numeric_quantity = float(raw_quantity)
                display_quantity = f"{numeric_quantity:.1f}"
            elif isinstance(raw_quantity, (int, float)):
                numeric_quantity = float(raw_quantity)
                display_quantity = f"{numeric_quantity:.1f}"
            elif raw_quantity == "Enough in store":
                numeric_quantity = 0
                display_quantity = "Enough in store"
            elif raw_quantity == "Not enough in store":
                numeric_quantity = 0
                display_quantity = "Not enough in store"
            else:
                numeric_quantity = 0
                display_quantity = "Invalid"
        except:
            numeric_quantity = 0
            display_quantity = "Invalid"

        # Needed stock logic
        if display_quantity == "Not enough in store":
            needed_stock = 1
        elif display_quantity == "Invalid":
            needed_stock = 0
        else:
            needed_stock = max(threshold - numeric_quantity, 0)

        stocktakes.append({
            "id": stock.id,
            "store_name": stock.store_name,
            "ingredient_name": stock.ingredient_name,
            "quantity_on_hand": display_quantity,
            "threshold": threshold,
            "needed_stock": needed_stock,
            "date_recorded": stock.date_recorded.strftime("%Y-%m-%d")
        })

    return render_template(
        "admin_weekly_stocktake.html",
        stocktakes=stocktakes,
        stores=stores,
        selected_date=selected_date,
        selected_store=selected_store
    )

@app.route("/admin/manage_thresholds", methods=["GET", "POST"])
@login_required
def manage_thresholds():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    # ✅ Get all stores
    stores = User.query.filter_by(role="user").all()

    # ✅ Store filter for better usability
    selected_store = request.args.get("store_id", "all")

    ingredients = []
    use_store_items = False
    if selected_store != "all":
        store_items = StoreWeeklyItem.query.filter_by(
            store_id=selected_store,
            enabled=True
        ).order_by(StoreWeeklyItem.order_position.asc()).all()
        if store_items:
            use_store_items = True
            ingredient_ids = [item.ingredient_id for item in store_items]
            ingredient_rows = Ingredient.query.filter(Ingredient.id.in_(ingredient_ids)).all()
            ingredient_map = {ingredient.id: ingredient for ingredient in ingredient_rows}
            ingredients = [
                ingredient_map[item_id]
                for item_id in ingredient_ids
                if item_id in ingredient_map
            ]
        else:
            ingredients = Ingredient.query.filter_by(weekly_stocktake=True, is_archived=False).all()
    else:
        ingredients = Ingredient.query.filter_by(weekly_stocktake=True, is_archived=False).all()

    if request.method == "POST":
        store_id = request.form.get("store_id")
        ingredient_id = request.form.get("ingredient_id")
        threshold = request.form.get("threshold")

        if store_id and ingredient_id and threshold:
            # ✅ Check if the threshold already exists
            existing_threshold = db.session.execute(
                text("SELECT id FROM store_thresholds WHERE store_id = :store_id AND ingredient_id = :ingredient_id"),
                {"store_id": store_id, "ingredient_id": ingredient_id}
            ).fetchone()

            if existing_threshold:
                flash("Threshold for this store and ingredient already exists!", "warning")
            else:
                # ✅ Insert a new threshold
                db.session.execute(
                    text("INSERT INTO store_thresholds (store_id, ingredient_id, threshold) VALUES (:store_id, :ingredient_id, :threshold)"),
                    {"store_id": store_id, "ingredient_id": ingredient_id, "threshold": threshold}
                )
                db.session.commit()
                flash("Threshold added successfully!", "success")

    # ✅ Fetch thresholds, optionally filtering by store
    query = """
        SELECT st.id, st.store_id, st.ingredient_id, st.threshold, u.username AS store_name, i.name AS ingredient_name
        FROM store_thresholds st
        JOIN user u ON st.store_id = u.id
        JOIN ingredient i ON st.ingredient_id = i.id
    """
    params = {}

    if selected_store != "all":
        query += " WHERE st.store_id = :store_id"
        params["store_id"] = selected_store

    thresholds = db.session.execute(text(query), params).mappings().all()
    if use_store_items:
        allowed_ids = {ingredient.id for ingredient in ingredients}
        thresholds = [row for row in thresholds if row["ingredient_id"] in allowed_ids]

    return render_template(
        "admin_manage_thresholds.html",
        stores=stores,
        ingredients=ingredients,
        thresholds=thresholds,
        selected_store=selected_store
    )

@app.route("/admin/update_threshold/<int:id>", methods=["POST"])
@login_required
def update_threshold(id):
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    new_threshold = request.form.get("new_threshold")

    if new_threshold:
        db.session.execute(
            text("UPDATE store_thresholds SET threshold = :threshold WHERE id = :id"),
            {"threshold": new_threshold, "id": id}
        )
        db.session.commit()
        flash("Threshold updated successfully!", "success")

    return redirect(url_for("manage_thresholds"))

@app.route("/admin/delete_threshold/<int:id>", methods=["POST"])
@login_required
def delete_threshold(id):
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    db.session.execute(text("DELETE FROM store_thresholds WHERE id = :id"), {"id": id})
    db.session.commit()
    flash("Threshold deleted successfully!", "danger")

    return redirect(url_for("manage_thresholds"))

@app.route("/admin/update_stocktake_order", methods=["POST"])
@login_required
def update_stocktake_order():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    payload = request.json or {}
    order_data = payload.get("order")
    enabled_entries = payload.get("enabled_entries")

    enabled_ids, section_by_ingredient = parse_weekly_enabled_entries(enabled_entries, order_data)

    if enabled_ids:
        ingredient_rows = Ingredient.query.filter(Ingredient.id.in_(enabled_ids)).all()
        ingredient_map = {ingredient.id: ingredient for ingredient in ingredient_rows}

        for index, ingredient_id in enumerate(enabled_ids):
            ingredient = ingredient_map.get(ingredient_id)
            if not ingredient:
                continue
            ingredient.order_position = index
            ingredient.daily_section_name = section_by_ingredient.get(ingredient_id)
        db.session.commit()
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "No order data received"})

@app.route("/admin/manage_stocktake_order", methods=["GET"])
@login_required
def manage_stocktake_order():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    # Fetch ingredients ordered by `order_position`
    ingredients = Ingredient.query.filter_by(daily_stocktake=True).order_by(Ingredient.order_position.asc()).all()

    stocktake_entries = build_default_daily_section_entries(ingredients)

    return render_template(
        "admin_manage_stocktake_order.html",
        ingredients=ingredients,
        stocktake_entries=stocktake_entries
    )

@app.route("/admin/manage_weekly_stocktake_order", methods=["GET"])
@login_required
def manage_weekly_stocktake_order():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    stores = User.query.filter_by(role="user").all()
    selected_store = request.args.get("store_id")

    if not selected_store:
        selected_store = "all"

    all_ingredients = Ingredient.query.order_by(Ingredient.name.asc()).all()
    enabled_entries = []
    enabled_ingredients = []
    disabled_ingredients = []

    store_items = []
    if selected_store and selected_store != "all":
        store_items = StoreWeeklyItem.query.filter_by(store_id=selected_store).all()

    if store_items:
        enabled_items = sorted(
            [item for item in store_items if item.enabled],
            key=lambda item: item.order_position
        )
        enabled_ids = [item.ingredient_id for item in enabled_items]
        ingredient_map = {ingredient.id: ingredient for ingredient in all_ingredients}
        enabled_ingredients = [
            ingredient_map[ingredient_id]
            for ingredient_id in enabled_ids
            if ingredient_id in ingredient_map
        ]
        enabled_entries = build_weekly_section_entries(enabled_items, ingredient_map)
        disabled_ingredients = [
            ingredient for ingredient in all_ingredients
            if ingredient.id not in set(enabled_ids)
        ]
    else:
        enabled_ingredients = Ingredient.query.filter_by(
            weekly_stocktake=True
        ).order_by(Ingredient.weekly_order_position.asc()).all()
        enabled_ids = {ingredient.id for ingredient in enabled_ingredients}
        enabled_entries = build_default_weekly_section_entries(enabled_ingredients)
        disabled_ingredients = [
            ingredient for ingredient in all_ingredients
            if ingredient.id not in enabled_ids
        ]

    return render_template(
        "admin_manage_weekly_stocktake_order.html",
        stores=stores,
        selected_store=selected_store,
        enabled_entries=enabled_entries,
        enabled_ingredients=enabled_ingredients,
        disabled_ingredients=disabled_ingredients
    )

@app.route("/admin/update_weekly_stocktake_order", methods=["POST"])
@login_required
def update_weekly_stocktake_order():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    payload = request.json or {}
    order_data = payload.get("enabled_order")
    enabled_entries = payload.get("enabled_entries")
    disabled_data = payload.get("disabled")
    store_id = payload.get("store_id")

    if store_id == "all" and (enabled_entries is not None or order_data is not None) and disabled_data is not None:
        enabled_ids, section_by_ingredient = parse_weekly_enabled_entries(enabled_entries, order_data)
        disabled_ids = [int(item_id) for item_id in disabled_data]
        all_ids = list(set(enabled_ids + disabled_ids))

        if all_ids:
            ingredient_rows = Ingredient.query.filter(Ingredient.id.in_(all_ids)).all()
            ingredient_map = {ingredient.id: ingredient for ingredient in ingredient_rows}

            for index, ingredient_id in enumerate(enabled_ids):
                ingredient = ingredient_map.get(ingredient_id)
                if not ingredient:
                    continue
                ingredient.weekly_stocktake = True
                ingredient.weekly_order_position = index
                ingredient.weekly_section_name = section_by_ingredient.get(ingredient_id)

            for ingredient_id in disabled_ids:
                ingredient = ingredient_map.get(ingredient_id)
                if not ingredient:
                    continue
                ingredient.weekly_stocktake = False
                ingredient.weekly_order_position = 0
                ingredient.weekly_section_name = None

            # Reset store-specific overrides so every store uses the shared weekly order
            StoreWeeklyItem.query.delete()
            db.session.commit()

        return jsonify({"success": True})

    if store_id and (enabled_entries is not None or order_data is not None) and disabled_data is not None:
        try:
            store_id_int = int(store_id)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Invalid store_id"})

        enabled_ids, section_by_ingredient = parse_weekly_enabled_entries(enabled_entries, order_data)
        disabled_ids = [int(item_id) for item_id in disabled_data]
        all_ids = list(set(enabled_ids + disabled_ids))

        existing_rows = StoreWeeklyItem.query.filter(
            StoreWeeklyItem.store_id == store_id_int,
            StoreWeeklyItem.ingredient_id.in_(all_ids)
        ).all()
        existing_map = {row.ingredient_id: row for row in existing_rows}

        for index, ingredient_id in enumerate(enabled_ids):
            row = existing_map.get(ingredient_id)
            if not row:
                row = StoreWeeklyItem(
                    store_id=store_id_int,
                    ingredient_id=ingredient_id
                )
                db.session.add(row)
            row.enabled = True
            row.order_position = index
            row.section_name = section_by_ingredient.get(ingredient_id)

        for ingredient_id in disabled_ids:
            row = existing_map.get(ingredient_id)
            if not row:
                row = StoreWeeklyItem(
                    store_id=store_id_int,
                    ingredient_id=ingredient_id
                )
                db.session.add(row)
            row.enabled = False
            row.order_position = 0
            row.section_name = None

        db.session.commit()
        return jsonify({"success": True})

    if order_data:
        for index, ingredient_id in enumerate(order_data):
            db.session.execute(
                text("UPDATE ingredient SET weekly_order_position = :order WHERE id = :id"),
                {"order": index, "id": ingredient_id}
            )
        db.session.commit()
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "No order data received"})

@app.route("/debug_db", methods=["GET"])
@login_required
def debug_db():
    # ✅ Print current database connection
    print(f"🔍 Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")

    # ✅ Check if Flask is retrieving correct stock
    stock_check = db.session.query(Ingredient.id, Ingredient.name, Ingredient.quantity).all()
    for item in stock_check:
        print(f"📦 Stock: {item.id} - {item.name}: {item.quantity}")

    return jsonify({"success": True})

@app.route("/get_stock_data", methods=["GET"])
@login_required
def get_stock_data():
    ingredients = Ingredient.query.all()
    ingredient_data = [{"id": i.id, "name": i.name, "quantity": float(i.quantity)} for i in ingredients]
    return jsonify({"success": True, "ingredients": ingredient_data})

@app.route("/increase_output", methods=["POST"])
@login_required
def increase_output():
    recipe_id = request.form.get("recipe_id")
    quantity_made = request.form.get("quantity_made")

    if not recipe_id or not quantity_made:
        return jsonify({"success": False, "message": "Please select a recipe and enter a quantity."})

    try:
        quantity_made = Decimal(quantity_made)  # Convert to Decimal for accuracy
    except ValueError:
        return jsonify({"success": False, "message": "Invalid quantity entered."})

    # ✅ Fetch the recipe and validate
    recipe = Recipe.query.get(recipe_id)
    if not recipe:
        return jsonify({"success": False, "message": "Invalid recipe selected."})

    output_item = Ingredient.query.get(recipe.output_item_id)
    if output_item:
        new_output_quantity = Decimal(output_item.quantity) + quantity_made

        # ✅ Force raw SQL execution
        db.session.execute(
            "UPDATE ingredient SET quantity = :new_quantity WHERE id = :id",
            {"new_quantity": new_output_quantity, "id": recipe.output_item_id}
        )

        print(f"✅ Increased {output_item.name} by {quantity_made}. New Quantity: {new_output_quantity}")

        # ✅ Commit & Refresh
        db.session.commit()
        db.session.flush()
        db.session.expire_all()
        db.session.close()

        # ✅ Verify update
        updated_output = db.session.execute(
            "SELECT quantity FROM ingredient WHERE id = :id",
            {"id": recipe.output_item_id}
        ).fetchone()

        print(f"📝 AFTER COMMIT: {output_item.name} - DB Quantity: {updated_output[0]}")

        return jsonify({"success": True, "message": "Output stock updated successfully!"})

    return jsonify({"success": False, "message": "Output item not found."})

@app.route("/deduct_ingredients", methods=["POST"])
@login_required
def deduct_ingredients():
    recipe_id = request.form.get("recipe_id")
    quantity_made = request.form.get("quantity_made")

    if not recipe_id or not quantity_made:
        return jsonify({"success": False, "message": "Please select a recipe and enter a quantity."})

    try:
        quantity_made = Decimal(quantity_made)  # Convert to Decimal for accuracy
    except ValueError:
        return jsonify({"success": False, "message": "Invalid quantity entered."})

    # ✅ Fetch the recipe and validate
    recipe = Recipe.query.get(recipe_id)
    if not recipe:
        return jsonify({"success": False, "message": "Invalid recipe selected."})

    recipe_ingredients = RecipeIngredient.query.filter_by(recipe_id=recipe_id).all()

    for recipe_ingredient in recipe_ingredients:
        ingredient = Ingredient.query.get(recipe_ingredient.ingredient_id)

        if ingredient and ingredient.grams_per_unit > 0:
            total_grams_used = Decimal(recipe_ingredient.grams_used) * quantity_made
            units_to_deduct = total_grams_used / Decimal(ingredient.grams_per_unit)

            new_quantity = Decimal(ingredient.quantity) - units_to_deduct

            if new_quantity >= 0:
                # ✅ Force raw SQL execution to ensure database updates
                db.session.execute(
                    "UPDATE ingredient SET quantity = :new_quantity WHERE id = :id",
                    {"new_quantity": new_quantity, "id": recipe_ingredient.ingredient_id}
                )
                print(f"✅ Deducted {units_to_deduct} from {ingredient.name}. New Quantity: {new_quantity}")
            else:
                print(f"❌ Not enough stock for {ingredient.name}. Needed: {units_to_deduct}, Available: {ingredient.quantity}")
                return jsonify({"success": False, "message": f"Not enough stock for {ingredient.name}!"})

    # ✅ Commit & Refresh
    try:
        db.session.commit()
        db.session.flush()
        db.session.expire_all()
        db.session.close()

        # ✅ Verify the ingredient updates
        for recipe_ingredient in recipe_ingredients:
            updated_ingredient = db.session.execute(
                "SELECT quantity FROM ingredient WHERE id = :id",
                {"id": recipe_ingredient.ingredient_id}
            ).fetchone()

            print(f"📝 AFTER COMMIT: {recipe_ingredient.ingredient_id} - DB Quantity: {updated_ingredient[0]}")

        return jsonify({"success": True, "message": "Ingredients deducted successfully!"})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Database Commit Failed: {e}")
        return jsonify({"success": False, "message": "Database update failed."})

@app.route("/process_quantity_made", methods=["POST"])
@login_required
def process_quantity_made():
    recipe_id = request.form.get("recipe_id")
    quantity_made = request.form.get("quantity_made")

    if not recipe_id or not quantity_made:
        return jsonify({"success": False, "message": "Please select a recipe and enter a quantity."})

    try:
        quantity_made = Decimal(quantity_made)  # Convert to Decimal for accuracy
    except ValueError:
        return jsonify({"success": False, "message": "Invalid quantity entered."})

    # ✅ Fetch the recipe and validate
    recipe = Recipe.query.get(recipe_id)
    if not recipe:
        return jsonify({"success": False, "message": "Invalid recipe selected."})

    recipe_ingredients = RecipeIngredient.query.filter_by(recipe_id=recipe_id).all()

    # ✅ Step 1: Increase Output Item
    output_item = Ingredient.query.get(recipe.output_item_id)
    if output_item:
        new_output_quantity = Decimal(output_item.quantity) + quantity_made
        db.session.execute(
            "UPDATE ingredient SET quantity = :new_quantity WHERE id = :id",
            {"new_quantity": new_output_quantity, "id": recipe.output_item_id}
        )
        print(f"✅ Increased {output_item.name} by {quantity_made}. New Quantity: {new_output_quantity}")

    # ✅ Step 2: Deduct Ingredients
    for recipe_ingredient in recipe_ingredients:
        ingredient = Ingredient.query.get(recipe_ingredient.ingredient_id)

        if ingredient and ingredient.grams_per_unit > 0:
            total_grams_used = Decimal(recipe_ingredient.grams_used) * quantity_made
            units_to_deduct = total_grams_used / Decimal(ingredient.grams_per_unit)

            new_quantity = Decimal(ingredient.quantity) - units_to_deduct

            if new_quantity >= 0:
                db.session.execute(
                    "UPDATE ingredient SET quantity = :new_quantity WHERE id = :id",
                    {"new_quantity": new_quantity, "id": recipe_ingredient.ingredient_id}
                )
                print(f"✅ Deducted {units_to_deduct} from {ingredient.name}. New Quantity: {new_quantity}")
            else:
                print(f"❌ Not enough stock for {ingredient.name}. Needed: {units_to_deduct}, Available: {ingredient.quantity}")
                return jsonify({"success": False, "message": f"Not enough stock for {ingredient.name}!"})

    # ✅ Commit & Refresh
    try:
        db.session.commit()
        db.session.flush()
        db.session.expire_all()
        db.session.close()

        # ✅ Verify the ingredient updates
        updated_output = db.session.execute(
            "SELECT quantity FROM ingredient WHERE id = :id",
            {"id": recipe.output_item_id}
        ).fetchone()

        print(f"📝 AFTER COMMIT: {output_item.name} - DB Quantity: {updated_output[0]}")

        for recipe_ingredient in recipe_ingredients:
            updated_ingredient = db.session.execute(
                "SELECT quantity FROM ingredient WHERE id = :id",
                {"id": recipe_ingredient.ingredient_id}
            ).fetchone()
            print(f"📝 AFTER COMMIT: {recipe_ingredient.ingredient_id} - DB Quantity: {updated_ingredient[0]}")

        return jsonify({"success": True, "message": "Stock updated successfully!"})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Database Commit Failed: {e}")
        return jsonify({"success": False, "message": "Database update failed."})

@app.route("/get_recipe/<int:recipe_id>", methods=["GET"])
@login_required
def get_recipe(recipe_id):
    recipe = Recipe.query.get(recipe_id)
    if not recipe:
        return jsonify({"success": False, "message": "Recipe not found."})

    ingredients = [
        {"id": ri.ingredient_id, "name": Ingredient.query.get(ri.ingredient_id).name, "quantity": ri.grams_used}
        for ri in RecipeIngredient.query.filter_by(recipe_id=recipe_id).all()
    ]

    return jsonify({"success": True, "ingredients": ingredients})

@app.route("/delete_invoice/<string:invoice_no>", methods=["POST"])
@login_required
def delete_invoice(invoice_no):
    try:
        print(f"📝 Attempting to delete Invoice No: {invoice_no}")

        # ✅ Query all records with the given invoice number
        records = db.session.query(StockOutRecord).filter_by(invoice_no=invoice_no).all()

        if not records:
            print(f"❌ Invoice No {invoice_no} not found.")
            return jsonify({"success": False, "message": "Invoice not found."})

        # ✅ Delete all associated records
        for record in records:
            db.session.delete(record)

        db.session.commit()  # ✅ Commit changes after deleting all records

        print(f"✅ Invoice No {invoice_no} and all related records deleted successfully.")
        return jsonify({"success": True, "message": f"Invoice {invoice_no} deleted successfully!"})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error deleting invoice: {e}")  # ✅ Log actual error
        return jsonify({"success": False, "message": "Error deleting invoice. Please try again."})

@app.route("/monthly_stocktake", methods=["GET"])
@login_required
def monthly_stocktake():
    # ✅ Selected date (YYYY-MM-DD)
    selected_date_str = request.args.get(
        "date",
        datetime.utcnow().strftime("%Y-%m-%d")
    )
    selected_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()

    # ✅ All active ingredients
    ingredients = Ingredient.query.filter_by(is_archived=False).order_by(Ingredient.name).all()

    # ✅ Get existing stocktake records for this date
    stocktake_records = MonthlyStocktake.query.filter_by(
        stocktake_date=selected_date
    ).all()

    # ✅ Map by ingredient_id for template lookup
    stocktake_data = {
        record.ingredient_id: record
        for record in stocktake_records
    }

    return render_template(
        "monthly_stocktake.html",
        ingredients=ingredients,
        stocktake_data=stocktake_data,
        selected_date=selected_date.strftime("%Y-%m-%d")
    )


@app.route("/submit_monthly_stocktake", methods=["POST"])
@login_required
def submit_monthly_stocktake():
    payload = request.get_json() or {}
    stocktake_items = payload.get("stocktake", [])
    stocktake_date = payload.get("stocktake_date")  # YYYY-MM-DD

    if not stocktake_date or not stocktake_items:
        return jsonify({"success": False, "message": "Invalid stocktake submission."})

    # ✅ Parse date safely
    try:
        stocktake_date = datetime.strptime(stocktake_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "message": "Invalid stocktake date format."})

    # ✅ Sort items so row locks happen in a consistent order (prevents deadlocks)
    try:
        stocktake_items = sorted(stocktake_items, key=lambda x: int(x["ingredient_id"]))
    except Exception:
        return jsonify({"success": False, "message": "Invalid stocktake payload."})

    # ✅ Retry deadlocks a few times
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with db.session.no_autoflush:

                for entry in stocktake_items:
                    ingredient_id = int(entry["ingredient_id"])
                    counted_quantity = Decimal(str(entry["counted_quantity"]))

                    # 🔒 Lock the ingredient row so nobody else can update it mid-stocktake
                    ingredient = (
                        db.session.query(Ingredient)
                        .filter(Ingredient.id == ingredient_id)
                        .with_for_update()
                        .first()
                    )
                    if not ingredient:
                        continue

                    # 🔒 Check duplicate with lock-consistent order
                    exists = (
                        db.session.query(MonthlyStocktake)
                        .filter(
                            MonthlyStocktake.ingredient_id == ingredient_id,
                            MonthlyStocktake.stocktake_date == stocktake_date
                        )
                        .first()
                    )
                    if exists:
                        return jsonify({
                            "success": False,
                            "message": f"Stocktake already exists for {ingredient.name} on {stocktake_date}."
                        })

                    # 📸 SNAPSHOT BEFORE CHANGE
                    previous_quantity = Decimal(str(ingredient.quantity or 0))
                    price_per_unit = Decimal(str(ingredient.price_per_unit or 0))

                    variance_quantity = counted_quantity - previous_quantity
                    variance_value = variance_quantity * price_per_unit

                    record = MonthlyStocktake(
                        stocktake_date=stocktake_date,
                        ingredient_id=ingredient_id,
                        previous_quantity=previous_quantity,
                        counted_quantity=counted_quantity,
                        variance_quantity=variance_quantity,
                        price_per_unit=price_per_unit,
                        variance_value=variance_value
                    )

                    db.session.add(record)

                    # ✅ Update live stock (NO merge!)
                    ingredient.quantity = counted_quantity

            db.session.commit()

            return jsonify({
                "success": True,
                "message": f"Stocktake recorded successfully for {stocktake_date}."
            })

        except OperationalError as e:
            db.session.rollback()

            # MySQL deadlock (1213) or lock wait timeout (1205)
            msg = str(e.orig) if getattr(e, "orig", None) else str(e)
            if "1213" in msg or "1205" in msg:
                if attempt < MAX_RETRIES:
                    continue
                return jsonify({
                    "success": False,
                    "message": "Stocktake is busy right now (database lock). Please try again."
                })

            print("❌ Stocktake OperationalError:", e)
            return jsonify({"success": False, "message": "Error processing stocktake."})

        except IntegrityError as e:
            db.session.rollback()
            print("❌ Stocktake IntegrityError:", e)
            return jsonify({
                "success": False,
                "message": "Duplicate stocktake detected (already exists)."
            })

        except Exception as e:
            db.session.rollback()
            print("❌ Stocktake Error:", e)
            return jsonify({"success": False, "message": "Error processing stocktake."})


@app.route("/monthly_stocktake_report", methods=["GET"])
@login_required
def monthly_stocktake_report():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    # 📅 Date range (YYYY-MM-DD)
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")

    # Defaults: current month
    today = datetime.utcnow().date()
    if not start_date_str or not end_date_str:
        start_date = today.replace(day=1)
        end_date = today
    else:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

    records = (
        MonthlyStocktake.query
        .join(Ingredient)
        .filter(MonthlyStocktake.stocktake_date.between(start_date, end_date))
        .order_by(MonthlyStocktake.stocktake_date.asc(), Ingredient.name.asc())
        .all()
    )

    total_gain = Decimal("0")
    total_loss = Decimal("0")

    for r in records:
        if r.variance_value > 0:
            total_gain += r.variance_value
        else:
            total_loss += abs(r.variance_value)

    net_variance = total_gain - total_loss

    return render_template(
        "monthly_stocktake_report.html",
        records=records,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
        total_gain=total_gain,
        total_loss=total_loss,
        net_variance=net_variance
    )


# Run the Flask app
if __name__ == "__main__":
    app.run(debug=True, port=8081)

@app.route("/submit_weekly_stocktake", methods=["POST"])
@login_required
def submit_weekly_stocktake():
    """Handles the submission of weekly stocktake."""
    try:
        data = request.get_json().get("stocktake", [])

        if not data:
            return jsonify({"success": False, "message": "No stocktake data provided."})

        for entry in data:
            ingredient_id = entry["ingredient_id"]
            recorded_stock = entry["recorded_stock"]

            # ✅ Determine if it's a text response or numeric value
            need_to_buy = False
            if recorded_stock == "Not enough in store":
                need_to_buy = True  # ✅ Mark as needing purchase

            # ✅ Insert into WeeklyStocktake
            new_stocktake = WeeklyStocktake(
                ingredient_id=ingredient_id,
                recorded_stock=recorded_stock,
                need_to_buy=need_to_buy
            )
            db.session.add(new_stocktake)

        db.session.commit()
        return jsonify({"success": True, "message": "Weekly stocktake recorded successfully!"})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error submitting weekly stocktake: {e}")
        return jsonify({"success": False, "message": "Error processing stocktake."})

import smtplib
import os
from email.message import EmailMessage


SMTP_SERVER = "smtp.gmail.com"  # ✅ Your email provider's SMTP
SMTP_PORT = 587

@app.route("/send_invoice/<int:invoice_no>", methods=["POST"])
@login_required
def send_invoice(invoice_no):
    try:
        data = request.get_json()
        recipient_email = data.get("email")

        if not recipient_email:
            return jsonify({"success": False, "message": "No recipient email provided."})

        # ✅ Set invoices directory
        invoices_dir = os.path.join(os.getcwd(), "static", "invoices")
        if not os.path.exists(invoices_dir):
            os.makedirs(invoices_dir)

        # ✅ Retrieve store name from database
        record = StockOutRecord.query.filter_by(invoice_no=invoice_no).first()
        if not record:
            return jsonify({"success": False, "message": "Invoice not found."})

        store_name = record.store.replace(" ", "_")
        pdf_filename = f"Invoice_{invoice_no}_{store_name}.pdf"
        pdf_path = os.path.join(invoices_dir, pdf_filename)

        # ✅ If PDF does NOT exist, generate it
        if not os.path.exists(pdf_path):
            print(f"⚠️ Invoice PDF not found. Generating Invoice #{invoice_no}...")
            export_invoice(invoice_no)  # ✅ Generate the invoice PDF
            if not os.path.exists(pdf_path):  # ✅ Double-check after generation
                return jsonify({"success": False, "message": "Failed to generate invoice PDF."})

        # ✅ Create email
        mail_username = app.config.get("MAIL_USERNAME") or "binginvoice@gmail.com"
        mail_password = app.config.get("MAIL_PASSWORD")

        if not mail_password:
            return jsonify({"success": False, "message": "Email password is not configured on the server."})

        msg = EmailMessage()
        msg["From"] = mail_username
        msg["To"] = recipient_email
        msg["Subject"] = f"Invoice #{invoice_no}"

        # ✅ Email Body
        body = f"Please find attached Invoice #{invoice_no}."
        msg.set_content(body)

        # ✅ Attach PDF file
        with open(pdf_path, "rb") as attachment:
            msg.add_attachment(
                attachment.read(),
                maintype="application",
                subtype="pdf",
                filename=pdf_filename,
            )

        # ✅ Send email
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(mail_username, mail_password)
        server.send_message(msg)
        server.quit()

        return jsonify({"success": True, "message": f"Invoice #{invoice_no} sent to {recipient_email}!"})

    except Exception as e:
        print(f"❌ Error sending invoice: {e}")
        return jsonify({"success": False, "message": f"Error sending invoice: {e}"})

@app.route("/prepare_stock_out", methods=["POST"])
@login_required
def prepare_stock_out():
    store_id = request.form.get("store_id")
    date = request.form.get("date")
    ingredient_names = request.form.getlist("ingredient_names[]")
    stock_needed = request.form.getlist("stock_needed[]")

    # ✅ Get store name from ID
    store_user = User.query.filter_by(id=store_id).first()
    store_name = store_user.username if store_user else "Unknown"

    # ✅ Build filtered items list
    items = []
    for name, qty in zip(ingredient_names, stock_needed):
        try:
            qty_float = float(qty)
            if qty_float <= 0:
                continue  # 🔥 Skip items with 0 or less
        except (ValueError, TypeError):
            continue  # 🔥 Skip non-numeric or invalid inputs

        ingredient = Ingredient.query.filter_by(name=name).first()
        if not ingredient:
            print(f"⚠️ Ingredient not found: {name}")
            continue

        items.append({
            "ingredientId": ingredient.id,
            "ingredientName": ingredient.name,
            "stockRemoved": qty_float,
            "store": store_name,
            "stockDate": date
        })

    # ✅ Save only items with valid stockRemoved > 0
    session["prefilled_stock_out"] = {
        "store": store_name,
        "date": date,
        "items": items
    }

    print("📦 Prefilled Stock Out Items:", session["prefilled_stock_out"])

    return redirect(url_for("stock_out"))

@app.route("/past_daily_stocktakes")
@login_required
def past_daily_stocktakes():
    if current_user.role != "user":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    user_id = current_user.id

    # ✅ Only include stocktakes from the last 7 days
    today = datetime.utcnow().date()
    seven_days_ago = today - timedelta(days=6)

    # ✅ Get all daily stocktake ingredients in correct order
    ordered_ingredients = Ingredient.query \
        .filter_by(daily_stocktake=True) \
        .order_by(Ingredient.order_position.asc()) \
        .all()
    ingredient_names = [ing.name for ing in ordered_ingredients]

    # ✅ Fetch stocktakes from last 7 days
    stocktakes = db.session.query(
        Stocktake.ingredient_id,
        Stocktake.date_recorded,
        Stocktake.quantity_on_hand,
        Ingredient.name.label("ingredient_name")
    ).join(Ingredient, Stocktake.ingredient_id == Ingredient.id
    ).filter(
        Stocktake.user_id == user_id,
        Stocktake.stocktake_type == "daily",
        db.func.date(Stocktake.date_recorded) >= seven_days_ago
    ).order_by(Stocktake.date_recorded.asc()).all()

    # ✅ Organize into pivot structure
    data = defaultdict(dict)
    all_dates = set()

    for record in stocktakes:
        formatted_date = record.date_recorded.strftime('%d/%m/%y')
        data[formatted_date][record.ingredient_name] = record.quantity_on_hand
        all_dates.add(formatted_date)

    sorted_dates = sorted(all_dates, key=lambda d: datetime.strptime(d, "%d/%m/%y"))

    return render_template(
        "past_daily_stocktakes.html",
        data=data,
        dates=sorted_dates,
        ingredient_names=ingredient_names  # ✅ Ordered properly
    )

@app.route("/past_weekly_stocktakes")
@login_required
def past_weekly_stocktakes():
    if current_user.role != "user":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    user_id = current_user.id

    # Get last 7 dates where user did weekly stocktake
    date_query = (
        db.session.query(db.func.date(Stocktake.date_recorded))
        .filter_by(user_id=user_id, stocktake_type="weekly")
        .group_by(db.func.date(Stocktake.date_recorded))
        .order_by(db.func.date(Stocktake.date_recorded).desc())
        .limit(7)
    )

    recent_dates = [d[0] for d in date_query.all()]
    recent_dates.sort()  # Sort ascending for left-to-right display

    # Get all weekly ingredients for the user
    ingredients = (
        Ingredient.query
        .filter_by(weekly_stocktake=True)
        .order_by(Ingredient.weekly_order_position.asc())
        .all()
    )
    ingredient_names = [ingredient.name for ingredient in ingredients]

    # Initialize data dictionary
    data = {date.strftime("%d/%m/%y"): {name: "" for name in ingredient_names} for date in recent_dates}

    # Get actual stocktake records
    records = (
        db.session.query(
            db.func.date(Stocktake.date_recorded),
            Ingredient.name,
            Stocktake.quantity_on_hand
        )
        .join(Ingredient, Stocktake.ingredient_id == Ingredient.id)
        .filter(
            Stocktake.user_id == user_id,
            Stocktake.stocktake_type == "weekly",
            db.func.date(Stocktake.date_recorded).in_(recent_dates)
        )
        .all()
    )

    for record_date, ingredient_name, quantity in records:
        formatted_date = record_date.strftime("%d/%m/%y")
        data[formatted_date][ingredient_name] = quantity

    return render_template(
        "past_weekly_stocktakes.html",
        dates=[d.strftime("%d/%m/%y") for d in recent_dates],
        ingredient_names=ingredient_names,
        data=data
    )

@app.route("/purchasing")
@login_required
def purchasing():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    # ✅ If no date filters, show all records
    if start_date and end_date:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()

            records = StockInRecord.query \
                .filter(StockInRecord.date >= start, StockInRecord.date <= end) \
                .order_by(StockInRecord.date.desc()) \
                .all()
        except ValueError:
            flash("Invalid date format.", "danger")
            return redirect(url_for("purchasing"))
    else:
        records = StockInRecord.query.order_by(StockInRecord.date.desc()).all()

    # ✅ Group data by date
    grouped_data = {}
    for record in records:
        date_str = record.date.strftime("%d/%m/%y")
        if date_str not in grouped_data:
            grouped_data[date_str] = {"total": 0, "items": []}

        price = float(record.price)
        quantity = float(record.quantity)
        total_price = price * quantity

        grouped_data[date_str]["total"] += total_price
        grouped_data[date_str]["items"].append({
            "ingredient": record.item,
            "quantity": quantity,
            "supplier": record.supplier,
            "price_per_unit": price,
            "total_price": total_price
        })

    # ✅ Calculate total spend (reacts to filter)
    total_spend = sum(data["total"] for data in grouped_data.values())

    return render_template(
        "purchasing.html",
        grouped_data=grouped_data,
        start_date=start_date,
        end_date=end_date,
        total_spend=total_spend
    )

@app.route("/sales_report", methods=["GET"])
@login_required
def sales_report():
    try:
        db.session.execute(text("SELECT 1"))
    except OperationalError as e:
        print("❌ DB connection lost. Reconnecting...")
        db.session.remove()
        db.session.execute(text("SELECT 1"))

    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    # 🔎 Date filters
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    store_filter = request.args.get("store_filter")

    if not start_date or not end_date:
        today = datetime.utcnow().date()
        start_date = today.strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

    start_utc = datetime.strptime(start_date + " 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
    end_utc = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')

    stores = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]
    selected_stores = [store_filter] if store_filter in stores else stores

    all_sales = {}
    total_revenue = 0
    total_orders = 0
    total_items_sold = 0
    store_revenue = {}
    category_sales = defaultdict(lambda: defaultdict(lambda: {"quantity": 0, "revenue": 0.0}))

    for store_name in selected_stores:
        store_orders = fetch_sales_for_store(store_name, start_date=start_utc, end_date=end_utc)

        store_total = 0
        for order in store_orders:
            total_orders += 1
            order_total = order.get('total_money', {}).get('amount', 0) / 100
            total_revenue += order_total
            store_total += order_total

            for item in order.get("line_items", []):
                item_name = item.get("name", "").strip()
                if not item_name:
                    continue
                qty = int(item.get("quantity", 0))
                line_revenue = get_square_line_item_revenue(item)
                total_items_sold += qty
                category = ITEM_CATEGORY_MAP.get(item_name, "Uncategorized")
                category_sales[category][item_name]["quantity"] += qty
                category_sales[category][item_name]["revenue"] += line_revenue

        all_sales[store_name] = store_orders
        store_revenue[store_name] = round(store_total, 2)

    sorted_category_sales = {}
    for cat, items in category_sales.items():
        sorted_items = dict(
            sorted(items.items(), key=lambda x: x[1]["quantity"], reverse=True)
        )
        category_revenue = sum(item["revenue"] for item in sorted_items.values())
        category_quantity = sum(item["quantity"] for item in sorted_items.values())
        sales_share = (category_revenue / total_revenue * 100) if total_revenue else 0

        sorted_category_sales[cat] = {
            "items": sorted_items,
            "total_revenue": round(category_revenue, 2),
            "total_quantity": category_quantity,
            "sales_share": round(sales_share, 1),
        }

    average_order_value = total_revenue / total_orders if total_orders > 0 else 0

    return render_template(
        "sales_report.html",
        all_sales=all_sales,
        start_date=start_date,
        end_date=end_date,
        total_revenue=round(total_revenue, 2),
        total_orders=total_orders,
        total_items_sold=total_items_sold,
        average_order_value=round(average_order_value, 2),
        category_sales=sorted_category_sales,
        store_revenue=store_revenue,
        store_filter=store_filter,
        stores=stores  # for dropdown options
    )

@app.route("/admin/square_sync", methods=["GET", "POST"])
@login_required
def square_sync():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    stores = User.query.filter_by(role="user").all()
    selected_store = request.form.get("store_name") or request.args.get("store_name")
    if not selected_store and stores:
        selected_store = stores[0].username

    today = datetime.utcnow().date()
    start_date = request.form.get("start_date") or request.args.get("start_date") or today.strftime("%Y-%m-%d")
    end_date = request.form.get("end_date") or request.args.get("end_date") or today.strftime("%Y-%m-%d")

    result = None
    if request.method == "POST":
        start_utc = datetime.strptime(start_date + " 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
        end_utc = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')

        result = sync_square_orders(selected_store, start_utc, end_utc)
        if result.get("skipped"):
            flash("Square sync already running for this store. Try again shortly.", "warning")
        elif result.get("error"):
            flash(result["error"], "danger")
        else:
            flash("Square sync completed.", "success")

    return render_template(
        "square_sync.html",
        stores=stores,
        selected_store=selected_store,
        start_date=start_date,
        end_date=end_date,
        result=result
    )

@app.route("/admin/square_mappings", methods=["GET", "POST"])
@login_required
def square_mappings():
    if current_user.role != "admin":
        flash("Access denied.", "danger")
        return redirect(url_for("index"))

    stores = User.query.filter_by(role="user").all()
    selected_store = request.args.get("store_name")
    if not selected_store and stores:
        selected_store = stores[0].username

    if request.method == "POST":
        action = request.form.get("action", "save")
        store_name = request.form.get("store_name") or selected_store

        if action == "apply":
            applied = apply_square_mappings(store_name)
            if applied.get("skipped"):
                flash("Square mapping already running for this store. Try again shortly.", "warning")
            elif applied.get("error"):
                flash(applied["error"], "danger")
            else:
                flash(f"Applied mappings. Ledger entries: {applied['created']}.", "success")
            return redirect(url_for("square_mappings", store_name=store_name))

        if action and action.startswith("save_one_"):
            try:
                save_index = int(action.split("_")[-1])
            except (ValueError, IndexError):
                save_index = None
        else:
            save_index = None

        catalog_object_ids = request.form.getlist("catalog_object_id[]")
        recipe_ids = request.form.getlist("recipe_id[]")
        multipliers = request.form.getlist("multiplier[]")
        item_names = request.form.getlist("item_name[]")

        def upsert_mapping(idx):
            if idx is None or idx < 0:
                return False
            if idx >= len(catalog_object_ids) or idx >= len(recipe_ids):
                return False
            catalog_object_id = catalog_object_ids[idx]
            recipe_id = recipe_ids[idx]
            if not catalog_object_id or not recipe_id:
                return False

            multiplier_raw = multipliers[idx] if idx < len(multipliers) else "1"
            try:
                multiplier = Decimal(str(multiplier_raw))
            except (InvalidOperation, ValueError):
                multiplier = Decimal("1")

            item_name = item_names[idx] if idx < len(item_names) else None
            mapping = SquareItemSalesRecipe.query.filter_by(
                store_name=store_name,
                catalog_object_id=catalog_object_id
            ).first()

            if not mapping:
                mapping = SquareItemSalesRecipe(
                    store_name=store_name,
                    catalog_object_id=catalog_object_id
                )
                db.session.add(mapping)

            mapping.sales_recipe_id = int(recipe_id)
            mapping.item_name = item_name
            mapping.multiplier = multiplier
            mapping.active = True
            return True

        if action == "bulk_save":
            updated = 0
            for idx in range(min(len(catalog_object_ids), len(recipe_ids))):
                if upsert_mapping(idx):
                    updated += 1
            db.session.commit()
            flash(f"Bulk save complete. Updated {updated} mapping(s).", "success")
            return redirect(url_for("square_mappings", store_name=store_name))

        if save_index is not None:
            if upsert_mapping(save_index):
                db.session.commit()
                flash("Mapping saved.", "success")
                return redirect(url_for("square_mappings", store_name=store_name))
            flash("Missing catalog object ID or recipe.", "danger")
            return redirect(url_for("square_mappings", store_name=store_name))

        flash("Missing catalog object ID or recipe.", "danger")

    recipes = []
    recipe_name_map = {}
    recipe_choices = []
    for recipe in SalesRecipe.query.order_by(SalesRecipe.name.asc()).all():
        recipes.append({"id": recipe.id, "name": recipe.name})
        recipe_name_map[recipe.id] = recipe.name
        recipe_choices.append((recipe.id, normalize_label(recipe.name)))

    line_items = []
    if selected_store:
        raw_items = db.session.query(
            SquareOrderLine.catalog_object_id,
            func.max(SquareOrderLine.item_name).label("item_name")
        ).filter(
            SquareOrderLine.store_name == selected_store,
            SquareOrderLine.catalog_object_id.isnot(None)
        ).group_by(SquareOrderLine.catalog_object_id).all()

        mappings = SquareItemSalesRecipe.query.filter_by(store_name=selected_store).all()
        mapping_map = {m.catalog_object_id: m for m in mappings}

        suggest_threshold = 0.75
        for row in raw_items:
            mapping = mapping_map.get(row.catalog_object_id)
            current_recipe_id = mapping.sales_recipe_id if mapping else None
            suggested_recipe_id = None
            suggested_ratio = None

            if not current_recipe_id:
                suggested_recipe_id, ratio = best_recipe_match(row.item_name, recipe_choices)
                if suggested_recipe_id and ratio >= suggest_threshold:
                    suggested_ratio = ratio
                else:
                    suggested_recipe_id = None

            line_items.append({
                "catalog_object_id": row.catalog_object_id,
                "item_name": row.item_name,
                "recipe_id": current_recipe_id,
                "multiplier": float(mapping.multiplier) if mapping and mapping.multiplier is not None else 1.0,
                "mapped": bool(mapping),
                "suggested_recipe_id": suggested_recipe_id,
                "suggested_recipe_name": recipe_name_map.get(suggested_recipe_id) if suggested_recipe_id else None,
                "suggested_ratio": round(suggested_ratio * 100, 0) if suggested_ratio else None
            })

    return render_template(
        "square_mappings.html",
        stores=stores,
        selected_store=selected_store,
        line_items=line_items,
        recipes=recipes
    )

@app.route("/freezer_pack_needs", methods=["GET"])
@login_required
def freezer_pack_needs():
    try:
        db.session.execute("SELECT 1")
    except Exception as e:
        db.session.rollback()
        print("🔁 Reconnected DB session:", e)

    # Get date range from query params, default to last 7 days
    today = datetime.utcnow().date()
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")

    if start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    else:
        start_date = today - timedelta(days=7)
        end_date = today

    start_utc = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    stores = ["Doncaster", "Lonsdale", "Clayton", "Glen Waverley"]
    bingsu_flavor_sales = defaultdict(int)

    for store in stores:
        orders = fetch_sales_for_store(store, start_date=start_utc, end_date=end_utc)
        for order in orders:
            for item in order.get("line_items", []):
                name = item.get("name", "")
                if ITEM_CATEGORY_MAP.get(name) == "Bingsu":
                    qty = int(item.get("quantity", 0))
                    bingsu_flavor_sales[name] += qty

    freezer_pack_ingredient = Ingredient.query.filter_by(name="Freezer Pack").first()
    freezer_pack_stock = 0
    if freezer_pack_ingredient:
        latest_stocktake = (
            Stocktake.query.filter_by(ingredient_id=freezer_pack_ingredient.id)
            .order_by(Stocktake.timestamp.desc())
            .first()
        )
        if latest_stocktake:
            try:
                freezer_pack_stock = float(latest_stocktake.quantity)
            except ValueError:
                freezer_pack_stock = 0

    per_flavor_packs = {
        flavor: round(qty_sold / 5.5, 2)
        for flavor, qty_sold in bingsu_flavor_sales.items()
    }

    ingredients_by_flavor = {}
    for flavor, packs_needed in per_flavor_packs.items():
        recipe = Recipe.query.join(Recipe.output_item).filter(Ingredient.name == flavor).first()
        if not recipe:
            continue

        ingredients = {}
        for ri in RecipeIngredient.query.filter_by(recipe_id=recipe.id).all():
            ing = Ingredient.query.get(ri.ingredient_id)
            if not ing:
                continue
            total_grams = packs_needed * ri.grams_used
            units_needed = total_grams / ing.grams_per_unit if ing.grams_per_unit else 0

            ingredients[ing.name] = {
                "grams_needed": round(total_grams, 2),
                "units_to_purchase": round(units_needed, 2),
                "supplier": ing.supplier,
                "category": ing.category
            }

        ingredients_by_flavor[flavor] = ingredients

    return render_template(
        "freezer_pack_needs.html",
        bingsu_flavor_sales=dict(bingsu_flavor_sales),
        per_flavor_packs=per_flavor_packs,
        current_stock=freezer_pack_stock,
        ingredients_by_flavor=ingredients_by_flavor,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d")
    )

@app.route("/check_session")
def check_session():
    from flask_login import current_user
    return jsonify({"active": current_user.is_authenticated})

@app.route("/cashflow")
@login_required
def cashflow():
    from sqlalchemy import func

    # ✅ Sum prices of unpaid stock outs
    unpaid_records = (
        db.session.query(func.sum(StockOutRecord.price * StockOutRecord.quantity))
        .filter(StockOutRecord.paid == False)
        .scalar()
    )

    total_unpaid = float(unpaid_records or 0)

    ingredients = Ingredient.query.order_by(Ingredient.name).all()
    return render_template("cashflow.html", total_unpaid=total_unpaid, ingredients=ingredients)
