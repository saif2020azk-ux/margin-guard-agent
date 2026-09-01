"""Razorpay payment link creation, with retries and injectable failures.

The gateway is wrapped rather than called directly for three reasons:

1. **Idempotency.** Every link carries a `reference_id` derived from the
   customer and the offer. Retrying after a timeout reuses the same reference,
   so a network failure cannot produce two payment links for one offer.

2. **Retries with backoff.** Transient network errors are retried. Errors that
   will never succeed on retry -- a bad payload, a rejected amount -- are not.
   Retrying a 400 forever is a bug, not resilience.

3. **Testability and demos.** `FakeGateway` implements the same interface, so
   the whole system can be exercised without touching the network, and the
   chaos harness can force specific failures on demand.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


class GatewayError(Exception):
    """Payment link could not be created."""


class TransientGatewayError(GatewayError):
    """Worth retrying: timeout, connection reset, 5xx."""


class PermanentGatewayError(GatewayError):
    """Not worth retrying: bad payload, invalid amount, auth failure."""


@dataclass(frozen=True)
class PaymentLink:
    id: str
    short_url: str
    amount_paise: int
    reference_id: str


def reference_for(customer_id: str, sku: str, amount_paise: int) -> str:
    """A stable id for this specific offer to this specific customer.

    Same offer retried == same reference == Razorpay returns the existing link
    rather than creating a second one.
    """
    return f"offer_{customer_id}_{sku}_{amount_paise}"


class RazorpayGateway:
    """Thin wrapper over the Razorpay SDK."""

    def __init__(self, client=None, *, max_attempts: int = 3, backoff: float = 0.5):
        if client is None:
            import razorpay

            client = razorpay.Client(
                auth=(
                    os.environ["RAZORPAY_KEY_ID"],
                    os.environ["RAZORPAY_KEY_SECRET"],
                )
            )
        self._client = client
        self.max_attempts = max_attempts
        self.backoff = backoff

    def create_link(
        self, *, customer_id: str, sku: str, description: str, amount_paise: int
    ) -> PaymentLink:
        if amount_paise <= 0:
            raise PermanentGatewayError("amount must be positive")

        reference = reference_for(customer_id, sku, amount_paise)
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "description": description[:255],
            "reference_id": reference,
            "notes": {"customer_id": customer_id, "sku": sku},
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = self._client.payment_link.create(payload)
                return PaymentLink(
                    id=result["id"],
                    short_url=result.get("short_url", ""),
                    amount_paise=amount_paise,
                    reference_id=reference,
                )
            except PermanentGatewayError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if not _is_transient(exc) or attempt == self.max_attempts:
                    break
                time.sleep(self.backoff * attempt)

        raise TransientGatewayError(
            f"failed after {self.max_attempts} attempts: {last_error}"
        ) from last_error


def _is_transient(exc: Exception) -> bool:
    """Decide whether an error is worth another attempt.

    Errs on the side of NOT retrying. A retried permanent failure wastes time
    and can confuse a demo; a non-retried transient failure is at least honest.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    transient_markers = (
        "timeout", "timed out", "connection", "temporarily",
        "unavailable", "reset", "502", "503", "504",
    )
    return any(marker in text for marker in transient_markers)


class FakeGateway:
    """In-memory gateway for tests, demos, and the chaos harness.

    `fail_times` makes the next N calls raise a transient error before
    succeeding -- which is how the retry path gets exercised on camera.
    """

    def __init__(self, *, fail_times: int = 0, always_fail: bool = False):
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.calls: list[dict] = []
        self.created: dict[str, PaymentLink] = {}

    def create_link(
        self, *, customer_id: str, sku: str, description: str, amount_paise: int
    ) -> PaymentLink:
        self.calls.append(
            {"customer_id": customer_id, "sku": sku, "amount_paise": amount_paise}
        )

        if amount_paise <= 0:
            raise PermanentGatewayError("amount must be positive")

        if self.always_fail:
            raise TransientGatewayError("simulated outage")

        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientGatewayError("simulated timeout")

        reference = reference_for(customer_id, sku, amount_paise)

        # Idempotency: the same reference returns the same link.
        if reference in self.created:
            return self.created[reference]

        link = PaymentLink(
            id=f"plink_fake_{len(self.created) + 1}",
            short_url=f"https://rzp.io/i/fake{len(self.created) + 1}",
            amount_paise=amount_paise,
            reference_id=reference,
        )
        self.created[reference] = link
        return link
