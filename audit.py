"""Append-only audit log of every offer decision.

Two properties matter here:

1. **Append-only.** There is no update and no delete. A wrong entry is
   corrected by writing a new one, never by editing history.

2. **Hash chained.** Each row stores the SHA-256 of its own contents plus the
   previous row's hash. Change any historical row and every hash after it stops
   matching, so `verify_chain()` can prove the log has not been rewritten. This
   is the same idea a bank ledger uses, and it costs about ten lines.

The log is also the source of truth for campaign state. Rather than keeping a
separate running total of budget spent -- which could drift out of sync with
what actually happened -- we derive it from the decisions themselves.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass

from policy import CampaignState, Decision

DB_PATH = "audit.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    customer_id   TEXT    NOT NULL,
    cart_skus     TEXT    NOT NULL,
    actor         TEXT    NOT NULL,
    proposed_sku  TEXT,
    discount_paise INTEGER NOT NULL DEFAULT 0,
    approved      INTEGER NOT NULL,
    reason        TEXT    NOT NULL,
    checks        TEXT    NOT NULL,
    payment_link  TEXT,
    error         TEXT,
    prev_hash     TEXT    NOT NULL,
    row_hash      TEXT    NOT NULL
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _hash_row(payload: dict, prev_hash: str) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")) + prev_hash
    return hashlib.sha256(blob.encode()).hexdigest()


def _last_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT row_hash FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    return row["row_hash"] if row else "genesis"


@dataclass(frozen=True)
class LogEntry:
    """What happened, and why. One row per decision."""

    customer_id: str
    cart_skus: list[str]
    actor: str                 # "agent" or "policy_engine" or "gateway"
    proposed_sku: str | None
    discount_paise: int
    approved: bool
    reason: str
    checks: list[dict]
    payment_link: str | None = None
    error: str | None = None


def record(conn: sqlite3.Connection, entry: LogEntry, *, now: float | None = None) -> int:
    """Append one decision. Returns the row id."""
    now = time.time() if now is None else now
    prev = _last_hash(conn)

    payload = {
        "ts": now,
        "customer_id": entry.customer_id,
        "cart_skus": entry.cart_skus,
        "actor": entry.actor,
        "proposed_sku": entry.proposed_sku,
        "discount_paise": entry.discount_paise,
        "approved": entry.approved,
        "reason": entry.reason,
        "checks": entry.checks,
        "payment_link": entry.payment_link,
        "error": entry.error,
    }
    row_hash = _hash_row(payload, prev)

    cur = conn.execute(
        """INSERT INTO decisions
           (ts, customer_id, cart_skus, actor, proposed_sku, discount_paise,
            approved, reason, checks, payment_link, error, prev_hash, row_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now,
            entry.customer_id,
            json.dumps(entry.cart_skus),
            entry.actor,
            entry.proposed_sku,
            entry.discount_paise,
            int(entry.approved),
            entry.reason,
            json.dumps(entry.checks),
            entry.payment_link,
            entry.error,
            prev,
            row_hash,
        ),
    )
    conn.commit()
    return cur.lastrowid


def record_decision(
    conn: sqlite3.Connection,
    customer_id: str,
    cart_skus: list[str],
    decision: Decision,
    *,
    payment_link: str | None = None,
    error: str | None = None,
    now: float | None = None,
) -> int:
    """Convenience wrapper: turn a policy Decision into a log entry."""
    return record(
        conn,
        LogEntry(
            customer_id=customer_id,
            cart_skus=cart_skus,
            actor="policy_engine",
            proposed_sku=decision.proposal.sku,
            discount_paise=decision.proposal.discount_paise,
            approved=decision.approved,
            reason=decision.reason,
            checks=[
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in decision.checks
            ],
            payment_link=payment_link,
            error=error,
        ),
        now=now,
    )


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Recompute every hash. Returns (ok, message).

    If someone edits a historical row directly in the database, this catches it
    and names the first row that fails.
    """
    prev = "genesis"
    for row in conn.execute("SELECT * FROM decisions ORDER BY id"):
        payload = {
            "ts": row["ts"],
            "customer_id": row["customer_id"],
            "cart_skus": json.loads(row["cart_skus"]),
            "actor": row["actor"],
            "proposed_sku": row["proposed_sku"],
            "discount_paise": row["discount_paise"],
            "approved": bool(row["approved"]),
            "reason": row["reason"],
            "checks": json.loads(row["checks"]),
            "payment_link": row["payment_link"],
            "error": row["error"],
        }
        expected = _hash_row(payload, prev)
        if row["prev_hash"] != prev:
            return False, f"row {row['id']}: previous hash does not match"
        if row["row_hash"] != expected:
            return False, f"row {row['id']}: contents have been altered"
        prev = row["row_hash"]
    return True, "chain intact"


def campaign_state(
    conn: sqlite3.Connection, *, window_seconds: float = 86_400
) -> CampaignState:
    """Derive current campaign state from the log itself.

    Budget spent is the sum of discounts on APPROVED offers only -- a rejected
    proposal costs nothing and must not consume budget. Deriving this rather
    than storing a counter means the log and the state can never disagree.
    """
    spent = conn.execute(
        "SELECT COALESCE(SUM(discount_paise), 0) AS total "
        "FROM decisions WHERE approved = 1"
    ).fetchone()["total"]

    cutoff = time.time() - window_seconds
    last_offer: dict[str, float] = {}
    for row in conn.execute(
        "SELECT customer_id, MAX(ts) AS last_ts FROM decisions "
        "WHERE approved = 1 AND ts >= ? GROUP BY customer_id",
        (cutoff,),
    ):
        last_offer[row["customer_id"]] = row["last_ts"]

    return CampaignState(spent_paise=int(spent), last_offer_at=last_offer)


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))
    )
