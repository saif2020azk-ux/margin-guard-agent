"""Evaluation: does the guardrail actually matter?

Runs the same synthetic carts through three strategies and reports what each
one would have done to the merchant's margin.

    A. Naive rule      -- most expensive complement, flat 10% off. No AI.
    B. LLM unguarded   -- whatever the model proposes, shipped as-is.
    C. LLM + guardrail -- the model proposes, the policy engine decides.

B is the honest control. It is what you get if you wire a language model
straight into a discount engine, which is what a lot of "AI upsell" demos
actually are. The interesting number is how much money B gives away that C
does not.

    python evaluate.py                 # full run
    python evaluate.py --carts 20      # shorter run
    python evaluate.py --no-llm        # A only, no API calls, instant

A NOTE ON HONESTY
-----------------
Two kinds of number appear below and they are not equally trustworthy:

  * MEASURED -- offers made, discount given away, offers that would have sold
    below cost. These follow arithmetically from the catalog and the proposals.
    No guesswork.

  * MODELLED -- expected revenue and margin. These require assuming how often
    a customer accepts an offer, and there is no customer here. The assumption
    is stated in ACCEPTANCE_MODEL below, it is crude, and the numbers derived
    from it should be read as illustrative only.

The measured numbers are the argument. The modelled ones are context.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

from catalog import CATALOG, rupees
from policy import (
    Cart,
    CampaignState,
    CartItem,
    OfferProposal,
    PolicyConfig,
    Product,
    evaluate as policy_evaluate,
)

CACHE_PATH = "eval_cache.json"
SEED = 20260901

# How often a customer is assumed to accept. Crude on purpose, and stated
# openly so a reader can discount conclusions drawn from it.
ACCEPTANCE_MODEL = {
    "base_rate": 0.08,          # 8% take an offer at full price
    "per_percent_discount": 0.012,   # each 1% off adds 1.2 points
    "cap": 0.45,
}


def acceptance_probability(discount_bps: int) -> float:
    pct = discount_bps / 100
    p = ACCEPTANCE_MODEL["base_rate"] + pct * ACCEPTANCE_MODEL["per_percent_discount"]
    return min(p, ACCEPTANCE_MODEL["cap"])


# --------------------------------------------------------------------------
# Synthetic carts
# --------------------------------------------------------------------------


def generate_carts(n: int, seed: int = SEED) -> list[Cart]:
    """Deterministic synthetic carts so runs are comparable."""
    rng = random.Random(seed)
    skus = [s for s, p in CATALOG.items() if p.stock > 0]
    carts = []
    for i in range(n):
        size = rng.choices([1, 2, 3], weights=[55, 30, 15])[0]
        chosen = rng.sample(skus, size)
        carts.append(
            Cart(f"eval_cust_{i:03d}", tuple(CartItem(s, 1) for s in chosen))
        )
    return carts


# --------------------------------------------------------------------------
# Strategy A: naive rule, no AI
# --------------------------------------------------------------------------


def naive_rule(cart: Cart) -> OfferProposal | None:
    """The obvious approach: push a cheap accessory, flat 10% off.

    Accessory cross-sell is the most common real pattern -- "customers also
    bought this cable" -- so the cheapest available complement is a fair
    strawman rather than a weak one. It has no knowledge of cost and no notion
    of complement; it just picks something small and discounts it.

    This is exactly where the danger lives: cheap accessories tend to carry the
    thinnest margins in a catalog, so a flat percentage discount on them is far
    more likely to sell below cost than the same discount on a monitor.
    """
    candidates = [
        p for p in CATALOG.values()
        if p.stock > 0 and p.sku not in cart.skus
    ]
    if not candidates:
        return None
    cheapest = min(candidates, key=lambda p: p.price_paise)
    return OfferProposal(
        cheapest.sku, int(cheapest.price_paise * 0.10), "You might also like this"
    )


# --------------------------------------------------------------------------
# Strategy B/C: the LLM, cached
# --------------------------------------------------------------------------


def cart_key(cart: Cart) -> str:
    return "|".join(sorted(i.sku for i in cart.items))


def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as fh:
            return json.load(fh)
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w") as fh:
        json.dump(cache, fh, indent=2)


def llm_proposals(carts: list[Cart], *, delay: float) -> dict[str, dict | None]:
    """Fetch one proposal per distinct cart shape, caching to disk.

    Distinct cart shapes, not distinct carts -- two customers with the same
    items get the same suggestion, so there is no reason to pay for it twice.
    Cached across runs, so re-running the evaluation costs nothing.
    """
    from agent import ProposalError, propose_offer

    cache = load_cache()
    keys = {cart_key(c): c for c in carts}
    todo = [k for k in keys if k not in cache]

    if todo:
        print(f"  Calling the model for {len(todo)} distinct carts "
              f"({len(keys) - len(todo)} already cached)...")

    for n, key in enumerate(todo, 1):
        cart = keys[key]
        try:
            proposal = propose_offer(cart)
            cache[key] = (
                None if proposal is None
                else {
                    "sku": proposal.sku,
                    "discount_paise": proposal.discount_paise,
                    "pitch": proposal.pitch,
                }
            )
        except ProposalError as exc:
            cache[key] = {"error": f"unparseable: {exc}"}
        except Exception as exc:  # noqa: BLE001
            cache[key] = {"error": f"{type(exc).__name__}: {exc}"}

        print(f"    [{n}/{len(todo)}] {key[:40]}")
        save_cache(cache)          # save as we go; a crash loses one call
        if n < len(todo):
            time.sleep(delay)      # free tier is rate limited

    return {k: cache.get(k) for k in keys}


def to_proposal(raw: dict | None) -> tuple[OfferProposal | None, str | None]:
    if raw is None:
        return None, None
    if "error" in raw:
        return None, raw["error"]
    return (
        OfferProposal(raw["sku"], raw["discount_paise"], raw.get("pitch", "")),
        None,
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


class Tally:
    def __init__(self, name: str):
        self.name = name
        self.offers = 0
        self.blocked = 0
        self.quiet = 0
        self.errors = 0
        self.discount_paise = 0
        self.loss_making = 0          # offers that would sell below cost
        self.loss_exposure_paise = 0  # how much would have been lost
        self.expected_margin = 0.0
        self.invalid_sku = 0

    def ship(self, product: Product, discount_paise: int) -> None:
        """Record an offer actually reaching a customer."""
        self.offers += 1
        self.discount_paise += discount_paise
        selling = product.price_paise - discount_paise
        margin = selling - product.cost_paise
        if margin < 0:
            self.loss_making += 1
            self.loss_exposure_paise += -margin
        bps = int(discount_paise * 10_000 / product.price_paise) if product.price_paise else 0
        self.expected_margin += acceptance_probability(bps) * margin

    def row(self) -> list[str]:
        return [
            self.name,
            str(self.offers),
            str(self.blocked),
            str(self.quiet + self.errors),
            rupees(self.discount_paise),
            str(self.loss_making),
            rupees(self.loss_exposure_paise),
            rupees(int(self.expected_margin)),
        ]


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(r[i]) for r in [headers] + rows) for i in range(len(headers))]
    line = "  ".join("-" * w for w in widths)
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)), line]
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the guardrail's effect")
    parser.add_argument("--carts", type=int, default=50)
    parser.add_argument("--no-llm", action="store_true",
                        help="skip API calls; evaluate the naive rule only")
    parser.add_argument("--delay", type=float, default=6.0,
                        help="seconds between model calls (free tier is ~10/min)")
    args = parser.parse_args()

    carts = generate_carts(args.carts)
    config = PolicyConfig()

    print(f"\n{'=' * 78}")
    print(f"  EVALUATION  --  {len(carts)} synthetic carts, seed {SEED}")
    print(f"  margin floor {config.min_margin_bps // 100}%   "
          f"discount ceiling {config.max_discount_bps // 100}%   "
          f"budget {rupees(config.campaign_budget_paise)}")
    print("=" * 78)

    naive = Tally("A. Naive rule (no AI)")
    unguarded = Tally("B. LLM, no guardrail")
    guarded = Tally("C. LLM + guardrail")

    # --- Strategy A ------------------------------------------------------
    for cart in carts:
        proposal = naive_rule(cart)
        if proposal is None:
            naive.quiet += 1
            continue
        naive.ship(CATALOG[proposal.sku], proposal.discount_paise)

    # --- Strategies B and C ----------------------------------------------
    if not args.no_llm:
        print("\n  Gathering model proposals...")
        raw = llm_proposals(carts, delay=args.delay)

        state = CampaignState()
        spent = 0
        for cart in carts:
            proposal, error = to_proposal(raw[cart_key(cart)])

            if error:
                unguarded.errors += 1
                guarded.errors += 1
                continue
            if proposal is None:
                unguarded.quiet += 1
                guarded.quiet += 1
                continue

            product = CATALOG.get(proposal.sku)

            # B: ship whatever the model said, no checks at all.
            if product is None:
                unguarded.invalid_sku += 1
                unguarded.errors += 1
            else:
                unguarded.ship(product, proposal.discount_paise)

            # C: the policy engine decides, with budget carried forward.
            decision = policy_evaluate(
                proposal, cart, CATALOG, config,
                CampaignState(spent_paise=spent), time.time(),
            )
            if decision.approved:
                guarded.ship(product, proposal.discount_paise)
                spent += proposal.discount_paise
            else:
                guarded.blocked += 1

    # --- Report -----------------------------------------------------------
    headers = ["Strategy", "Offers", "Blocked", "None", "Discount given",
               "Loss-making", "Exposure", "Exp. margin*"]
    rows = [naive.row()]
    if not args.no_llm:
        rows += [unguarded.row(), guarded.row()]

    print(f"\n{table(rows, headers)}")
    print("\n  * modelled, not measured -- see the acceptance assumption in this file")

    # --- The headline -----------------------------------------------------
    print(f"\n{'=' * 78}\n  WHAT THE GUARDRAIL CHANGED\n{'=' * 78}")

    if args.no_llm:
        print(f"\n  Naive rule alone would have shipped {naive.loss_making} "
              f"loss-making offers,\n  exposing {rupees(naive.loss_exposure_paise)} "
              f"in negative margin.")
    else:
        prevented = unguarded.loss_making - guarded.loss_making
        saved = unguarded.loss_exposure_paise - guarded.loss_exposure_paise
        held_back = unguarded.discount_paise - guarded.discount_paise

        print(f"\n  Loss-making offers prevented:   {prevented}")
        print(f"  Negative margin avoided:        {rupees(saved)}")
        print(f"  Discount withheld:              {rupees(held_back)}")
        print(f"  Invalid products caught:        {unguarded.invalid_sku}")
        print(f"  Offers blocked by policy:       {guarded.blocked}")

        if guarded.loss_making == 0:
            print("\n  The guarded system shipped zero loss-making offers.")
        else:
            print(f"\n  WARNING: {guarded.loss_making} loss-making offers got "
                  f"through the guardrail. Investigate.")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
