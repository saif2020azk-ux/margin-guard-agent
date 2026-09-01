"""Tests for the audit log, gateway, and orchestrator.

No network. The gateway is faked and the agent is a plain function, so every
path -- including the failures -- runs deterministically.
"""

import sqlite3

import pytest

import audit
import orchestrator
from catalog import CATALOG
from gateway import (
    FakeGateway,
    PermanentGatewayError,
    RazorpayGateway,
    TransientGatewayError,
    reference_for,
)
from policy import Cart, CartItem, OfferProposal, PolicyConfig

NOW = 1_000_000.0


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(audit.SCHEMA)
    return c


@pytest.fixture
def cart():
    return Cart("cust_001", (CartItem("SKU-DESK-01", 1),))


def proposing(sku, pct=5, pitch="Pairs well with your desk"):
    """Build a fake agent that always proposes the same thing."""
    product = CATALOG.get(sku)
    paise = int(product.price_paise * pct / 100) if product else 0
    return lambda cart: OfferProposal(sku, paise, pitch)


def declining(cart):
    return None


def exploding(cart):
    raise RuntimeError("model unavailable")


# ---------------------------------------------------------------- audit log


def test_log_is_hash_chained(conn):
    for i in range(3):
        audit.record(
            conn,
            audit.LogEntry(f"c{i}", ["SKU-DESK-01"], "policy_engine",
                           "SKU-LAMP-01", 1000, True, "ok", []),
        )
    ok, msg = audit.verify_chain(conn)
    assert ok, msg


def test_tampering_is_detected(conn):
    audit.record(
        conn,
        audit.LogEntry("c1", ["SKU-DESK-01"], "policy_engine",
                       "SKU-LAMP-01", 1000, True, "ok", []),
    )
    audit.record(
        conn,
        audit.LogEntry("c2", ["SKU-DESK-01"], "policy_engine",
                       "SKU-LAMP-01", 2000, True, "ok", []),
    )
    # Someone edits history directly in the database.
    conn.execute("UPDATE decisions SET discount_paise = 99999 WHERE id = 1")
    conn.commit()

    ok, msg = audit.verify_chain(conn)
    assert not ok
    assert "altered" in msg


def test_budget_derived_only_from_approved_offers(conn):
    audit.record(
        conn,
        audit.LogEntry("c1", [], "policy_engine", "SKU-LAMP-01", 5_000, True, "ok", []),
    )
    audit.record(
        conn,
        audit.LogEntry("c2", [], "policy_engine", "SKU-LAMP-01", 90_000, False,
                       "margin floor", []),
    )
    state = audit.campaign_state(conn)
    assert state.spent_paise == 5_000  # the rejected 90,000 must not count


# ------------------------------------------------------------------ gateway


def test_reference_id_is_stable():
    a = reference_for("cust_1", "SKU-LAMP-01", 5000)
    b = reference_for("cust_1", "SKU-LAMP-01", 5000)
    assert a == b


def test_fake_gateway_is_idempotent():
    gw = FakeGateway()
    first = gw.create_link(customer_id="c1", sku="SKU-LAMP-01",
                           description="x", amount_paise=5000)
    second = gw.create_link(customer_id="c1", sku="SKU-LAMP-01",
                            description="x", amount_paise=5000)
    assert first.id == second.id
    assert len(gw.created) == 1


def test_zero_amount_is_permanently_rejected():
    gw = FakeGateway()
    with pytest.raises(PermanentGatewayError):
        gw.create_link(customer_id="c1", sku="SKU-LAMP-01",
                       description="x", amount_paise=0)


def test_transient_failures_are_retried_then_succeed():
    class Flaky:
        def __init__(self):
            self.attempts = 0
            self.payment_link = self

        def create(self, payload):
            self.attempts += 1
            if self.attempts < 3:
                raise ConnectionError("connection reset by peer")
            return {"id": "plink_1", "short_url": "https://rzp.io/i/x"}

    flaky = Flaky()
    gw = RazorpayGateway(client=flaky, backoff=0)
    link = gw.create_link(customer_id="c1", sku="SKU-LAMP-01",
                          description="x", amount_paise=5000)
    assert link.id == "plink_1"
    assert flaky.attempts == 3


def test_permanent_failures_are_not_retried():
    class Broken:
        def __init__(self):
            self.attempts = 0
            self.payment_link = self

        def create(self, payload):
            self.attempts += 1
            raise ValueError("invalid payload: bad currency")

    broken = Broken()
    gw = RazorpayGateway(client=broken, backoff=0)
    with pytest.raises(TransientGatewayError):
        gw.create_link(customer_id="c1", sku="SKU-LAMP-01",
                       description="x", amount_paise=5000)
    assert broken.attempts == 1  # tried once, recognised it was hopeless


# ------------------------------------------------------------- orchestrator


def test_happy_path_creates_link_and_logs(conn, cart):
    gw = FakeGateway()
    outcome = orchestrator.run(
        cart, conn=conn, gateway=gw,
        propose=proposing("SKU-LAMP-01"), now=NOW,
    )
    assert outcome.offered
    assert outcome.payment_link is not None
    assert len(gw.calls) == 1

    rows = audit.recent(conn)
    assert len(rows) == 1
    assert rows[0]["approved"] == 1
    assert rows[0]["payment_link"]


def test_rejected_offer_creates_no_payment_link(conn, cart):
    """A thin-margin product must never reach the gateway."""
    gw = FakeGateway()
    outcome = orchestrator.run(
        cart, conn=conn, gateway=gw,
        propose=proposing("SKU-CABLE-C", pct=10), now=NOW,
    )
    assert outcome.status == "rejected"
    assert gw.calls == []  # the gateway was never called at all

    rows = audit.recent(conn)
    assert rows[0]["approved"] == 0
    assert "margin" in rows[0]["reason"]


def test_agent_declining_is_recorded_not_treated_as_failure(conn, cart):
    gw = FakeGateway()
    outcome = orchestrator.run(
        cart, conn=conn, gateway=gw, propose=declining, now=NOW,
    )
    assert outcome.status == "declined"
    rows = audit.recent(conn)
    assert rows[0]["actor"] == "agent"
    assert rows[0]["proposed_sku"] is None


def test_agent_crash_is_contained_and_logged(conn, cart):
    gw = FakeGateway()
    outcome = orchestrator.run(
        cart, conn=conn, gateway=gw, propose=exploding, now=NOW,
    )
    assert outcome.status == "error"
    assert gw.calls == []
    rows = audit.recent(conn)
    assert rows[0]["error"]


def test_gateway_outage_logs_approval_and_the_failure(conn, cart):
    """The decision was sound. The gateway broke. Both facts are recorded."""
    gw = FakeGateway(always_fail=True)
    outcome = orchestrator.run(
        cart, conn=conn, gateway=gw,
        propose=proposing("SKU-LAMP-01"), now=NOW,
    )
    assert outcome.status == "error"
    rows = audit.recent(conn)
    assert rows[0]["approved"] == 1        # policy said yes
    assert rows[0]["error"]                 # gateway said no
    assert rows[0]["payment_link"] is None


def test_budget_exhaustion_stops_offers(conn, cart):
    """Run until the campaign budget runs out, then confirm it stops."""
    gw = FakeGateway()
    config = PolicyConfig(campaign_budget_paise=30_000, offer_cooldown_seconds=0)

    outcomes = []
    for i in range(6):
        c = Cart(f"cust_{i}", (CartItem("SKU-DESK-01", 1),))
        outcomes.append(
            orchestrator.run(
                c, conn=conn, gateway=gw,
                propose=proposing("SKU-LAMP-01", pct=5),
                config=config, now=NOW + i,
            )
        )

    assert any(o.offered for o in outcomes)
    assert any("budget" in (o.decision.reason if o.decision else "") for o in outcomes)

    spent = audit.campaign_state(conn).spent_paise
    assert spent <= config.campaign_budget_paise


def test_chain_stays_intact_across_mixed_outcomes(conn, cart):
    gw = FakeGateway()
    orchestrator.run(cart, conn=conn, gateway=gw,
                     propose=proposing("SKU-LAMP-01"), now=NOW)
    orchestrator.run(cart, conn=conn, gateway=gw,
                     propose=proposing("SKU-CABLE-C", pct=10), now=NOW + 1)
    orchestrator.run(cart, conn=conn, gateway=gw, propose=declining, now=NOW + 2)
    orchestrator.run(cart, conn=conn, gateway=gw, propose=exploding, now=NOW + 3)

    ok, msg = audit.verify_chain(conn)
    assert ok, msg
    assert len(audit.recent(conn)) == 4
