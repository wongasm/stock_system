from square_api import fetch_sales_for_store  # Replace 'your_module' with actual filename (e.g., app)
from dotenv import load_dotenv

load_dotenv()

orders = fetch_sales_for_store("Clayton")

print(f"✅ {len(orders)} orders fetched.")
for order in orders:
    print(order)
