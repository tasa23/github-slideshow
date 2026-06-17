# IaC Build — security hardening update (deploy guide)

This bundle contains a hardened `app.py` plus tests. It is a **drop-in replacement
for `app.py`** in your existing deploy bundle — nothing else in your bundle
(Dockerfile, templates, `landing.html`, `app.html`, `requirements.txt`) needs to
change, and **no new environment variables or Azure resources are required** (all
new settings have safe built-in defaults).

> Scope note: this pass covers the changes that are pure code and low-risk to a
> live deploy. It deliberately does **not** change the model/cost profile, touch
> your SSE streaming, or add infrastructure. See "Still open" at the bottom.

---

## 1. What changed (and why)

All changes are in `app.py`. Diff it against your current file before deploying
(`diff -u old/app.py app.py`).

| Area | Change | Security-plan item |
|------|--------|--------------------|
| **LLM model pin** | `/api/generate` now uses a **server-pinned** model (`LLM_MODEL`, default `claude-sonnet-4-6`). The client-supplied `model` field is ignored. | P1-5 |
| **OTP strength** | Codes are now **6-digit, cryptographically random** (`secrets`), stored as an **HMAC** (never plaintext), with **server-side expiry** (10 min) and **lockout** after 5 wrong attempts. Single-use. | P0-1 |
| **Password-reset authorization** | `/recover_reset` now **requires a verified OTP in the session**. Previously the reset step did not check that the code had been entered, so anyone who set `recover_email` (a plain POST to `/recover_lookup`) could POST `/recover_reset` and change another account's password. This is the most important fix here. | (gap found during review) |
| **Registration authorization** | `/profile` (account creation) likewise requires a verified OTP, so accounts can't be created for an unverified email by skipping the code step. | P0-1 related |
| **Session fixation** | `login()` and the OAuth callback now rotate the session (`session.clear()` then re-issue) via a shared `_login_user()` helper. | P1-3 |
| **Email enumeration** | `/recover_lookup` returns a **uniform** "if that account exists, a code has been sent" response whether or not the email exists. | P2 (upgrades) |
| **Session version** | `SESSION_VERSION` bumped `3 → 4`, so all pre-deploy sessions are invalidated once (everyone is logged out a single time after deploy). | — |

New env vars (all **optional**, defaults shown):

| Env var | Default | Purpose |
|---------|---------|---------|
| `LLM_MODEL` | `claude-sonnet-4-6` | The pinned generation model. |
| `LLM_MAX_TOKENS` | `8000` | Hard clamp on output tokens (unchanged value). |
| `LLM_DEFAULT_TOKENS` | `4000` | Default when the client omits `max_tokens` (unchanged value). |
| `OTP_TTL_SECONDS` | `600` | OTP validity window. |
| `OTP_MAX_ATTEMPTS` | `5` | Wrong attempts before lockout. |

---

## 2. Test before you ship (optional but recommended)

From the bundle root (where `app.py` and `templates/` are):

```bash
pip install -r requirements-dev.txt
LOCAL_DEV=1 FLASK_SECRET_KEY=test-secret pytest -v
```

This exercises the OTP logic, the reset-authorization fix, and the model pin
with no Azure dependencies and no network calls.

---

## 3. Deploy (Azure Cloud Shell)

This follows your existing build/deploy pattern. Replace only `app.py` in the
bundle, then rebuild and roll the revision.

```bash
# 0) Note the current image so you can roll back instantly if needed.
PREV=$(az containerapp show -n ca-iacb-web -g rg-iacb-lean \
  --query "properties.template.containers[0].image" -o tsv)
echo "rollback image: $PREV"

# 1) Unzip your current deploy bundle (adjust the zip name/path to yours).
rm -rf ~/iacb && mkdir -p ~/iacb && unzip -o ~/your-deploy-bundle.zip -d ~/iacb

# 2) Drop in the updated app.py (from this update bundle).
cp /path/to/iac-aistudio-update/app.py ~/iacb/app.py
cd ~/iacb

# 3) Build the image in ACR (Dockerfile base must already exist in ACR:
#    FROM acriacblean.azurecr.io/python:3.12-slim-bookworm).
TAG=$(date +%Y%m%d-%H%M)
az acr build --registry acriacblean --image iacb-web:$TAG .

# 4) Roll the Container App to the new image.
az containerapp update -n ca-iacb-web -g rg-iacb-lean \
  --image "acriacblean.azurecr.io/iacb-web:$TAG"
```

Optional — override any of the new settings (not required; defaults are baked in):

```bash
az containerapp update -n ca-iacb-web -g rg-iacb-lean \
  --set-env-vars LLM_MODEL=claude-sonnet-4-6 OTP_TTL_SECONDS=600 OTP_MAX_ATTEMPTS=5
```

---

## 4. Post-deploy verification

```bash
# Health (also proves DB connectivity)
curl -fsS https://iac-aistudio.com/healthz && echo
```

Then click through once:

1. **Register** a throwaway account → confirm the email code is now **6 digits**,
   that a **wrong** code is rejected, and that completing the flow logs you in.
2. **Forgot password** for that account → confirm reset works **only after**
   entering the code, and that entering an **unknown** email gives the same
   "if that account exists…" message (no "no account found").
3. **Reset bypass is closed**: a direct POST to `/recover_reset` without first
   verifying a code must NOT change any password (the test asserts this too).
4. **Model pin**: in App Insights / container logs, confirm generations run on
   `LLM_MODEL` regardless of what the client sends.
5. Everyone is logged out once (expected — `SESSION_VERSION` bump).

---

## 5. Rollback (instant)

```bash
az containerapp update -n ca-iacb-web -g rg-iacb-lean --image "$PREV"
```

---

## 6. Still open (not in this code drop)

- **P0-2 — shared rate-limit store.** The limiter is still in-memory (per
  replica). This needs an **Azure Cache for Redis** resource provisioned first,
  then `storage_uri="redis://…"` on the `Limiter(...)`. Infrastructure, not a
  pure code change — do it as a follow-up.
- **CSP `'unsafe-inline'`** on `script-src` (P1-4) — requires externalizing
  inline JS to `/static/js/*.js` and moving to nonces. Large, deferred.
- **Central input-validation layer** (P1-5 cont.) — Pydantic/marshmallow on POST
  bodies. Deferred.
- **Monthly LLM spend cap / WAF / image scanning** — roadmap items.
