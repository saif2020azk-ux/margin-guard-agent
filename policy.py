"""Deterministic offer policy engine.

This module decides whether a proposed upsell offer is allowed to reach a
customer. It is intentionally free of any LLM call, any network call and any
I/O. The language model proposes; this module disposes.

Design rules enforced here:
  * All money is integer paise. Never floats -- binary floats cannot represent
    rupee amounts exactly and rounding drift in a discount calculation is a
    real bug, not a theoretical one.
  * Every check is recorded whether it passed or failed, so the audit log can
    show what was evaluated and not merely what tripped.
  * The engine is a pure function of (proposal, cart, catalog, config, state).
    Same inputs, same decision, always -- which is what makes it explainable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

BPS = 10_000  # basis points denominator; 1500 bps == 15%


# --------------------------------------------------------------------------
# Domain models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_paise: int
    cost_paise: int
    stock: int

    @property
    def list_margin_paise(self) -> int:
        return self.price_paise - self.cost_paise


@dataclass(frozen=True)
class CartItem:
    sku: str
    qty: int


@dataclass(frozen=True)
class Cart:
    customer_id: str
    items: tuple[CartItem, ...]

    @property
    def skus(self) -> frozenset[str]:
        return frozenset(item.sku for item in self.items)


@dataclass(frozen=True)
class OfferProposal:
    """What the agent proposes. Treated as untrusted input.

    The SKU may not exist. The discount may be absurd. The pitch may be
    nonsense. Nothing here is believed without checking.
    """

    sku: str
    discount_paise: int
    pitch: str


@dataclass(frozen=True)
class PolicyConfig:
    """The merchant's guardrails. Set once, enforced always."""

    min_margin_bps: int = 1_500      # >=15% of selling price must remain margin
    max_discount_bps: int = 2_000    # never more than 20% off list price
    campaign_budget_paise: int = 500_000   # Rs 5,000 total giveaway budget
    offer_cooldown_seconds: int = 86_400   # one offer per customer per day


@dataclass(frozen=True)
class CampaignState:
    """Mutable-in-the-world facts, passed in as an immutable snapshot."""

    spent_paise: int = 0
    last_offer_at: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Decision:
    approved: bool
    proposal: OfferProposal
    checks: tuple[CheckResult, ...]
    final_price_paise: int | None = None

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def reason(self) -> str:
        if self.approved:
            return "all checks passed"
        return "; ".join(c.detail for c in self.failed_checks)


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


def evaluate(
    proposal: OfferProposal,
    cart: Cart,
    catalog: Mapping[str, Product],
    config: PolicyConfig,
    state: CampaignState,
    now: float,
) -> Decision:
    """Return an approve/reject Decision with a full record of every check."""

    checks: list[CheckResult] = []

    # Check 1: the SKU must actually exist. A model can hallucinate a plausible
    # product ID; if it does, we stop here rather than pricing a phantom.
    product = catalog.get(proposal.sku)
    if product is None:
        checks.append(
            CheckResult(
                "sku_exists",
                False,
                f"SKU {proposal.sku!r} is not in the catalog",
            )
        )
        return Decision(False, proposal, tuple(checks))
    checks.append(
        CheckResult("sku_exists", True, f"SKU {product.sku} resolved to {product.name}")
    )

    # Check 2: never upsell something already in the cart.
    already = proposal.sku in cart.skus
    checks.append(
        CheckResult(
            "not_already_in_cart",
            not already,
            f"{product.sku} is already in the cart" if already
            else f"{product.sku} is not already in the cart",
        )
    )

    # Check 3: stock.
    in_stock = product.stock > 0
    checks.append(
        CheckResult(
            "in_stock",
            in_stock,
            f"{product.sku} is out of stock" if not in_stock
            else f"{product.stock} units available",
        )
    )

    # Check 4: the discount must be sane in itself.
    sane = 0 <= proposal.discount_paise <= product.price_paise
    checks.append(
        CheckResult(
            "discount_well_formed",
            sane,
            f"discount {proposal.discount_paise} outside [0, {product.price_paise}]"
            if not sane else f"discount {proposal.discount_paise} paise is well formed",
        )
    )
    if not sane:
        # Downstream margin maths would be meaningless; stop cleanly.
        return Decision(False, proposal, tuple(checks))

    selling_paise = product.price_paise - proposal.discount_paise

    # Check 5: discount ceiling as a share of list price.
    within_ceiling = proposal.discount_paise * BPS <= config.max_discount_bps * product.price_paise
    actual_bps = (proposal.discount_paise * BPS) // product.price_paise if product.price_paise else 0
    checks.append(
        CheckResult(
            "discount_ceiling",
            within_ceiling,
            f"discount {actual_bps} bps exceeds ceiling {config.max_discount_bps} bps"
            if not within_ceiling
            else f"discount {actual_bps} bps within ceiling {config.max_discount_bps} bps",
        )
    )

    # Check 6: margin floor. This is the one that matters most -- it is the
    # difference between an upsell agent and a margin-destruction agent.
    margin_paise = selling_paise - product.cost_paise
    floor_ok = margin_paise * BPS >= config.min_margin_bps * selling_paise
    actual_margin_bps = (margin_paise * BPS) // selling_paise if selling_paise else 0
    checks.append(
        CheckResult(
            "margin_floor",
            floor_ok,
            f"resulting margin {actual_margin_bps} bps is below floor {config.min_margin_bps} bps"
            if not floor_ok
            else f"resulting margin {actual_margin_bps} bps clears floor {config.min_margin_bps} bps",
        )
    )

    # Check 7: campaign budget.
    budget_ok = state.spent_paise + proposal.discount_paise <= config.campaign_budget_paise
    remaining = config.campaign_budget_paise - state.spent_paise
    checks.append(
        CheckResult(
            "campaign_budget",
            budget_ok,
            f"discount {proposal.discount_paise} exceeds remaining budget {remaining}"
            if not budget_ok
            else f"{remaining} paise of campaign budget remaining",
        )
    )

    # Check 8: per-customer cooldown, so one shopper is not pestered.
    last = state.last_offer_at.get(cart.customer_id)
    cooled = last is None or (now - last) >= config.offer_cooldown_seconds
    checks.append(
        CheckResult(
            "customer_cooldown",
            cooled,
            f"customer {cart.customer_id} received an offer {int(now - last)}s ago"
            if not cooled else f"customer {cart.customer_id} is eligible",
        )
    )

    approved = all(c.passed for c in checks)
    return Decision(
        approved=approved,
        proposal=proposal,
        checks=tuple(checks),
        final_price_paise=selling_paise if approved else None,
    )
