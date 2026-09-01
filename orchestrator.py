"""The orchestrator: one cart in, one recorded outcome out.

This is the only module that knows the full sequence. It deliberately owns no
business rules of its own -- it routes between components and makes sure
everything that happens gets written down.

The sequence:

    cart -> agent proposes -> policy engine decides -> gateway (if approved)
                                     |
                                     +--> audit log (always, either way)

Note what happens on every branch, including the unhappy ones: something is
always written to the audit log. An offer that was never made and a payment
link that failed to create are both facts a merchant needs to be able to see.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from audit import LogEntry, campaign_state, connect, record, record_decision
from catalog import CATALOG, rupees
from gateway import GatewayError, PaymentLink
from policy import Cart, Decision, PolicyConfig, evaluate


@dataclass(frozen=True)
class Outcome:
    """What the system did for one cart, and why."""

    customer_id: str
    status: str            # "offered" | "rejected" | "declined" | "error"
    decision: Decision | None
    payment_link: PaymentLink | None
    message: str

    @property
    def offered(self) -> bool:
        return self.status == "offered"


def run(
    cart: Cart,
    *,
    conn,
    gateway,
    propose,
    config: PolicyConfig | None = None,
    now: float | None = None,
) -> Outcome:
    """Process one cart end to end.

    Dependencies are injected rather than imported so the whole pipeline can be
    tested without a network, and so the chaos harness can swap in a failing
    gateway without touching this file.
    """
    config = config or PolicyConfig()
    now = time.time() if now is None else now
    cart_skus = [item.sku for item in cart.items]

    # --- 1. Ask the agent for a proposal --------------------------------
    try:
        proposal = propose(cart)
    except Exception as exc:  # noqa: BLE001
        record(
            conn,
            LogEntry(
                customer_id=cart.customer_id,
                cart_skus=cart_skus,
                actor="agent",
                proposed_sku=None,
                discount_paise=0,
                approved=False,
                reason=f"agent failed: {type(exc).__name__}",
                checks=[],
                error=str(exc),
            ),
            now=now,
        )
        return Outcome(
            cart.customer_id, "error", None, None,
            f"Agent failed, no offer made: {exc}",
        )

    # --- 2. The agent chose to stay quiet -------------------------------
    if proposal is None:
        record(
            conn,
            LogEntry(
                customer_id=cart.customer_id,
                cart_skus=cart_skus,
                actor="agent",
                proposed_sku=None,
                discount_paise=0,
                approved=False,
                reason="agent declined to make an offer",
                checks=[],
            ),
            now=now,
        )
        return Outcome(
            cart.customer_id, "declined", None, None,
            "No offer: nothing in the catalog complemented this cart.",
        )

    # --- 3. The policy engine decides -----------------------------------
    state = campaign_state(conn)
    decision = evaluate(proposal, cart, CATALOG, config, state, now)

    if not decision.approved:
        record_decision(conn, cart.customer_id, cart_skus, decision, now=now)
        return Outcome(
            cart.customer_id, "rejected", decision, None,
            f"Offer blocked: {decision.reason}",
        )

    # --- 4. Approved: create the payment link ---------------------------
    product = CATALOG[proposal.sku]
    try:
        link = gateway.create_link(
            customer_id=cart.customer_id,
            sku=proposal.sku,
            description=f"{product.name} - {proposal.pitch}",
            amount_paise=decision.final_price_paise,
        )
    except GatewayError as exc:
        # The decision was sound; the gateway failed. Record BOTH facts. The
        # budget is not consumed, because no offer reached the customer.
        record_decision(
            conn, cart.customer_id, cart_skus, decision,
            error=f"{type(exc).__name__}: {exc}", now=now,
        )
        return Outcome(
            cart.customer_id, "error", decision, None,
            f"Offer approved but payment link failed: {exc}",
        )

    record_decision(
        conn, cart.customer_id, cart_skus, decision,
        payment_link=link.short_url or link.id, now=now,
    )

    saving = product.price_paise - decision.final_price_paise
    return Outcome(
        cart.customer_id, "offered", decision, link,
        f"Offered {product.name} at {rupees(decision.final_price_paise)} "
        f"(saving {rupees(saving)}): {proposal.pitch}",
    )
