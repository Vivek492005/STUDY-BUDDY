"""Anonymous visitor sessions: session_id on traction_event

Revision ID: ef8a36f12059
Revises: 1b9da824d9bb
Create Date: 2026-11-19

- Nullable session_id column (uuid4 hex, 32 chars) on traction_event,
  plus a composite (event_type, session_id, created_at) index for the
  once-per-session-per-day existence check in
  studybuddy/traction.py::ensure_daily_anon_session_event.
- No PII: the id is random, stored only to dedupe daily visitor counts.
  Prod gets the column via _ensure_prod_schema's additive self-heal
  (the index is a perf nicety; the existing (event_type, created_at)
  index keeps the queries fast without it).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "ef8a36f12059"
down_revision = "1b9da824d9bb"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "traction_event",
        sa.Column("session_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_traction_event_anon_session",
        "traction_event",
        ["event_type", "session_id", "created_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_traction_event_anon_session", table_name="traction_event")
    op.drop_column("traction_event", "session_id")
