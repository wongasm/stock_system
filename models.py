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
