"""Tests for the offer policy engine.

Each test pins one guardrail. If a test here goes red, the agent is capable of
giving away money it should not have given away.
"""

import pytest

from policy import (
    Cart,
    CartItem,
    CampaignState,
    OfferProposal,
    PolicyConfig,
    Product,
    evaluate,
)

NOW = 1_000_000.0


@pytest.fixture
def catalog():
    return {
        # Rs 1000 list, Rs 600 cost -> 40% list margin, room to discount.
        "SKU-MOUSE": Product("SKU-MOUSE", "Wireless Mouse", 100_000, 60_000, 25),
        # Rs 500 list, Rs 460 cost -> 8% margin, no room at all.
        "SKU-CABLE": Product("SKU-CABLE", "USB-C Cable", 50_000, 46_000, 40),
        "SKU-STAND": Product("SKU-STAND", "Laptop Stand", 200_000, 100_000, 0),
        "SKU-DESK": Product("SKU-DESK", "Standing Desk", 1_500_000, 900_000, 5),
    }


@pytest.fixture
def cart():
    return Cart("cust_001", (CartItem("SKU-DESK", 1),))


@pytest.fixture
def config():
    return PolicyConfig()


def test_clean_offer_is_approved(catalog, cart, config):
    proposal = OfferProposal("SKU-MOUSE", 10_000, "Pairs well with your desk")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert d.approved
    assert d.final_price_paise == 90_000
    assert d.failed_checks == ()


def test_hallucinated_sku_is_rejected(catalog, cart, config):
    """A model inventing a plausible product ID must not get priced."""
    proposal = OfferProposal("SKU-KEYBOARD-PRO", 5_000, "Great keyboard")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert not d.approved
    assert d.failed_checks[0].name == "sku_exists"


def test_item_already_in_cart_is_rejected(catalog, cart, config):
    proposal = OfferProposal("SKU-DESK", 10_000, "Buy another desk")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert not d.approved
    assert "not_already_in_cart" in {c.name for c in d.failed_checks}


def test_out_of_stock_is_rejected(catalog, cart, config):
    proposal = OfferProposal("SKU-STAND", 10_000, "Raise your screen")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert not d.approved
    assert "in_stock" in {c.name for c in d.failed_checks}


def test_discount_above_ceiling_is_rejected(catalog, cart, config):
    """30% off a product whose ceiling is 20%."""
    proposal = OfferProposal("SKU-MOUSE", 30_000, "Huge deal!")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert not d.approved
    assert "discount_ceiling" in {c.name for c in d.failed_checks}


def test_margin_floor_is_rejected(catalog, cart, config):
    """A thin-margin product cannot absorb even a small discount.

    Cable: Rs 500 list, Rs 460 cost. A 10% discount (within the ceiling) leaves
    a selling price of Rs 450 against a Rs 460 cost -- a loss. The ceiling check
    passes and the margin floor is what saves us. This is precisely why both
    checks exist.
    """
    proposal = OfferProposal("SKU-CABLE", 5_000, "Cable deal")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert not d.approved
    failed = {c.name for c in d.failed_checks}
    assert "margin_floor" in failed
    assert "discount_ceiling" not in failed


def test_negative_discount_is_rejected(catalog, cart, config):
    proposal = OfferProposal("SKU-MOUSE", -5_000, "Negative discount")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert not d.approved
    assert "discount_well_formed" in {c.name for c in d.failed_checks}


def test_exhausted_campaign_budget_is_rejected(catalog, cart, config):
    state = CampaignState(spent_paise=config.campaign_budget_paise - 1_000)
    proposal = OfferProposal("SKU-MOUSE", 10_000, "Pairs well with your desk")
    d = evaluate(proposal, cart, catalog, config, CampaignState(spent_paise=state.spent_paise), NOW)
    assert not d.approved
    assert "campaign_budget" in {c.name for c in d.failed_checks}


def test_customer_cooldown_is_enforced(catalog, cart, config):
    state = CampaignState(last_offer_at={"cust_001": NOW - 3_600})
    proposal = OfferProposal("SKU-MOUSE", 10_000, "Pairs well with your desk")
    d = evaluate(proposal, cart, catalog, config, state, NOW)
    assert not d.approved
    assert "customer_cooldown" in {c.name for c in d.failed_checks}


def test_cooldown_expires(catalog, cart, config):
    state = CampaignState(last_offer_at={"cust_001": NOW - 90_000})
    proposal = OfferProposal("SKU-MOUSE", 10_000, "Pairs well with your desk")
    d = evaluate(proposal, cart, catalog, config, state, NOW)
    assert d.approved


def test_every_check_is_recorded_even_when_passing(catalog, cart, config):
    """The audit trail should show what was evaluated, not just what tripped."""
    proposal = OfferProposal("SKU-MOUSE", 10_000, "Pairs well with your desk")
    d = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    names = [c.name for c in d.checks]
    assert names == [
        "sku_exists",
        "not_already_in_cart",
        "in_stock",
        "discount_well_formed",
        "discount_ceiling",
        "margin_floor",
        "campaign_budget",
        "customer_cooldown",
    ]


def test_decision_is_deterministic(catalog, cart, config):
    proposal = OfferProposal("SKU-MOUSE", 10_000, "Pairs well with your desk")
    a = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    b = evaluate(proposal, cart, catalog, config, CampaignState(), NOW)
    assert a == b
