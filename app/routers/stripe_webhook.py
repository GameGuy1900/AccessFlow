"""Stripe webhook: confirms payment for both renewal and invite payment links.

No admin/session auth here — Stripe is the caller, authenticated instead via
the webhook signing secret (HMAC over the raw body). The CSRF origin middleware
in app.main already lets this through unaffected: Stripe's POST carries no
Origin/Sec-Fetch-Site header, which is the same "non-browser client" case that
already applies to curl and health probes.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app import runtime_config
from app.db import engine
from app.models import AppUser, Invite, InviteStatus, Renewal, utcnow
from app.services import stripe_service
from app.services import subscriptions as sub_svc

router = APIRouter()
log = logging.getLogger("pum.stripe_webhook")


def _handle_renewal_paid(session: Session, renewal_id: str) -> None:
    renewal = session.get(Renewal, int(renewal_id)) if renewal_id.isdigit() else None
    if renewal is None:
        log.warning("stripe webhook: unknown renewal_id=%s", renewal_id)
        return
    try:
        sub_svc.mark_renewal_paid(session, renewal, causale="Stripe payment")
    except ValueError:
        # Already paid (Stripe retried the same event) or no longer pending
        # (cancelled) — both are safe no-ops, not errors.
        return
    from app.models import Subscription
    from app.services import access_service

    sub = session.get(Subscription, renewal.subscription_id)
    user = session.get(AppUser, sub.user_id) if sub else None
    if user is not None:
        access_service.reactivate(session, user)


def _handle_invite_paid(session: Session, invite_id: str) -> None:
    invite = session.get(Invite, int(invite_id)) if invite_id.isdigit() else None
    if invite is None or invite.status != InviteStatus.pending:
        return
    if invite.stripe_paid_at is not None:
        return  # already recorded (duplicate webhook delivery)
    invite.stripe_paid_at = utcnow()
    session.add(invite)
    session.commit()


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    secret = runtime_config.stripe_config()["webhook_secret"]
    if not stripe_service.verify_webhook_signature(payload, sig_header, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    event = await request.json()
    if event.get("type") == "checkout.session.completed":
        obj = event.get("data", {}).get("object", {})
        metadata = obj.get("metadata") or {}
        kind = metadata.get("type")
        with Session(engine) as session:
            if kind == "renewal" and metadata.get("renewal_id"):
                _handle_renewal_paid(session, metadata["renewal_id"])
            elif kind == "invite" and metadata.get("invite_id"):
                _handle_invite_paid(session, metadata["invite_id"])
            else:
                log.warning("stripe webhook: unrecognized metadata %r", metadata)

    return JSONResponse({"received": True})
