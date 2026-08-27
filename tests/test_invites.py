from sqlmodel import select

import app.services.plex_oauth as po
import app.services.plex_service as plex_service
from app.models import (
    AppUser,
    Invite,
    InviteStatus,
    Renewal,
    RenewalStatus,
    Role,
    utcnow,
)
from app.services import subscriptions as sub_svc


def _mk(session, role, name, manager_id=None):
    u = AppUser(role=role, real_name=name, manager_id=manager_id)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _plan(session, slug):
    from app.models import Plan

    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def test_admin_creates_invite(client, db_session, login_as, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        plex_service,
        "invite_friend",
        lambda email, sections=None: calls.setdefault("email", email),
    )
    admin = _mk(db_session, Role.admin, "InvAdmin")
    login_as(client, admin.id)
    resp = client.post(
        "/invites",
        data={
            "email": "new@example.com",
            "real_name": "New Person",
            "role": "user",
            "manager_id": str(admin.id),
            "plan_slug": "bronze",
            "trial_days": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert calls["email"] == "new@example.com"
    db_session.commit()
    inv = db_session.exec(
        select(Invite).where(Invite.email == "new@example.com")
    ).one()
    assert inv.status == InviteStatus.pending
    assert inv.plex_invite_sent_at is not None
    assert inv.plan_id == _plan(db_session, "bronze").id


def test_admin_withdraws_pending_invite(client, db_session, login_as, monkeypatch):
    cancelled = {}
    monkeypatch.setattr(
        plex_service, "cancel_invite", lambda email: cancelled.setdefault("email", email)
    )
    admin = _mk(db_session, Role.admin, "DelAdmin")
    inv = Invite(email="bye@example.com", real_name="Bye", token="t-bye")
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    login_as(client, admin.id)
    resp = client.post(f"/invites/{inv.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert cancelled["email"] == "bye@example.com"
    inv_id = inv.id
    db_session.expunge_all()  # drop the stale instance the request session deleted
    assert db_session.get(Invite, inv_id) is None


def test_withdraw_ignores_already_accepted_invite(client, db_session, login_as, monkeypatch):
    # An accepted invite must not be deletable (and Plex is never touched).
    monkeypatch.setattr(
        plex_service, "cancel_invite", lambda email: (_ for _ in ()).throw(AssertionError)
    )
    admin = _mk(db_session, Role.admin, "KeepAdmin")
    inv = Invite(
        email="kept@example.com", real_name="Kept", token="t-kept",
        status=InviteStatus.accepted,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    login_as(client, admin.id)
    resp = client.post(f"/invites/{inv.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    db_session.commit()
    assert db_session.get(Invite, inv.id) is not None


def test_moderator_cannot_invite(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "NoInvite")
    login_as(client, mod.id)
    resp = client.post(
        "/invites",
        data={"email": "x@example.com", "real_name": "X"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_invite_not_persisted_when_plex_fails(client, db_session, login_as, monkeypatch):
    def _boom(email):
        raise RuntimeError("plex down")

    monkeypatch.setattr(plex_service, "invite_friend", _boom)
    admin = _mk(db_session, Role.admin, "FailAdmin")
    login_as(client, admin.id)
    resp = client.post(
        "/invites",
        data={"email": "fail@example.com", "real_name": "F", "role": "user"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    db_session.commit()
    assert (
        db_session.exec(
            select(Invite).where(Invite.email == "fail@example.com")
        ).first()
        is None
    )


def _activate_via_plex(client, db_session, monkeypatch, email, acc_id="500"):
    monkeypatch.setattr(po, "create_pin", lambda: {"id": 1, "code": "C"})
    client.get("/login/plex", follow_redirects=False)
    monkeypatch.setattr(po, "poll_pin", lambda pid: "tok")
    monkeypatch.setattr(
        po,
        "fetch_account",
        lambda t: {"id": acc_id, "email": email, "username": "newp"},
    )
    return client.get("/login/plex/callback", follow_redirects=False)


def test_activation_provisions_paid_subscription(client, db_session, monkeypatch):
    mgr = _mk(db_session, Role.admin, "Mgr")
    bronze = _plan(db_session, "bronze")
    db_session.add(
        Invite(
            email="paid@example.com",
            real_name="Paid User",
            intended_role=Role.user,
            manager_id=mgr.id,
            plan_id=bronze.id,
            token="t-paid",
        )
    )
    db_session.commit()

    resp = _activate_via_plex(client, db_session, monkeypatch, "paid@example.com")
    assert resp.status_code == 303
    db_session.commit()

    user = db_session.exec(
        select(AppUser).where(AppUser.plex_email == "paid@example.com")
    ).one()
    sub = sub_svc.get_active_subscription(db_session, user.id)
    assert sub is not None and sub.plan_id == bronze.id
    renewals = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).all()
    assert len(renewals) == 1
    assert renewals[0].status == RenewalStatus.pending
    assert renewals[0].collected_by == mgr.id


def test_activation_ff_no_renewal(client, db_session, monkeypatch):
    ff = _plan(db_session, "family_friends")
    db_session.add(
        Invite(
            email="ff@example.com",
            real_name="FF User",
            intended_role=Role.user,
            plan_id=ff.id,
            token="t-ff",
        )
    )
    db_session.commit()

    _activate_via_plex(client, db_session, monkeypatch, "ff@example.com", acc_id="501")
    db_session.commit()

    user = db_session.exec(
        select(AppUser).where(AppUser.plex_email == "ff@example.com")
    ).one()
    sub = sub_svc.get_active_subscription(db_session, user.id)
    assert sub is not None and sub.expiry_at is None
    assert (
        db_session.exec(
            select(Renewal).where(Renewal.subscription_id == sub.id)
        ).first()
        is None
    )


# ---- Stripe payment link on invites ----

def _enable_stripe(session):
    from app.services import settings_store

    settings_store.set_value(session, "stripe_secret_key", "sk_test_1")
    settings_store.set_value(session, "stripe_enabled", "true")


def test_create_invite_stripe_link(client, db_session, login_as, monkeypatch):
    import app.services.stripe_service as stripe_service

    _enable_stripe(db_session)
    admin = _mk(db_session, Role.admin, "PayAdmin")
    bronze = _plan(db_session, "bronze")
    inv = Invite(
        email="pay@example.com", real_name="Pay Person", plan_id=bronze.id,
        token="t-pay",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    monkeypatch.setattr(
        stripe_service, "create_payment_link",
        lambda **kw: {"id": "plink_1", "url": "https://buy.stripe.com/plink_1"},
    )
    login_as(client, admin.id)
    resp = client.post(f"/invites/{inv.id}/stripe-link", follow_redirects=False)
    assert resp.status_code == 303
    db_session.commit()
    db_session.refresh(inv)
    assert inv.stripe_payment_link_id == "plink_1"
    assert inv.stripe_payment_link_url == "https://buy.stripe.com/plink_1"


def test_create_invite_stripe_link_rejects_unpayable_plan(client, db_session, login_as, monkeypatch):
    import app.services.stripe_service as stripe_service

    _enable_stripe(db_session)
    admin = _mk(db_session, Role.admin, "PayAdmin2")
    trial_plan = _plan(db_session, "trial")
    inv = Invite(
        email="trial@example.com", real_name="Trial Person",
        plan_id=trial_plan.id, token="t-trial",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)

    monkeypatch.setattr(
        stripe_service, "create_payment_link",
        lambda **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    login_as(client, admin.id)
    resp = client.post(f"/invites/{inv.id}/stripe-link", follow_redirects=False)
    assert resp.status_code == 400


def test_create_invite_stripe_link_disabled(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "PayAdmin3")
    bronze = _plan(db_session, "bronze")
    inv = Invite(
        email="pay2@example.com", real_name="Pay Person 2", plan_id=bronze.id,
        token="t-pay2",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    login_as(client, admin.id)
    resp = client.post(f"/invites/{inv.id}/stripe-link", follow_redirects=False)
    assert resp.status_code == 400


def test_activation_settles_prepaid_invite(client, db_session, monkeypatch):
    """An invite paid via its Stripe link before the invitee accepts on Plex ->
    the first renewal is created already paid, not left pending."""
    bronze = _plan(db_session, "bronze")
    inv = Invite(
        email="prepaid@example.com", real_name="Prepaid User",
        intended_role=Role.user, plan_id=bronze.id, token="t-prepaid",
        stripe_paid_at=utcnow(),
    )
    db_session.add(inv)
    db_session.commit()

    resp = _activate_via_plex(client, db_session, monkeypatch, "prepaid@example.com", acc_id="502")
    assert resp.status_code == 303
    db_session.commit()

    user = db_session.exec(
        select(AppUser).where(AppUser.plex_email == "prepaid@example.com")
    ).one()
    sub = sub_svc.get_active_subscription(db_session, user.id)
    renewals = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).all()
    assert len(renewals) == 1
    assert renewals[0].status == RenewalStatus.paid
    assert renewals[0].causale == "Stripe (pre-paid at invite)"
