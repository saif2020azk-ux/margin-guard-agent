"""The LLM layer: turns a cart into a *proposed* offer.

This is the only module in the project that talks to a language model, and it
is deliberately thin. Everything it returns is a proposal, not a decision --
`policy.evaluate` has the final word on whether any money moves.

Three design points worth defending in a review:

1. **Provider isolation.** One function, one SDK import. Swapping Gemini for
   another provider is a change to this file alone.

2. **The model never sees cost price.** It cannot reason about margins because
   it is not told them. This limits the blast radius of a bad or manipulated
   response.

3. **Structured output is parsed and validated, never trusted.** The model is
   asked for JSON; we parse it defensively and hand the result to the policy
   engine, which re-checks everything anyway.
"""

from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv

from catalog import CATALOG, catalog_for_prompt, rupees
from policy import Cart, OfferProposal

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

SYSTEM_PROMPT = """You are a merchandising assistant for an online store that \
sells work-from-home equipment. Given a customer's cart, suggest ONE \
complementary product to offer them, or decline to suggest anything.

Rules:
- Suggest a genuine complement, not a random product and not a near-duplicate \
of something already in the cart.
- If nothing in the catalog genuinely complements the cart, decline. Declining \
is a valid and often correct answer. Do not force a suggestion.
- discount_percent may be 0. Only suggest a discount if it is likely to change \
the customer's mind, and never above 15.
- The pitch must be one sentence, factual, and free of urgency or pressure \
tactics.

Respond with ONLY a JSON object, no markdown fences, no commentary:

{"sku": "SKU-XXX", "discount_percent": 0, "pitch": "one sentence", \
"reasoning": "why this complements the cart"}

To decline, respond with:

{"sku": null, "reasoning": "why nothing fits"}
"""


class ProposalError(Exception):
    """Raised when the model's response cannot be used."""


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Models wrap JSON in markdown fences roughly as often as not, regardless of
    instructions. Handle it rather than failing on it.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        braces = re.search(r"\{.*\}", text, re.DOTALL)
        if braces:
            text = braces.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalError(f"model did not return parseable JSON: {exc}") from exc


def _cart_description(cart: Cart) -> str:
    lines = []
    for item in cart.items:
        product = CATALOG.get(item.sku)
        if product is None:
            lines.append(f"{item.sku} x{item.qty} (unknown product)")
        else:
            lines.append(
                f"{product.sku} | {product.name} x{item.qty} "
                f"| {rupees(product.price_paise)}"
            )
    return "\n".join(lines)


def propose_offer(cart: Cart, *, client=None) -> OfferProposal | None:
    """Ask the model for an upsell proposal. Returns None if it declines.

    The returned proposal is UNVALIDATED. Pass it to `policy.evaluate` before
    acting on it. A None here means the agent chose not to offer -- which is a
    legitimate outcome, not an error.
    """
    if client is None:
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"CUSTOMER'S CART:\n{_cart_description(cart)}\n\n"
        f"AVAILABLE CATALOG (sku | name | price):\n"
        f"{catalog_for_prompt(exclude=set(cart.skus))}\n"
    )

    response = client.models.generate_content(model=MODEL, contents=prompt)
    data = _extract_json(response.text or "")

    return build_proposal(data)


def build_proposal(data: dict) -> OfferProposal | None:
    """Convert a parsed model response into an OfferProposal.

    Separated from the API call so it can be unit tested without a network
    round trip -- including against deliberately malformed responses.
    """
    sku = data.get("sku")
    if sku is None:
        return None  # the model declined, which is allowed

    if not isinstance(sku, str):
        raise ProposalError(f"sku must be a string, got {type(sku).__name__}")

    pitch = data.get("pitch") or ""
    if not isinstance(pitch, str):
        raise ProposalError("pitch must be a string")

    raw_pct = data.get("discount_percent", 0)
    try:
        pct = float(raw_pct)
    except (TypeError, ValueError) as exc:
        raise ProposalError(f"discount_percent not numeric: {raw_pct!r}") from exc

    # Convert a percentage into paise against the product's *list* price.
    # If the SKU is unknown we cannot compute an amount -- but we still build
    # the proposal with a zero discount so the policy engine gets to be the one
    # that rejects it, and the rejection lands in the audit log with a proper
    # sku_exists failure rather than an exception here.
    product = CATALOG.get(sku)
    if product is None:
        return OfferProposal(sku=sku, discount_paise=0, pitch=pitch.strip())

    discount_paise = int(product.price_paise * pct / 100)

    return OfferProposal(
        sku=sku,
        discount_paise=discount_paise,
        pitch=pitch.strip(),
    )
