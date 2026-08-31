# Build notes

## Aug 31 — environment setup

- Razorpay test-mode order created successfully on the first attempt.
- Gemini 2.5 Flash returned 404: retired for new projects. Google's error
  named the replacement. The model name was already read from .env rather
  than hardcoded, so the fix was one config line and no code change.
- pytest was not on PATH on Windows; switched to `python -m pytest`.
- Accidentally opened an interactive rebase to fix a duplicated commit
  message; aborted it rather than risk the history for a cosmetic gain.

Policy engine: 12 tests. Agent layer: 12 tests. All green.