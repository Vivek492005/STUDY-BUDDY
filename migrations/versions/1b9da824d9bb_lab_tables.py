"""Ultimate Lab Phase 0: lab_run / lab_save / lab_tool_favorite / lab_path_progress

Revision ID: 1b9da824d9bb
Revises: f6a7b8c9d0e1
Create Date: 2026-11-17

The Lab models (unified_app.py: LabRun, LabSave, LabToolFavorite,
LabPathProgress) shipped in 6887c29 without an Alembic migration —
production got the tables from the boot-time _ensure_prod_schema
create_all repair instead. This migration makes the schema
Alembic-managed (ARC-03) so `flask db upgrade` on a fresh database
produces the same tables create_all does. Columns mirror the models
exactly (Python-side defaults only — no server defaults, matching
create_all DDL); unique constraints keep the model names.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "1b9da824d9bb"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lab_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.String(length=200), nullable=False),
        sa.Column("feature", sa.String(length=120), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("basis", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_lab_run_user_id", "user_id"),
        sa.Index("ix_lab_run_tool_id", "tool_id"),
    )
    op.create_table(
        "lab_save",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["lab_run.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "run_id", name="uq_lab_save_user_run"),
        sa.Index("ix_lab_save_user_id", "user_id"),
        sa.Index("ix_lab_save_run_id", "run_id"),
    )
    op.create_table(
        "lab_tool_favorite",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "tool_id", name="uq_lab_fav_user_tool"),
        sa.Index("ix_lab_tool_favorite_user_id", "user_id"),
        sa.Index("ix_lab_tool_favorite_tool_id", "tool_id"),
    )
    op.create_table(
        "lab_path_progress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=80), nullable=False),
        sa.Column("step", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["lab_run.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "path", "step",
                            name="uq_lab_path_user_step"),
        sa.Index("ix_lab_path_progress_user_id", "user_id"),
        sa.Index("ix_lab_path_progress_path", "path"),
    )


def downgrade():
    op.drop_table("lab_path_progress")
    op.drop_table("lab_tool_favorite")
    op.drop_table("lab_save")
    op.drop_table("lab_run")
