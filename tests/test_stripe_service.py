from app.services import settings_store, stripe_service


def _enable_stripe(session, days_before="3"):
    settings_store.set_value(session, "stripe_secret_key", "sk_test_123")
    settings_store.set_value(session, "stripe_enabled", "true")
    settings_store.set_value(session, "stripe_reminder_days_before", days_before)


class _Resp:
    def __init__(self, data, status_code=200):
        self._d = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


def test_enabled_requires_flag_and_key(db_session):
    assert stripe_service.enabled() is False
    settings_store.set_value(db_session, "stripe_secret_key", "sk_test_123")
    assert stripe_service.enabled() is False  # flag still off
    settings_store.set_value(db_session, "stripe_enabled", "true")
    assert stripe_service.enabled() is True


def test_test_raises_key_error_when_unset(db_session):
    try:
        stripe_service.test()
        assert False, "expected StripeKeyError"
    except stripe_service.StripeKeyError:
        pass


def test_test_raises_key_error_on_401(db_session, monkeypatch):
    _enable_stripe(db_session)
    monkeypatch.setattr(
        stripe_service.httpx, "get", lambda *a, **k: _Resp({}, status_code=401)
    )
    try:
        stripe_service.test()
        assert False, "expected StripeKeyError"
    except stripe_service.StripeKeyError:
        pass


def test_test_ok_returns_balance(db_session, monkeypatch):
    _enable_stripe(db_session)
    monkeypatch.setattr(
        stripe_service.httpx, "get", lambda *a, **k: _Resp({"object": "balance"})
    )
    assert stripe_service.test()["object"] == "balance"


def test_create_payment_link_two_step(db_session, monkeypatch):
    _enable_stripe(db_session)
    calls = []

    def _post(url, headers=None, data=None, timeout=None):
        calls.append((url, data))
        if url.endswith("/prices"):
            return _Resp({"id": "price_123"})
        return _Resp({"id": "plink_123", "url": "https://buy.stripe.com/plink_123"})

    monkeypatch.setattr(stripe_service.httpx, "post", _post)
    link = stripe_service.create_payment_link(
        amount_cents=500, currency="EUR", description="Bronze — Mario",
        metadata={"type": "renewal", "renewal_id": "9"},
    )
    assert link == {"id": "plink_123", "url": "https://buy.stripe.com/plink_123"}
    assert calls[0][0].endswith("/prices")
    assert calls[0][1]["unit_amount"] == "500"
    assert calls[0][1]["currency"] == "eur"
    assert calls[1][0].endswith("/payment_links")
    assert calls[1][1]["line_items[0][price]"] == "price_123"
    assert calls[1][1]["metadata[renewal_id]"] == "9"


def test_create_payment_link_requires_key(db_session):
    try:
        stripe_service.create_payment_link(
            amount_cents=500, currency="EUR", description="x", metadata={}
        )
        assert False, "expected StripeKeyError"
    except stripe_service.StripeKeyError:
        pass


# ---- webhook signature verification ----

def test_verify_webhook_signature_roundtrip():
    import hashlib
    import hmac
    import time

    secret = "whsec_test"
    payload = b'{"id": "evt_1"}'
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert stripe_service.verify_webhook_signature(payload, header, secret) is True


def test_verify_webhook_signature_rejects_bad_sig():
    header = "t=123,v1=deadbeef"
    assert stripe_service.verify_webhook_signature(b"{}", header, "whsec_test") is False


def test_verify_webhook_signature_rejects_stale_timestamp():
    import hashlib
    import hmac

    secret = "whsec_test"
    payload = b"{}"
    ts = "1000000000"  # ancient
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert stripe_service.verify_webhook_signature(payload, header, secret) is False


def test_verify_webhook_signature_rejects_missing_secret():
    assert stripe_service.verify_webhook_signature(b"{}", "t=1,v1=x", "") is False
