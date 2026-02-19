from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from sqlalchemy import Numeric


db = SQLAlchemy()

# Define Ingredient Model
class Ingredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(10, 1), nullable=False)  # Supports 1 decimal place
    unit = db.Column(db.String(50), nullable=False)
    supplier = db.Column(db.String(255), nullable=True)
    category = db.Column(db.String(255), nullable=True)  # New Category column
    grams_per_unit = db.Column(db.Float, nullable=False)  # Grams per unit, must not be NULL
    threshold = db.Column(db.Integer, nullable=True)  # New Threshold column
    price_per_unit = db.Column(Numeric(10, 2))
    selling_price = db.Column(Numeric(10, 2))
    daily_stocktake = db.Column(db.Boolean, default=False, nullable=False)
    weekly_stocktake = db.Column(db.Boolean, default=False, nullable=False)  # ✅ New column
    order_position = db.Column(db.Integer, default=0)
    weekly_order_position = db.Column(db.Integer, default=0)
    monthly_stocktakes = db.relationship("MonthlyStocktake", backref="ingredient_stock", cascade="all, delete")
    measurement_type = db.Column(db.String(10), default="numeric", nullable=False)
    is_archived = db.Column(db.Boolean, default=False, nullable=False)

class StoreInventory(db.Model):
    __tablename__ = "store_inventory"

    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)
    quantity = db.Column(db.Numeric(10, 1), nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    store = db.relationship("User", backref="store_inventory")
    ingredient = db.relationship("Ingredient", backref="store_inventory")

    __table_args__ = (
        db.UniqueConstraint("store_id", "ingredient_id", name="uq_store_inventory"),
    )

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)

class StockOutRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_no = db.Column(db.Integer, nullable=False)
    date = db.Column(db.String(10), nullable=False)
    store = db.Column(db.String(50), nullable=False)
    item = db.Column(db.String(255), nullable=False)
    price = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Numeric(10, 1), nullable=False)
    selling_price = db.Column(db.Numeric(10, 2), nullable=True)
    paid = db.Column(db.Boolean, default=False)

class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    output_item_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)  # Output Item
    output_item = db.relationship("Ingredient", foreign_keys=[output_item_id])  # Link Output Item

class RecipeIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("recipe.id"), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)
    grams_used = db.Column(db.Float, nullable=False)  # Amount used in grams

    recipe = db.relationship("Recipe", foreign_keys=[recipe_id])
    ingredient = db.relationship("Ingredient", foreign_keys=[ingredient_id])

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(50), nullable=False, default='user')  # 'admin' or 'user'

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Stocktake(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)
    quantity_on_hand = db.Column(db.String(50), nullable=False)
    stocktake_type = db.Column(db.String(20), nullable=False)  # "daily" or "weekly"
    date_recorded = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="stocktakes")
    ingredient = db.relationship("Ingredient", backref="stocktakes")

class StoreThreshold(db.Model):
    __tablename__ = "store_thresholds"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    store_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)
    threshold = db.Column(db.Float, nullable=False, default=0)

    # ✅ Relationships (optional, useful for joins)
    store = db.relationship("User", backref="thresholds")
    ingredient = db.relationship("Ingredient", backref="thresholds")

class StoreWeeklyItem(db.Model):
    __tablename__ = "store_weekly_item"

    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    order_position = db.Column(db.Integer, default=0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    store = db.relationship("User", backref="weekly_items")
    ingredient = db.relationship("Ingredient", backref="weekly_items")

    __table_args__ = (
        db.UniqueConstraint("store_id", "ingredient_id", name="uq_store_weekly_item"),
    )

class SalesRecipe(db.Model):
    __tablename__ = "sales_recipe"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SalesRecipeIngredient(db.Model):
    __tablename__ = "sales_recipe_ingredient"

    id = db.Column(db.Integer, primary_key=True)
    sales_recipe_id = db.Column(db.Integer, db.ForeignKey("sales_recipe.id"), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)
    grams_used = db.Column(db.Numeric(12, 3), nullable=False)

    sales_recipe = db.relationship("SalesRecipe", backref="ingredients")
    ingredient = db.relationship("Ingredient")

class SquareItemSalesRecipe(db.Model):
    __tablename__ = "square_item_sales_recipe"

    id = db.Column(db.Integer, primary_key=True)
    store_name = db.Column(db.String(50), nullable=False)
    catalog_object_id = db.Column(db.String(64), nullable=False)
    item_name = db.Column(db.String(255), nullable=True)
    sales_recipe_id = db.Column(db.Integer, db.ForeignKey("sales_recipe.id"), nullable=False)
    multiplier = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    active = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sales_recipe = db.relationship("SalesRecipe", backref="square_mappings")

    __table_args__ = (
        db.UniqueConstraint("store_name", "catalog_object_id", name="uq_square_item_sales_recipe"),
    )

class SquareOrder(db.Model):
    __tablename__ = "square_order"

    id = db.Column(db.Integer, primary_key=True)
    square_order_id = db.Column(db.String(64), unique=True, nullable=False)
    store_name = db.Column(db.String(50), nullable=False)
    location_id = db.Column(db.String(64), nullable=True)
    state = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=True)

class SquareOrderLine(db.Model):
    __tablename__ = "square_order_line"

    id = db.Column(db.Integer, primary_key=True)
    square_order_id = db.Column(db.String(64), nullable=False)
    store_name = db.Column(db.String(50), nullable=False)
    line_uid = db.Column(db.String(64), nullable=False)
    source_line_uid = db.Column(db.String(64), nullable=True)
    item_name = db.Column(db.String(255), nullable=True)
    variation_name = db.Column(db.String(255), nullable=True)
    catalog_object_id = db.Column(db.String(64), nullable=True)
    item_type = db.Column(db.String(32), nullable=True)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    is_return = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("square_order_id", "line_uid", name="uq_square_order_line"),
    )

class SquareItemRecipe(db.Model):
    __tablename__ = "square_item_recipe"

    id = db.Column(db.Integer, primary_key=True)
    store_name = db.Column(db.String(50), nullable=False)
    catalog_object_id = db.Column(db.String(64), nullable=False)
    item_name = db.Column(db.String(255), nullable=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("recipe.id"), nullable=False)
    multiplier = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    active = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    recipe = db.relationship("Recipe", backref="square_mappings")

    __table_args__ = (
        db.UniqueConstraint("store_name", "catalog_object_id", name="uq_square_item_recipe"),
    )

class InventoryLedger(db.Model):
    __tablename__ = "inventory_ledger"

    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)
    qty_delta = db.Column(db.Numeric(12, 3), nullable=False)
    reason = db.Column(db.String(32), nullable=False)
    source_type = db.Column(db.String(32), nullable=True)
    source_id = db.Column(db.String(64), nullable=True)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    store = db.relationship("User", backref="inventory_ledger")
    ingredient = db.relationship("Ingredient", backref="inventory_ledger")

    __table_args__ = (
        db.UniqueConstraint("source_type", "source_id", "ingredient_id", name="uq_ledger_source"),
    )

class MonthlyStocktake(db.Model):
    __tablename__ = "monthly_stocktake"

    id = db.Column(db.Integer, primary_key=True)

    stocktake_date = db.Column(db.Date, nullable=False, index=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredient.id"), nullable=False)

    previous_quantity = db.Column(db.Numeric(10,1), nullable=False)
    counted_quantity = db.Column(db.Numeric(10,1), nullable=False)
    variance_quantity = db.Column(db.Numeric(10,1), nullable=False)

    price_per_unit = db.Column(db.Numeric(10,2), nullable=False)
    variance_value = db.Column(db.Numeric(10,2), nullable=False)

    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)

    ingredient = db.relationship("Ingredient")


class Invoice(db.Model):
    __tablename__ = "invoice"  # Ensure this matches your actual table name in MySQL
    invoice_no = db.Column(db.Integer, primary_key=True, autoincrement=True)
    date = db.Column(db.Date, nullable=False)
    store = db.Column(db.String(255), nullable=False)
    total_amount = db.Column(db.Numeric(10,2), nullable=False)  # Adjust if needed

    def __repr__(self):
        return f"<Invoice {self.invoice_no}>"

class WeeklyStocktake(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id'), nullable=False)
    recorded_stock = db.Column(db.String(50), nullable=True)  # ✅ Supports "Enough in store" or numeric value
    need_to_buy = db.Column(db.Boolean, default=False)  # ✅ Tracks if the item needs to be bought
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)

    ingredient = db.relationship("Ingredient", backref="weekly_stocktakes")

class StockInRecord(db.Model):

    __tablename__ = 'stock_in_record'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    supplier = db.Column(db.String(100))
    item = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), nullable=False)
    total_cost = db.Column(db.Numeric(10, 2), nullable=False)
