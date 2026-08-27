"""stripe payment links (renewal + invite)

Revision ID: c4d5e6f7a8b9
Revises: b7c8d9e0f1a2
Create Date: 2026-08-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('renewal', sa.Column(
        'stripe_payment_link_id', sa.String(), nullable=True))
    op.add_column('renewal', sa.Column(
        'stripe_payment_link_url', sa.String(), nullable=True))
    op.create_index(
        'ix_renewal_stripe_payment_link_id', 'renewal',
        ['stripe_payment_link_id'],
    )

    op.add_column('invite', sa.Column(
        'stripe_payment_link_id', sa.String(), nullable=True))
    op.add_column('invite', sa.Column(
        'stripe_payment_link_url', sa.String(), nullable=True))
    op.add_column('invite', sa.Column(
        'stripe_paid_at', sa.DateTime(), nullable=True))
    op.create_index(
        'ix_invite_stripe_payment_link_id', 'invite',
        ['stripe_payment_link_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_invite_stripe_payment_link_id', table_name='invite')
    op.drop_column('invite', 'stripe_paid_at')
    op.drop_column('invite', 'stripe_payment_link_url')
    op.drop_column('invite', 'stripe_payment_link_id')

    op.drop_index('ix_renewal_stripe_payment_link_id', table_name='renewal')
    op.drop_column('renewal', 'stripe_payment_link_url')
    op.drop_column('renewal', 'stripe_payment_link_id')
