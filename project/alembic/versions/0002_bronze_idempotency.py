"""add bronze idempotency constraint and run_id column

Revision ID: 0002
Revises: 0001_initial_schema
Create Date: 2026-08-12

Adds:
- UniqueConstraint on (external_id, scraped_at) to bronze_listings,
  making re-ingestion of the same JSONL file idempotent.
- run_id column (nullable, indexed) for batch lineage tracking.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the run_id column for batch lineage tracking
    op.add_column('bronze_listings', sa.Column('run_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_bronze_listings_run_id'), 'bronze_listings', ['run_id'], unique=False)

    # Add the unique constraint that makes re-ingestion idempotent.
    # If the table already has duplicate (external_id, scraped_at) rows from
    # prior runs, this will fail — clean them up first:
    #   DELETE FROM bronze_listings a USING bronze_listings b
    #   WHERE a.id < b.id AND a.external_id = b.external_id AND a.scraped_at = b.scraped_at;
    op.create_unique_constraint('uq_bronze_external_scraped', 'bronze_listings', ['external_id', 'scraped_at'])


def downgrade() -> None:
    op.drop_constraint('uq_bronze_external_scraped', 'bronze_listings', type_='unique')
    op.drop_index(op.f('ix_bronze_listings_run_id'), table_name='bronze_listings')
    op.drop_column('bronze_listings', 'run_id')
