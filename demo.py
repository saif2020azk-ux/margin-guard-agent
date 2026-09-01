"""Demo runner: shows the system deciding, in plain English.

    python demo.py              # run every scenario against the fake gateway
    python demo.py --live       # use real Razorpay test-mode payment links
    python demo.py --chaos outage      # payment provider is down
    python demo.py --chaos flaky       # times out twice, then recovers
    python demo.py --chaos rogue       # the agent proposes an 80% discount
    python demo.py --chaos hallucinate # the agent invents a product
    python demo.py --verify     # check the audit log has not been tampered with
    python demo.py --log        # print the audit trail

The scenarios are chosen so that a viewer sees an offer made, an offer refused
for a good reason, the agent staying quiet, and a failure handled -- in that
order, without anyone having to explain what they are looking at.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import audit
import orchestrator
from agent import propose_offer
from catalog import CATALOG, rupees
from gateway import FakeGateway, RazorpayGateway
from policy import Cart, CartItem, OfferProposal, PolicyConfig

DEMO_DB = "demo_audit.db"

BAR = "=" * 72


# --------------------------------------------------------------------------
# Scenario carts
# --------------------------------------------------------------------------

SCENARIOS = [
    (
        "A desk buyer with room to upsell",
        Cart("cust_101", (CartItem("SKU-DESK-01", 1),)),
    ),
    (
        "A full workstation order",
        Cart("cust_102", (CartItem("SKU-DESK-01", 1), CartItem("SKU-CHAIR-01", 1))),
    ),
    (
        "A single cheap cable -- not worth an offer",
        Cart("cust_103", (CartItem("SKU-CABLE-C", 1),)),
    ),
    (
        "A monitor buyer",
        Cart("cust_104", (CartItem("SKU-MON-27", 1),)),
    ),
]


# --------------------------------------------------------------------------
# Scripted agents for the chaos modes
# --------------------------------------------------------------------------


def rogue_agent(cart: Cart) -> OfferProposal:
    """Proposes a discount that would destroy the margin."""
    product = CATALOG["SKU-LAMP-01"]
    return OfferProposal(
        "SKU-LAMP-01",
        int(product.price_paise * 0.80),
        "Incredible once-in-a-lifetime deal, 80% off!",
    )


def hallucinating_agent(cart: Cart) -> OfferProposal:
    """Proposes a product that does not exist."""
    return OfferProposal("SKU-KEYBOARD-PRO-MAX", 5_000, "Our best keyboard")


def thin_margin_agent(cart: Cart) -> OfferProposal:
    """Discounts a product that cannot afford it -- within the % ceiling."""
    product = CATALOG["SKU-CABLE-C"]
    return OfferProposal(
        "SKU-CABLE-C",
        int(product.price_paise * 0.10),
        "10% off a cable, seems harmless",
    )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def show(title: str, cart: Cart, outcome) -> None:
    items = ", ".join(CATALOG[i.sku].name for i in cart.items if i.sku in CATALOG)
    print(f"\n{BAR}")
    print(f"  {title}")
    print(f"  Cart: {items}")
    print(BAR)

    icon = {
        "offered": "OFFER MADE",
        "rejected": "OFFER BLOCKED",
        "declined": "NO OFFER",
        "error": "FAILURE HANDLED",
    }[outcome.status]

    print(f"\n  [{icon}]  {outcome.message}")

    if outcome.decision:
        print("\n  Policy checks:")
        for check in outcome.decision.checks:
            mark = "ok  " if check.passed else "FAIL"
            print(f"    {mark}  {check.name:22} {check.detail}")

    if outcome.payment_link:
        print(f"\n  Payment link: {outcome.payment_link.short_url}")
        print(f"  Reference:    {outcome.payment_link.reference_id}")


def print_log(conn) -> None:
    rows = audit.recent(conn, limit=50)
    print(f"\n{BAR}\n  AUDIT TRAIL ({len(rows)} entries, newest first)\n{BAR}")
    for row in reversed(rows):
        stamp = time.strftime("%H:%M:%S", time.localtime(row["ts"]))
        verdict = "APPROVED" if row["approved"] else "REJECTED"
        print(f"\n  #{row['id']}  {stamp}  {verdict}  by {row['actor']}")
        print(f"      customer:  {row['customer_id']}")
        print(f"      proposed:  {row['proposed_sku'] or '(none)'}"
              f"   discount: {rupees(row['discount_paise'])}")
        print(f"      reason:    {row['reason']}")
        if row["payment_link"]:
            print(f"      link:      {row['payment_link']}")
        if row["error"]:
            print(f"      error:     {row['error']}")
        checks = json.loads(row["checks"])
        failed = [c["name"] for c in checks if not c["passed"]]
        if failed:
            print(f"      failed:    {', '.join(failed)}")


def print_summary(conn, config: PolicyConfig) -> None:
    state = audit.campaign_state(conn)
    rows = audit.recent(conn, limit=1000)
    # An offer that failed at the gateway was approved but never delivered --
    # count it as a failure, not as revenue.
    offered = sum(1 for r in rows if r["approved"] and not r["error"])
    blocked = sum(1 for r in rows if not r["approved"] and r["proposed_sku"])
    failed = sum(1 for r in rows if r["error"])
    quiet = sum(1 for r in rows if not r["proposed_sku"] and not r["error"])

    print(f"\n{BAR}\n  SUMMARY\n{BAR}")
    print(f"  Offers made:            {offered}")
    print(f"  Offers blocked:         {blocked}")
    print(f"  Agent stayed quiet:     {quiet}")
    print(f"  Failures handled:       {failed}")
    print(f"  Discount given away:    {rupees(state.spent_paise)}"
          f"  of {rupees(config.campaign_budget_paise)} budget")

    ok, msg = audit.verify_chain(conn)
    print(f"  Audit chain:            {msg}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Margin-guarded upsell agent demo")
    parser.add_argument("--live", action="store_true",
                        help="use real Razorpay test-mode API")
    parser.add_argument("--chaos", choices=["outage", "flaky", "rogue",
                                            "hallucinate", "thin-margin"],
                        help="inject a specific failure mode")
    parser.add_argument("--verify", action="store_true",
                        help="check the audit chain and exit")
    parser.add_argument("--log", action="store_true",
                        help="print the audit trail and exit")
    parser.add_argument("--fresh", action="store_true",
                        help="delete the demo database first")
    args = parser.parse_args()

    if args.fresh and os.path.exists(DEMO_DB):
        os.remove(DEMO_DB)

    conn = audit.connect(DEMO_DB)
    config = PolicyConfig()

    if args.verify:
        ok, msg = audit.verify_chain(conn)
        print(f"\n  Audit chain: {msg}\n")
        return 0 if ok else 1

    if args.log:
        print_log(conn)
        return 0

    # --- choose the agent -------------------------------------------------
    if args.chaos == "rogue":
        propose, note = rogue_agent, "the agent has been told to propose 80% off"
    elif args.chaos == "hallucinate":
        propose, note = hallucinating_agent, "the agent invents a product that does not exist"
    elif args.chaos == "thin-margin":
        propose, note = thin_margin_agent, "a 10% discount on a product with an 8% margin"
    else:
        propose, note = propose_offer, None

    # --- choose the gateway -----------------------------------------------
    if args.chaos == "outage":
        gateway = FakeGateway(always_fail=True)
        note = "the payment provider is completely down"
    elif args.chaos == "flaky":
        gateway = FakeGateway(fail_times=2)
        note = "the payment provider times out twice, then recovers"
    elif args.live:
        gateway = RazorpayGateway()
    else:
        gateway = FakeGateway()

    print(f"\n{BAR}")
    print("  MARGIN-GUARDED UPSELL AGENT")
    print(f"  gateway: {'Razorpay test mode' if args.live else 'simulated'}")
    if note:
        print(f"  chaos:   {note}")
    print(f"  margin floor {config.min_margin_bps // 100}%"
          f"   discount ceiling {config.max_discount_bps // 100}%"
          f"   budget {rupees(config.campaign_budget_paise)}")
    print(BAR)

    for title, cart in SCENARIOS:
        outcome = orchestrator.run(
            cart, conn=conn, gateway=gateway, propose=propose, config=config
        )
        show(title, cart, outcome)

    print_summary(conn, config)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
