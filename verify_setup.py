"""One-shot check that the development environment is wired up correctly.

Run this whenever something feels off:

    python verify_setup.py

It checks five things and keeps going after a failure, so you see every
problem at once rather than fixing them one reload at a time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    line = f"{status} {name}"
    if detail:
        line += f"\n       {detail}"
    print(line)


# ---------------------------------------------------------------------------
# 1. Environment file
# ---------------------------------------------------------------------------
print("\n--- Checking environment ---")

try:
    from dotenv import load_dotenv

    load_dotenv()
    record(PASS, "python-dotenv installed and .env loaded")
except ImportError:
    record(FAIL, "python-dotenv not installed", "run: pip install python-dotenv")

gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
rzp_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
rzp_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

if not gemini_key:
    record(FAIL, "GEMINI_API_KEY missing", "add it to .env")
elif gemini_key.startswith("your_") or "xxxx" in gemini_key.lower():
    record(FAIL, "GEMINI_API_KEY still a placeholder")
else:
    record(PASS, "GEMINI_API_KEY present", f"starts with {gemini_key[:6]}...")

if not rzp_id:
    record(FAIL, "RAZORPAY_KEY_ID missing", "add it to .env")
elif not rzp_id.startswith("rzp_test_"):
    record(
        WARN,
        "RAZORPAY_KEY_ID is not a test key",
        "it should start with rzp_test_ -- never build against live keys",
    )
else:
    record(PASS, "RAZORPAY_KEY_ID present and is a test key")

if not rzp_secret or "xxxx" in rzp_secret.lower():
    record(FAIL, "RAZORPAY_KEY_SECRET missing or placeholder")
else:
    record(PASS, "RAZORPAY_KEY_SECRET present")


# ---------------------------------------------------------------------------
# 2. Git hygiene -- is .env actually being ignored?
# ---------------------------------------------------------------------------
print("\n--- Checking git hygiene ---")

if not os.path.isdir(".git"):
    record(WARN, "no git repository yet", "run: git init")
else:
    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.split()
        if ".env" in tracked:
            record(
                FAIL,
                ".env IS TRACKED BY GIT",
                "run: git rm --cached .env   then rotate both keys",
            )
        else:
            record(PASS, ".env is not tracked by git")

        ignored = subprocess.run(
            ["git", "check-ignore", ".env"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ignored.returncode == 0:
            record(PASS, ".gitignore is correctly excluding .env")
        else:
            record(FAIL, ".gitignore is NOT excluding .env", "check the filename")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "could not run git", str(exc))


# ---------------------------------------------------------------------------
# 3. Policy engine
# ---------------------------------------------------------------------------
print("\n--- Checking policy engine ---")

try:
    from policy import (
        Cart,
        CartItem,
        CampaignState,
        OfferProposal,
        PolicyConfig,
        Product,
        evaluate,
    )

    catalog = {"SKU-A": Product("SKU-A", "Widget", 100_000, 60_000, 10)}
    cart = Cart("cust_test", (CartItem("SKU-B", 1),))

    good = evaluate(
        OfferProposal("SKU-A", 10_000, "test"),
        cart, catalog, PolicyConfig(), CampaignState(), time.time(),
    )
    bad = evaluate(
        OfferProposal("SKU-A", 90_000, "too generous"),
        cart, catalog, PolicyConfig(), CampaignState(), time.time(),
    )

    if good.approved and not bad.approved:
        record(PASS, "policy engine approves valid and rejects invalid offers")
    else:
        record(FAIL, "policy engine behaving unexpectedly",
               f"good={good.approved} bad={bad.approved}")
except Exception as exc:  # noqa: BLE001
    record(FAIL, "could not exercise policy engine", str(exc))


# ---------------------------------------------------------------------------
# 4. Gemini
# ---------------------------------------------------------------------------
print("\n--- Checking Gemini API ---")

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not gemini_key:
    record(WARN, "skipping Gemini call", "no key set")
else:
    try:
        from google import genai

        client = genai.Client(api_key=gemini_key)
        resp = client.models.generate_content(
            model=MODEL, contents="Reply with exactly: OK"
        )
        text = (resp.text or "").strip()
        if "OK" in text.upper():
            record(PASS, f"Gemini responded using {MODEL}")
        else:
            record(WARN, "Gemini responded unexpectedly", f"got: {text[:60]}")
    except ImportError:
        record(FAIL, "google-genai not installed", "run: pip install google-genai")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "Gemini call failed", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 5. Razorpay test mode
# ---------------------------------------------------------------------------
print("\n--- Checking Razorpay test mode ---")

if not (rzp_id and rzp_secret):
    record(WARN, "skipping Razorpay call", "keys not set")
else:
    try:
        import razorpay

        client = razorpay.Client(auth=(rzp_id, rzp_secret))
        order = client.order.create(
            {
                "amount": 10_000,  # paise -> Rs 100
                "currency": "INR",
                "receipt": f"setup_check_{int(time.time())}",
                "notes": {"purpose": "environment verification"},
            }
        )
        record(PASS, "Razorpay test order created", f"order id: {order['id']}")
    except ImportError:
        record(FAIL, "razorpay SDK not installed", "run: pip install razorpay")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "Razorpay call failed", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
failed = [r for r in results if r[0] == FAIL]
warned = [r for r in results if r[0] == WARN]

print("\n" + "=" * 60)
print(f"{len(results) - len(failed) - len(warned)} passed, "
      f"{len(warned)} warnings, {len(failed)} failed")
if failed:
    print("\nFix these before continuing:")
    for _, name, detail in failed:
        print(f"  - {name}" + (f" ({detail})" if detail else ""))
    sys.exit(1)
print("\nEnvironment looks good. Go build.")
