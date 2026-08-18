"""initial schema (squashed)

Full schema in one migration: bronze/silver layers, the canonical gold model,
feature store, analysis-property tables for the dashboard matcher, and the
pgvector table with an HNSW index.

Revision ID: 0001
Revises:
Create Date: 2026-08-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 2048  # matches Settings.embedding_dim default — see src/config.py


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- Bronze -------------------------------------------------------
    op.create_table(
        "bronze_listings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String, server_default="portal_eg", nullable=False),
        sa.Column("external_id", sa.String, nullable=False),
        sa.Column("raw_data", postgresql.JSONB, nullable=False),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("processed", sa.Boolean, server_default=sa.false(), nullable=False),
    )
    op.create_index("ix_bronze_listings_external_id", "bronze_listings", ["external_id"])
    op.create_index("ix_bronze_listings_processed", "bronze_listings", ["processed"])

    # --- Silver ---------------------------------------------------------
    op.create_table(
        "silver_listings",
        sa.Column("external_id", sa.String, primary_key=True),
        sa.Column("url", sa.String, nullable=True),
        sa.Column("title", sa.String, nullable=True),
        sa.Column("purpose", sa.String, nullable=True),
        sa.Column("price_egp", sa.Float, nullable=True),
        sa.Column("down_payment_egp", sa.Float, nullable=True),
        sa.Column("rooms", sa.Float, nullable=True),
        sa.Column("baths", sa.Float, nullable=True),
        sa.Column("area_m2", sa.Float, nullable=True),
        sa.Column("plot_area_m2", sa.Float, nullable=True),
        sa.Column("property_type", sa.String, nullable=True),
        sa.Column("city", sa.String, nullable=True),
        sa.Column("neighbourhood", sa.String, nullable=True),
        sa.Column("province", sa.String, nullable=True),
        sa.Column("lat", sa.Float, nullable=True),
        sa.Column("lng", sa.Float, nullable=True),
        sa.Column("furnishing_status", sa.String, nullable=True),
        sa.Column("completion_status", sa.String, nullable=True),
        sa.Column("agency_name", sa.String, nullable=True),
        sa.Column("photo_count", sa.Integer, nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- Gold: canonical model -------------------------------------------
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("city", sa.String, nullable=False),
        sa.Column("neighbourhood", sa.String, nullable=True),
        sa.Column("province", sa.String, nullable=True),
        sa.UniqueConstraint("city", "neighbourhood", name="uq_location_city_neighbourhood"),
    )

    op.create_table(
        "buildings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("location_id", sa.Integer, sa.ForeignKey("locations.id"), nullable=True),
        sa.Column("name", sa.String, nullable=True),
    )

    op.create_table(
        "properties",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("location_id", sa.Integer, sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("building_id", sa.Integer, sa.ForeignKey("buildings.id"), nullable=True),
        sa.Column("property_type", sa.String, nullable=True),
        sa.Column("rooms", sa.Float, nullable=True),
        sa.Column("baths", sa.Float, nullable=True),
        sa.Column("area_m2", sa.Float, nullable=True),
        sa.Column("lat", sa.Float, nullable=True),
        sa.Column("lng", sa.Float, nullable=True),
        sa.Column("match_method", sa.String, server_default="new", nullable=False),
        sa.Column("match_confidence", sa.Float, server_default="1.0", nullable=False),
        sa.Column("current_price_egp", sa.Float, nullable=True),
        sa.Column("price_per_m2", sa.Float, nullable=True),
        sa.Column("is_outlier", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_properties_location_id", "properties", ["location_id"])

    op.create_table(
        "listings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("external_id", sa.String, nullable=False, unique=True),
        sa.Column("source", sa.String, server_default="portal_eg", nullable=False),
        sa.Column("property_id", sa.Integer, sa.ForeignKey("properties.id"), nullable=False),
        sa.Column("url", sa.String, nullable=True),
        sa.Column("title", sa.String, nullable=True),
        sa.Column("furnishing_status", sa.String, nullable=True),
        sa.Column("completion_status", sa.String, nullable=True),
        sa.Column("agency_name", sa.String, nullable=True),
        sa.Column("photo_count", sa.Integer, nullable=True),
        sa.Column("current_price_egp", sa.Float, nullable=True),
        sa.Column("status", sa.String, server_default="active", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_listings_external_id", "listings", ["external_id"], unique=True)
    op.create_index("ix_listings_property_id", "listings", ["property_id"])

    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("listing_id", sa.Integer, sa.ForeignKey("listings.id"), nullable=False),
        sa.Column("price_egp", sa.Float, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_price_history_listing_id", "price_history", ["listing_id"])

    # --- Monitoring -------------------------------------------------------
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("stage", sa.String, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String, server_default="running", nullable=False),
        sa.Column("stats", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("error", sa.String, nullable=True),
    )
    op.create_index("ix_pipeline_runs_run_id", "pipeline_runs", ["run_id"])

    # --- Analysis matcher (dashboard groupings) ----------------------------
    op.create_table(
        "analysis_properties",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("location_id", sa.Integer, sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("property_type", sa.String, nullable=True),
        sa.Column("rooms", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("location_id", "property_type", "rooms", name="uq_analysis_property_key"),
    )

    op.create_table(
        "property_analysis_matches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("silver_external_id", sa.String, nullable=False),
        sa.Column("analysis_property_id", sa.Integer, sa.ForeignKey("analysis_properties.id"), nullable=True),
        sa.Column("confidence", sa.Float, server_default="0", nullable=False),
        sa.Column("method", sa.String, server_default="new", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "silver_external_id", name="uq_run_silver_external"),
    )
    op.create_index("ix_property_analysis_matches_run_id", "property_analysis_matches", ["run_id"])
    op.create_index("ix_property_analysis_matches_silver_external_id", "property_analysis_matches", ["silver_external_id"])

    # --- Feature store ------------------------------------------------
    op.create_table(
        "property_features",
        sa.Column("property_id", sa.Integer, primary_key=True),
        sa.Column("property_type", sa.String, nullable=True),
        sa.Column("rooms", sa.Float, nullable=True),
        sa.Column("baths", sa.Float, nullable=True),
        sa.Column("area_m2", sa.Float, nullable=True),
        sa.Column("city", sa.String, nullable=True),
        sa.Column("neighbourhood", sa.String, nullable=True),
        sa.Column("price_egp", sa.Float, nullable=True),
        sa.Column("price_per_m2", sa.Float, nullable=True),
        sa.Column("is_outlier", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("representative_title", sa.String, nullable=True),
        sa.Column("active_listing_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("neighbourhood_avg_price_per_m2", sa.Float, nullable=True),
        sa.Column("days_on_market", sa.Integer, nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- Vectors (pgvector) -----------------------------------------------
    op.create_table(
        "property_vectors",
        sa.Column("property_id", sa.Integer, primary_key=True),
        sa.Column("document_text", sa.String, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedup_key", sa.String, nullable=True),
    )
    # NOTE: deliberately NO index. pgvector indexes (HNSW/IVFFlat) are capped
    # at 2000 dimensions, and our embeddings are 2048 — so an exact cosine
    # scan is used instead. For ~16-50k rows an exact scan is both fast and
    # 100% accurate. If the data grows past ~100k rows, switch the embedding
    # model to one with <=1024 dims and add an HNSW index then.


def downgrade() -> None:
    op.drop_table("property_vectors")
    op.drop_table("property_features")
    op.drop_table("property_analysis_matches")
    op.drop_table("analysis_properties")
    op.drop_table("pipeline_runs")
    op.drop_table("price_history")
    op.drop_table("listings")
    op.drop_table("properties")
    op.drop_table("buildings")
    op.drop_table("locations")
    op.drop_table("silver_listings")
    op.drop_table("bronze_listings")
