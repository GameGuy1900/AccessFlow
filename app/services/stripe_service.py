"""Stripe integration: Payment Links + webhook signature verification.

Plain httpx calls against Stripe's REST API (no `stripe` SDK dependency, kept
lean like the rest of this app's third-party integrations). All calls no-op
(raise/return None) when unconfigured, mirroring overseerr_service.
"""
import hashlib
import hmac
import time

import httpx

from app import runtime_config

_API = "https://api.stripe.com/v1"
_TIMEOUT = 15


def _cfg() -> dict:
    return runtime_config.stripe_config()


def enabled() -> bool:
    c = _cfg()
    return c["enabled"] and bool(c["secret_key"])


def _headers(c: dict) -> dict:
    return {"Authorization": f"Bearer {c['secret_key']}"}


class StripeConnectionError(RuntimeError):
    """Stripe API unreachable (DNS, refused, timeout)."""


class StripeKeyError(RuntimeError):
    """Stripe API reached but the secret key was rejected (401)."""


def test() -> dict:
    """Validate the secret key via GET /v1/balance (auth-gated, cheap)."""
    c = _cfg()
    if not c["secret_key"]:
        raise StripeKeyError("Stripe secret key not set")
    try:
        resp = httpx.get(f"{_API}/balance", headers=_headers(c), timeout=_TIMEOUT)
    except httpx.RequestError as exc:
        raise StripeConnectionError(str(exc)) from exc
    if resp.status_code == 401:
        raise StripeKeyError("HTTP 401")
    resp.raise_for_status()
    return resp.json()


def create_payment_link(
    *,
    amount_cents: int,
    currency: str,
    description: str,
    metadata: dict[str, str],
) -> dict:
    """Create a one-off Price + Payment Link for it. Returns {"id", "url"}.

    Payment Links can't take metadata directly usable for webhook lookups on
    the *session* unless it's echoed onto checkout via `line_items` — metadata
    passed here is instead attached to the Payment Link itself and copied by
    Stripe onto the resulting `checkout.session` object, which is what the
    webhook handler reads.
    """
    c = _cfg()
    if not c["secret_key"]:
        raise StripeKeyError("Stripe secret key not set")

    price_data = {
        "unit_amount": str(amount_cents),
        "currency": currency.lower(),
        "product_data[name]": description,
    }
    try:
        price_resp = httpx.post(
            f"{_API}/prices", headers=_headers(c), data=price_data, timeout=_TIMEOUT
        )
    except httpx.RequestError as exc:
        raise StripeConnectionError(str(exc)) from exc
    if price_resp.status_code == 401:
        raise StripeKeyError("HTTP 401")
    price_resp.raise_for_status()
    price_id = price_resp.json()["id"]

    link_data = {
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
    }
    for k, v in metadata.items():
        link_data[f"metadata[{k}]"] = v
    try:
        link_resp = httpx.post(
            f"{_API}/payment_links", headers=_headers(c), data=link_data,
            timeout=_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise StripeConnectionError(str(exc)) from exc
    link_resp.raise_for_status()
    body = link_resp.json()
    return {"id": body["id"], "url": body["url"]}


def verify_webhook_signature(
    payload: bytes, sig_header: str, secret: str, *, tolerance_seconds: int = 300
) -> bool:
    """Stripe's documented scheme: header is `t=<ts>,v1=<hex_hmac>[,v0=...]`.
    Signed string is `f"{ts}.{payload}"`, HMAC-SHA256 with the webhook secret.
    Rejects stale timestamps to block replay of a captured request."""
    if not secret or not sig_header:
        return False
    parts = dict(
        item.split("=", 1) for item in sig_header.split(",") if "=" in item
    )
    ts = parts.get("t")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False
    try:
        if abs(time.time() - int(ts)) > tolerance_seconds:
            return False
    except ValueError:
        return False
    signed_payload = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)
