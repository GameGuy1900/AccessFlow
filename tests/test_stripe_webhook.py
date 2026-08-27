import hashlib
import hmac
import json
import time

from sqlmodel import select

from app.models import (
    AppUser,
    Invite,
    InviteStatus,
    Plan,
    Renewal,
    RenewalStatus,
    Role,
    Subscription,
    SubscriptionStatus,
    utcnow,
)
from app.services import settings_store
from app.services import subscriptions as sub_svc

WEBHOOK_SECRET = "whsec_test_1"


def _enable_stripe(session, webhook_secret=WEBHOOK_SECRET):
    settings_store.set_value(session, "stripe_secret_key", "sk_test_1")
    settings_store.set_value(session, "stripe_enabled", "true")
    settings_store.set_value(session, "stripe_webhook_secret", webhook_secret)


def _sig_header(payload: bytes, secret: str = WEBHOOK_SECRET, ts: str | None = None) -> str:
    ts = ts or str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(event_type, obj):
    return json.dumps({"type": event_type, "data": {"object": obj}}).encode()


def _plan(session, slug):
    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def _user(session, name, **kw):
    u = AppUser(role=Role.user, real_name=name, **kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _sub(session, user, plan):
    s = Subscription(
        user_id=user.id, plan_id=plan.id, status=SubscriptionStatus.active,
        expiry_at=utcnow(),
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def test_rejects_missing_signature(client, db_session):
    _enable_stripe(db_session)
    payload = _event("checkout.session.completed", {"metadata": {}})
    resp = client.post("/webhooks/stripe", content=payload)
    assert resp.status_code == 400


def test_rejects_bad_signature(client, db_session):
    _enable_stripe(db_session)
    payload = _event("checkout.session.completed", {"metadata": {}})
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 400


def test_rejects_when_webhook_secret_unset(client, db_session):
    payload = _event("checkout.session.completed", {"metadata": {}})
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 400


def test_renewal_paid_marks_renewal_and_reactivates(client, db_session):
    _enable_stripe(db_session)
    user = _user(db_session, "PayUser", access_suspended=True)
    sub = _sub(db_session, user, _plan(db_session, "bronze"))
    renewal = sub_svc.create_renewal(db_session, sub, actor_id=None, collected_by=None)
    db_session.commit()

    payload = _event(
        "checkout.session.completed",
        {"metadata": {"type": "renewal", "renewal_id": str(renewal.id)}},
    )
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 200
    db_session.commit()
    db_session.refresh(renewal)
    db_session.refresh(user)
    assert renewal.status == RenewalStatus.paid
    assert renewal.causale == "Stripe payment"
    assert user.access_suspended is False


def test_renewal_paid_is_idempotent_on_retry(client, db_session):
    _enable_stripe(db_session)
    user = _user(db_session, "RetryUser")
    sub = _sub(db_session, user, _plan(db_session, "bronze"))
    renewal = sub_svc.create_renewal(db_session, sub, actor_id=None, collected_by=None)
    db_session.commit()

    payload = _event(
        "checkout.session.completed",
        {"metadata": {"type": "renewal", "renewal_id": str(renewal.id)}},
    )
    headers = {"stripe-signature": _sig_header(payload)}
    first = client.post("/webhooks/stripe", content=payload, headers=headers)
    second = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200  # retried delivery is a safe no-op, not an error


def test_renewal_paid_unknown_renewal_id_is_noop(client, db_session):
    _enable_stripe(db_session)
    payload = _event(
        "checkout.session.completed",
        {"metadata": {"type": "renewal", "renewal_id": "999999"}},
    )
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 200


def test_invite_paid_sets_stripe_paid_at(client, db_session):
    _enable_stripe(db_session)
    inv = Invite(
        email="invpay@example.com", real_name="Inv Pay",
        plan_id=_plan(db_session, "bronze").id, token="t-invpay",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    payload = _event(
        "checkout.session.completed",
        {"metadata": {"type": "invite", "invite_id": str(inv.id)}},
    )
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 200
    db_session.commit()
    db_session.refresh(inv)
    assert inv.stripe_paid_at is not None


def test_invite_paid_duplicate_delivery_is_noop(client, db_session):
    _enable_stripe(db_session)
    already = utcnow()
    inv = Invite(
        email="invdup@example.com", real_name="Inv Dup",
        plan_id=_plan(db_session, "bronze").id, token="t-invdup",
        stripe_paid_at=already,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    payload = _event(
        "checkout.session.completed",
        {"metadata": {"type": "invite", "invite_id": str(inv.id)}},
    )
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 200
    db_session.commit()
    db_session.refresh(inv)
    assert inv.stripe_paid_at == already


def test_invite_paid_ignores_already_accepted_invite(client, db_session):
    _enable_stripe(db_session)
    inv = Invite(
        email="invacc@example.com", real_name="Inv Acc",
        plan_id=_plan(db_session, "bronze").id, token="t-invacc",
        status=InviteStatus.accepted,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    payload = _event(
        "checkout.session.completed",
        {"metadata": {"type": "invite", "invite_id": str(inv.id)}},
    )
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 200
    db_session.commit()
    db_session.refresh(inv)
    assert inv.stripe_paid_at is None


def test_unrecognized_metadata_is_noop(client, db_session):
    _enable_stripe(db_session)
    payload = _event("checkout.session.completed", {"metadata": {"type": "mystery"}})
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 200


def test_ignores_other_event_types(client, db_session):
    _enable_stripe(db_session)
    payload = _event("payment_intent.succeeded", {"metadata": {}})
    resp = client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": _sig_header(payload)},
    )
    assert resp.status_code == 200
