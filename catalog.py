"""A small synthetic catalog for a fictional work-from-home equipment merchant.

Deliberately shaped to exercise the policy engine rather than to look pretty:

  * Some products carry fat margins (chair, desk) and can absorb a discount.
  * Some are razor thin (cable, mousepad) and cannot -- these are the ones that
    catch a naive agent trying to be generous.
  * One is out of stock, so the stock check has something to reject.
  * Prices span two orders of magnitude, so percentage-based caps behave
    differently across the range.

All amounts are integer paise. Rs 1 == 100 paise.
"""

from policy import Product

CATALOG: dict[str, Product] = {
    p.sku: p
    for p in [
        # --- high margin, room to discount -------------------------------
        Product("SKU-DESK-01", "Standing Desk (120cm)", 1_899_000, 1_050_000, 12),
        Product("SKU-CHAIR-01", "Ergonomic Mesh Chair", 1_249_000, 690_000, 8),
        Product("SKU-MON-27", "27-inch QHD Monitor", 2_199_000, 1_540_000, 15),
        Product("SKU-LAMP-01", "Desk Lamp with Wireless Charging", 349_000, 175_000, 30),
        Product("SKU-STAND-01", "Aluminium Laptop Stand", 249_000, 112_000, 45),

        # --- medium margin ------------------------------------------------
        Product("SKU-KB-MECH", "Mechanical Keyboard (Brown)", 649_000, 421_000, 20),
        Product("SKU-MOUSE-01", "Wireless Ergonomic Mouse", 299_000, 194_000, 35),
        Product("SKU-HUB-USBC", "7-in-1 USB-C Hub", 399_000, 271_000, 25),
        Product("SKU-WEBCAM-01", "1080p Webcam with Privacy Shutter", 449_000, 310_000, 18),

        # --- thin margin: cannot absorb a discount -------------------------
        # A 10% discount here sells below cost. The margin floor is the only
        # thing standing between the agent and a loss-making offer.
        Product("SKU-CABLE-C", "USB-C Braided Cable (2m)", 59_000, 54_000, 120),
        Product("SKU-PAD-XL", "XL Desk Mat", 89_000, 82_000, 60),

        # --- out of stock --------------------------------------------------
        Product("SKU-ARM-01", "Single Monitor Arm", 549_000, 302_000, 0),

        # --- accessories ---------------------------------------------------
        Product("SKU-HEADSET", "Noise-Cancelling Headset", 899_000, 561_000, 14),
        Product("SKU-FOOTREST", "Adjustable Footrest", 199_000, 108_000, 40),
    ]
}


def rupees(paise: int) -> str:
    """Format paise as a readable rupee string for logs and demos."""
    return f"Rs {paise / 100:,.2f}"


def catalog_for_prompt(exclude: set[str] = frozenset()) -> str:
    """Render the catalog as compact text for the model.

    Cost price is deliberately NOT included. The model has no business knowing
    the merchant's margins -- it proposes, the policy engine prices. Leaking
    cost into the prompt would also let a jailbreak reason about how far it
    could push a discount.
    """
    lines = []
    for p in CATALOG.values():
        if p.sku in exclude or p.stock == 0:
            continue
        lines.append(f"{p.sku} | {p.name} | {rupees(p.price_paise)}")
    return "\n".join(lines)
