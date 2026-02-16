from math import ceil
from models import db, Ingredient, Supplier, Category, StockOutRecord, Recipe, RecipeIngredient, User, Stocktake, StoreThreshold, MonthlyStocktake, Invoice, WeeklyStocktake, StockInRecord
from sqlalchemy.orm import joinedload

def calculate_ingredients_for_freezer_pack(bingsus_sold, freezer_pack_stock, freezer_pack_item_name="Freezer Pack"):
    """
    Returns (ingredients_needed_dict, total_packs_needed, packs_to_make)
    """
    # Step 1: Calculate how many packs we need to make
    packs_needed = ceil(bingsus_sold / 5.5)

    # Step 2: Subtract what we already have in stock
    packs_to_make = max(0, packs_needed - freezer_pack_stock)

    if packs_to_make == 0:
        return {}, packs_needed, packs_to_make  # ✅ Always return 3 values

    # Step 3: Look up recipe for freezer pack
    recipe = Recipe.query.join(Recipe.output_item).filter(Ingredient.name == freezer_pack_item_name).first()
    if not recipe:
        print(f"⚠️ No recipe found for: {freezer_pack_item_name}")
        return {}, packs_needed, packs_to_make  # ✅ Always return 3 values

    # Step 4: Calculate ingredient requirements
    ingredients_needed = {}

    for ri in RecipeIngredient.query.filter_by(recipe_id=recipe.id).all():
        ingredient = Ingredient.query.get(ri.ingredient_id)
        if not ingredient:
            continue

        total_grams = packs_to_make * ri.grams_required
        units_needed = total_grams / ingredient.grams_per_unit if ingredient.grams_per_unit else 0

        ingredients_needed[ingredient.name] = {
            "grams_needed": round(total_grams, 2),
            "units_to_purchase": round(units_needed, 2),
            "supplier": ingredient.supplier,
            "category": ingredient.category
        }

    return ingredients_needed, packs_needed, packs_to_make  # ✅ Return all 3