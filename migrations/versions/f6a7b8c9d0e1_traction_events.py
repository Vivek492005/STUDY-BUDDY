"""Live Traction Tracker: traction_event table + time indexes

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-05

- New append-only traction_event table (event type + timestamp + user id
  only — no IP, no user agent, no PII). Powers "active users" and
  "sessions today" in studybuddy/traction.py.
- Time indexes on the content tables the tracker COUNTs over, so the
  /api/traction endpoint stays under 200ms as data grows:
  quiz_attempt.created_at, interview_session.created_at,
  resume_analysis.created_at, dsa_progress.created_at/updated_at.
  (Model columns carry index=True too, so dev/test create_all matches.)
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "traction_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_traction_event_event_type", "event_type"),
        sa.Index("ix_traction_event_user_id", "user_id"),
        sa.Index("ix_traction_event_created_at", "created_at"),
        sa.Index("ix_traction_event_type_created", "event_type", "created_at"),
        sa.Index("ix_traction_event_user_created", "user_id", "created_at"),
    )
    op.create_index("ix_quiz_attempt_created_at", "quiz_attempt", ["created_at"])
    op.create_index("ix_interview_session_created_at", "interview_session", ["created_at"])
    op.create_index("ix_resume_analysis_created_at", "resume_analysis", ["created_at"])
    op.create_index("ix_dsa_progress_created_at", "dsa_progress", ["created_at"])
    op.create_index("ix_dsa_progress_updated_at", "dsa_progress", ["updated_at"])


def downgrade():
    op.drop_index("ix_dsa_progress_updated_at", table_name="dsa_progress")
    op.drop_index("ix_dsa_progress_created_at", table_name="dsa_progress")
    op.drop_index("ix_resume_analysis_created_at", table_name="resume_analysis")
    op.drop_index("ix_interview_session_created_at", table_name="interview_session")
    op.drop_index("ix_quiz_attempt_created_at", table_name="quiz_attempt")
    op.drop_table("traction_event")
