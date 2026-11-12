"""Central registry for product categories and lookups."""
from .apple import PRODUCTS as APPLE_PRODUCTS
from .telegram import PRODUCTS as TG_PRODUCTS
from .instagram import PRODUCTS as INSTAGRAM_PRODUCTS
from .ai import PRODUCTS as AI_PRODUCTS
from .nodes import PRODUCTS as NODE_PRODUCTS

PRODUCT_CATALOGS = {
    "apple": APPLE_PRODUCTS,
    "tg": TG_PRODUCTS,
    "instagram": INSTAGRAM_PRODUCTS,
    "ai": AI_PRODUCTS,
    "node": NODE_PRODUCTS,
}

def get_all_products():
    """Return all configured products; brand navigation placeholders are excluded."""
    return [product for products in PRODUCT_CATALOGS.values() for product in products]
