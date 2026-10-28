"""Audit fix (2026-10-28): add session_version to user table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-28

Session revocation: User.session_version is bumped on every password
change/reset, and a before_request hook in unified_app.py logs out any
session whose stored version no longer matches. Without this column a
stolen session survived a victim's password reset until the 12-hour
session lifetime expired.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_version', sa.Integer(),
                                      nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('session_version')
