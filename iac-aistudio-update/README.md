# IaC Build — security hardening update

Drop-in replacement for `app.py` in the IaC-AIStudio deploy bundle, plus tests
and CI. Hardens authentication and pins the LLM model server-side, with no new
Azure resources and no required config changes.

**Start here:** [`IMPLEMENTATION_GUIDE.md`](./IMPLEMENTATION_GUIDE.md) — what
changed, how to test, and the exact Cloud Shell deploy + rollback steps.

## Contents

| File | What it is |
|------|------------|
| `app.py` | Hardened application (replaces your current `app.py`) |
| `IMPLEMENTATION_GUIDE.md` | Deploy guide + change log + verification checklist |
| `tests/test_security.py` | Security regression tests (OTP, reset auth, model pin) |
| `requirements-dev.txt` | Minimal deps to run the tests in `LOCAL_DEV` mode |
| `.gitlab-ci.yml` | Runs the tests on every push/MR (no secrets) |
| `requirements.txt` | Your runtime deps (unchanged copy, for reference) |

## Quick test

```bash
pip install -r requirements-dev.txt
LOCAL_DEV=1 FLASK_SECRET_KEY=test-secret pytest -v
```

## Summary of changes

- **Model pin (P1-5):** `/api/generate` ignores the client `model`; pinned via `LLM_MODEL`.
- **OTP (P0-1):** 6-digit crypto codes, HMAC-stored, 10-min expiry, 5-attempt lockout, single-use.
- **Reset/registration authorization:** both now require a verified OTP (closes a password-reset bypass).
- **Session fixation (P1-3):** session rotates on login and OAuth callback.
- **Email enumeration:** uniform recovery response.

See the guide for the full table and the items still left open (Redis limiter,
CSP nonces, validation layer).
