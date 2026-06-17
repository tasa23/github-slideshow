"""
Security regression tests for the IaC Build app (the 2026-06 hardening pass).

Run from the app bundle root (where app.py and templates/ live), with the
runtime deps installed and LOCAL_DEV on:

    pip install -r requirements-dev.txt
    LOCAL_DEV=1 FLASK_SECRET_KEY=test-secret pytest -v

These cover the changes made in this update:
  - OTP: 6-digit, single-use, expiry, lockout            (P0-1)
  - Password reset cannot proceed without a verified OTP (takeover fix)
  - /api/generate ignores the client-supplied "model"    (P1-5 pin)
"""
import os

os.environ["LOCAL_DEV"] = "1"
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-for-ci")

import time  # noqa: E402
import app as appmod  # noqa: E402

app = appmod.app
app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


# ----- OTP ------------------------------------------------------------------

def test_otp_is_six_digits_and_single_use():
    with app.test_request_context():
        code = appmod.issue_otp()
        assert len(code) == 6 and code.isdigit()
        ok, _ = appmod.verify_otp(code)
        assert ok is True
        # Code is consumed on success — a replay must fail.
        ok2, _ = appmod.verify_otp(code)
        assert ok2 is False


def test_otp_wrong_code_rejected():
    with app.test_request_context():
        code = appmod.issue_otp()
        wrong = "000000" if code != "000000" else "111111"
        ok, _ = appmod.verify_otp(wrong)
        assert ok is False


def test_otp_expires():
    with app.test_request_context() as ctx:
        code = appmod.issue_otp()
        ctx.session["otp_ts"] = 0  # issued at the epoch -> long expired
        ok, msg = appmod.verify_otp(code)
        assert ok is False and "expired" in msg.lower()


def test_otp_locks_out_after_max_attempts():
    with app.test_request_context():
        code = appmod.issue_otp()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(appmod.OTP_MAX_ATTEMPTS):
            appmod.verify_otp(wrong)
        # Next attempt — even with the RIGHT code — is locked out.
        ok, msg = appmod.verify_otp(code)
        assert ok is False and "request a new" in msg.lower()


# ----- Password-reset authorization ----------------------------------------

def test_recover_reset_blocked_without_verified_otp():
    client = app.test_client()
    # Simulate an attacker who set recover_email (a plain POST to
    # /recover_lookup) but never verified an OTP.
    with client.session_transaction() as s:
        s["recover_email"] = "victim@example.com"
        # deliberately NO recover_verified
    resp = client.post("/recover_reset", data={"new_password": "NewPassw0rd1"})
    body = resp.get_data(as_text=True).lower()
    # The reset must NOT have happened.
    assert "password reset. please log in" not in body


# ----- /api/generate model pin (P1-5) ---------------------------------------

def test_api_generate_pins_model(monkeypatch):
    client = app.test_client()
    appmod._LLM_KEY = "test-key"
    appmod.ADMIN_EMAILS = {"admin@example.com"}

    with app.app_context():
        u = appmod.User(email="admin@example.com", password_hash="x",
                        first_name="A", last_name="B", phone="")
        appmod.db.session.add(u)
        appmod.db.session.commit()
        uid = u.id

    captured = {}

    class _Resp:
        text = '{"ok": true}'
        status_code = 200

    def fake_post(*args, **kwargs):
        captured["body"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(appmod.requests, "post", fake_post)

    with client.session_transaction() as s:
        s["user_id"] = uid
        s["sv"] = appmod.SESSION_VERSION
        s["la"] = time.time()

    client.post("/api/generate", json={
        "model": "claude-please-bill-me-more",   # should be ignored
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert captured["body"]["model"] == appmod.LLM_MODEL
    assert captured["body"]["model"] != "claude-please-bill-me-more"
