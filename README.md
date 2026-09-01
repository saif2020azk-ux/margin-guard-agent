# margin-guard-agent

An AI upsell agent for Razorpay merchants, built so that the language model
proposes and deterministic code decides.

Submitted to the Razorpay AI Buildathon, Track 01 (AI Growth & Agentic
Commerce).

---

## The problem

Wiring a language model into a discount engine is easy. Wiring one in without
losing money is not.

A model asked to suggest an upsell will happily propose a 10% discount on a
product carrying an 8% margin. The suggestion looks reasonable, sits well
inside any sensible percentage cap, and sells below cost every time. The model
is not being stupid -- it has no idea what anything costs the merchant, and it
should not.

So the question this project answers is not "can an AI suggest products." It
is: **what has to be true before an AI is allowed to give away margin?**

---

## The answer

The model proposes. A deterministic policy engine decides. Nothing reaches a
customer, and no payment link is created, unless eight checks pass.

```
   customer cart
         |
         v
  +---------------+        +------------------+
  |   agent.py    |------->|    catalog.py    |
  | Gemini: one   |        | names and prices |
  | suggestion    |        | NO cost prices   |
  +---------------+        +------------------+
         |
         |  OfferProposal (untrusted)
         v
  +--------------------------------------------------+
  |                    policy.py                     |
  |  1. sku_exists           5. discount_ceiling     |
  |  2. not_already_in_cart  6. margin_floor         |
  |  3. in_stock             7. campaign_budget      |
  |  4. discount_well_formed 8. customer_cooldown    |
  |                                                  |
  |  pure function -- no AI, no network, no I/O      |
  +--------------------------------------------------+
         |                              |
    approved                       rejected
         |                              |
         v                              |
  +---------------+                     |
  |  gateway.py   |                     |
  | Razorpay link |                     |
  | retry + idem  |                     |
  +---------------+                     |
         |                              |
         +--------------+---------------+
                        v
              +--------------------+
              |      audit.py      |
              | append-only,       |
              | hash-chained log   |
              +--------------------+
```

`orchestrator.py` sequences these and owns no business rules of its own.

---

## Results

50 synthetic carts, three strategies, same catalog and same seed.

Strategy B is the honest control: ship whatever the model proposes, unchecked.
It is what a lot of "AI upsell" demos are underneath.

| Strategy | Offers | Blocked | Discount given | Loss-making | Exposure | Exp. margin* |
|---|---|---|---|---|---|---|
| A. Naive rule, no AI | 30 | 0 | Rs 1,920 | **30** | Rs 320 | **-Rs 64** |
| B. LLM, no guardrail | 23 | 0 | Rs 4,566 | **7** | Rs 133 | Rs 2,979 |
| C. LLM + guardrail | 12 | 11 | Rs 3,943 | **0** | Rs 0 | Rs 2,983 |

\* Expected margin is **modelled, not measured**. It assumes an acceptance rate
as a function of discount size; the assumption is stated in `evaluate.py` and
is crude. Every other column is arithmetic from the catalog with no guesswork.

Three things this shows:

1. **The naive rule loses money on every sale.** "Suggest a cheap accessory,
   give 10% off" is a rule many real widgets implement. Cheap accessories carry
   the thinnest margins, so the flat discount wipes them out. Expected margin
   is negative.

2. **An unguarded model ships losses about 30% of the time.** Seven of 23
   offers below cost -- not from stupidity, from missing information.

3. **The guardrail halved the offers and left margin unchanged.** 12 offers
   versus 23, expected margin within rounding of identical, zero loss-making
   offers. The restraint cost nothing.

The Rs 4 gap between B and C is not a win for C -- it is noise in a modelled
number. The claim being made is narrower and firmer: **the guardrail removed
every loss-making offer without measurably reducing margin.**

---

## Design decisions worth defending

**The model never sees cost price.** `catalog_for_prompt()` sends SKU, name and
list price only. The model cannot reason about margins because it is not told
them, so it cannot talk itself into a loss-making discount, and a prompt
injection has nothing to work with. This is a property of what was withheld,
not of a check that was added.

**All money is integer paise.** `0.1 + 0.2 != 0.3` in binary floating point.
Rupees are stored as integers throughout; there is no float arithmetic on any
monetary value.

**Two checks, not one.** A percentage ceiling alone is insufficient. See
`test_margin_floor_is_rejected`: a 10% discount is well within a 20% ceiling
and still sells the cable below cost. Only a check that knows the cost catches
it.

**A hallucinated SKU becomes a rejected proposal, not an exception.** Raising
in `agent.py` would lose the record. Letting it through means the policy engine
rejects it with a logged `sku_exists` failure, and the audit trail contains
evidence that the model hallucinated and the system caught it.

**Campaign state is derived, not stored.** There is no `budget_spent` counter.
It is computed by summing discounts across approved decisions in the log. A
counter can drift out of sync with what actually happened; a derivation cannot.

**The agent may decline.** `sku: null` is a valid response and is recorded as
such. In the 30-cart run it declined 7 times. An agent that stays quiet on a
cart with no good complement is behaving correctly, not failing.

**Retries distinguish transient from permanent.** A connection reset is retried
with backoff. An invalid payload is not -- retrying sends the same invalid
payload. See `test_permanent_failures_are_not_retried`.

---

## Failure handling

Four failure modes, each demonstrable live via `--chaos`:

| Mode | Command | Behaviour |
|---|---|---|
| Model proposes an absurd discount | `--chaos rogue` | Blocked by ceiling and margin floor; logged; no gateway call |
| Model invents a product | `--chaos hallucinate` | Blocked by `sku_exists`; logged |
| Discount within ceiling but below cost | `--chaos thin-margin` | Ceiling **passes**, margin floor blocks |
| Payment provider down | `--chaos outage` | Approval and failure both logged; no payment link; budget not consumed |
| Payment provider flaky | `--chaos flaky` | Two timeouts, retried with backoff, succeeds |

In every case the audit log records what happened. A merchant can distinguish
"we chose not to offer" from "we tried and the provider was down".

---

## The audit log

Append-only. No update, no delete. Each row stores the SHA-256 of its contents
plus the previous row's hash, so altering any historical row breaks every hash
after it.

```
python demo.py --verify
```

`test_tampering_is_detected` edits a row directly in SQLite and confirms
`verify_chain()` catches it and names the row.

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env        # then add your keys
python verify_setup.py      # checks keys, both APIs, git hygiene
```

```bash
python -m pytest -v         # 39 tests

python demo.py --fresh                  # live agent, simulated gateway
python demo.py --live                   # real Razorpay test-mode links
python demo.py --chaos thin-margin      # the interesting one
python demo.py --log                    # full audit trail
python demo.py --verify                 # check the chain

python evaluate.py --no-llm             # instant, naive rule only
python evaluate.py --carts 30           # full three-way comparison
```

Model responses cache to `eval_cache.json`, so re-running the evaluation is
free. Calls are spaced 6 seconds apart for free-tier rate limits.

---

## Layout

| File | Purpose |
|---|---|
| `policy.py` | The eight checks. Pure, deterministic, no AI. |
| `catalog.py` | Synthetic merchant catalog, shaped to exercise the checks. |
| `agent.py` | The only module that calls a language model. |
| `gateway.py` | Razorpay payment links, retries, idempotency, fake for tests. |
| `audit.py` | Hash-chained append-only log; derives campaign state. |
| `orchestrator.py` | Sequences the above. Owns no rules. |
| `demo.py` | CLI demo with failure injection. |
| `evaluate.py` | Three-strategy comparison across synthetic carts. |
| `verify_setup.py` | Environment checks. |

Tests live alongside as `test_*.py`.

---

## What is not built

Stated plainly, because a demo pretending to be a product is worse than a demo
that knows what it is:

- No customer acceptance data. The revenue column is modelled, not observed.
- The catalog is synthetic and small (14 products).
- No authentication, no multi-merchant support, no deployment.
- Payment links are created but never paid; nothing tracks conversion.
- The acceptance model in `evaluate.py` is a guess and is labelled as one.

The margin-safety numbers do not depend on any of these. They are arithmetic.
