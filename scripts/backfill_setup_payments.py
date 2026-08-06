"""One-shot: backfill the up-front payment for paid subscriptions that never got
one logged — either created before record_setup_payment existed, or upgraded
from a free/trial plan (change_plan) before that path also started logging one.

Creates a *paid* renewal (paid_at = sub.start_at) for every ACTIVE paid sub that
has NO setup-marked renewal — i.e. whose up-front first payment was never logged
and is therefore missing from the reports page. A sub that was renewed later
still needs this (the renewal is the extension, not the initial payment); only a
renewal whose causale starts with "Initial setup" counts as the setup, so subs
created/upgraded after the fix are skipped and we never double count.

Amount used is the plan's *current* price_cents — if a plan's price changed
since the historical upgrade, the backfilled amount won't match what was
actually collected at the time. Review the dry-run output before --apply.

Run inside the container:
    python scripts/backfill_setup_payments.py          # dry-run, lists what it would do
    python scripts/backfill_setup_payments.py --apply  # actually write
"""
import sys

from sqlmodel import Session, select

from app.db import engine
from app.models import Plan, Renewal, Subscription, SubscriptionStatus
from app.services import subscriptions as sub_svc


def main(apply: bool) -> None:
    with Session(engine) as session:
        subs = session.exec(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.active
            )
        ).all()
        done = 0
        for sub in subs:
            plan = session.get(Plan, sub.plan_id)
            if plan is None or not plan.is_paid or plan.is_trial or plan.is_unlimited:
                continue
            renewals = session.exec(
                select(Renewal).where(Renewal.subscription_id == sub.id)
            ).all()
            has_setup = any(
                (r.causale or "").startswith("Initial setup") for r in renewals
            )
            if has_setup:
                continue
            print(
                f"  sub {sub.id} user {sub.user_id} plan {plan.slug} "
                f"{plan.price_cents}c paid_at={sub.start_at}"
            )
            if apply:
                sub_svc.record_setup_payment(
                    session, sub, plan, actor_id=None, collected_by=None,
                    causale="Initial setup (backfill)",
                )
            done += 1
        print(f"\n{'Backfilled' if apply else 'Would backfill'} {done} subscription(s).")
        if not apply and done:
            print("Re-run with --apply to write.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
