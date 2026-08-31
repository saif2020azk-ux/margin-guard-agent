"""Tests for the LLM proposal layer.

No network calls here. These feed `build_proposal` and `_extract_json` the kind
of responses a language model actually produces -- including the malformed and
adversarial ones -- and check we degrade sensibly rather than crashing or
silently passing bad data downstream.
"""

import pytest

from agent import ProposalError, _extract_json, build_proposal
from catalog import CATALOG


def test_parses_clean_json():
    data = _extract_json('{"sku": "SKU-LAMP-01", "discount_percent": 10}')
    assert data["sku"] == "SKU-LAMP-01"


def test_parses_json_wrapped_in_markdown_fence():
    """Models add fences constantly, instructions notwithstanding."""
    raw = '```json\n{"sku": "SKU-LAMP-01", "discount_percent": 5}\n```'
    assert _extract_json(raw)["sku"] == "SKU-LAMP-01"


def test_parses_json_with_surrounding_chatter():
    raw = 'Sure! Here is my suggestion:\n{"sku": "SKU-LAMP-01"}\nHope that helps.'
    assert _extract_json(raw)["sku"] == "SKU-LAMP-01"


def test_unparseable_response_raises():
    with pytest.raises(ProposalError):
        _extract_json("I'm afraid I can't help with that.")


def test_decline_returns_none():
    """An agent that knows when to stay quiet."""
    assert build_proposal({"sku": None, "reasoning": "nothing complements"}) is None


def test_percent_converted_to_paise():
    product = CATALOG["SKU-LAMP-01"]
    proposal = build_proposal(
        {"sku": "SKU-LAMP-01", "discount_percent": 10, "pitch": "Nice lamp"}
    )
    assert proposal.discount_paise == product.price_paise // 10


def test_zero_discount_is_valid():
    proposal = build_proposal(
        {"sku": "SKU-LAMP-01", "discount_percent": 0, "pitch": "Nice lamp"}
    )
    assert proposal.discount_paise == 0


def test_missing_discount_defaults_to_zero():
    proposal = build_proposal({"sku": "SKU-LAMP-01", "pitch": "Nice lamp"})
    assert proposal.discount_paise == 0


def test_hallucinated_sku_becomes_a_proposal_not_an_exception():
    """We want the policy engine to reject it and log why.

    Raising here would lose the audit record. The rejection belongs in the
    decision log, not in a stack trace.
    """
    proposal = build_proposal({"sku": "SKU-DOES-NOT-EXIST", "discount_percent": 10})
    assert proposal is not None
    assert proposal.sku == "SKU-DOES-NOT-EXIST"


def test_non_numeric_discount_raises():
    with pytest.raises(ProposalError):
        build_proposal({"sku": "SKU-LAMP-01", "discount_percent": "ten percent"})


def test_non_string_sku_raises():
    with pytest.raises(ProposalError):
        build_proposal({"sku": 12345, "discount_percent": 5})


def test_absurd_discount_is_passed_through_for_the_policy_engine_to_reject():
    """80% off is not this layer's problem to catch.

    Validation belongs in one place. If this layer silently clamped the value,
    the audit log would show an approved offer and never record that the model
    tried to give away the store.
    """
    proposal = build_proposal({"sku": "SKU-LAMP-01", "discount_percent": 80})
    product = CATALOG["SKU-LAMP-01"]
    assert proposal.discount_paise == int(product.price_paise * 0.8)
