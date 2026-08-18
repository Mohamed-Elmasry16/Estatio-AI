"""
Database layer.

Bronze/Silver stay as before (raw + per-listing cleaned view).
Gold is now built on a proper canonical model instead of a flat table:

    Location    — normalized city/neighbourhood reference
    Building    — optional, mostly empty for now (the source portal doesn't expose
                  building-level data yet — placeholder for later enrichment)
    Property    — the canonical PHYSICAL unit (what we think is one real apartment)
    Listing     — one AD for a property, from one source, with its own price/status
    PriceHistory— append-only price observations per listing

This is what fixes the "same apartment in 4 ads" problem: many Listings
can point to the same Property.
"""
from sqlalchemy import (
    create_engine, String, Integer, Float, Boolean, DateTime, ForeignKey,
    UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship
from sqlalchemy.pool import NullPool
from datetime import datetime

from src.config import get_settings
from src.logging_config import get_logger

settings = get_settings()
log = get_logger(__name__)


def _connect_args(url: str) -> dict:
    """
    Connection kwargs needed to run cleanly against Supabase's pooled Postgres.

    - sslmode: Supabase requires TLS on every connection.
    - connect_timeout: fail fast instead of hanging if the pooler is unreachable.
    - options: per-session statement_timeout so a stuck query can't hold a
      pooled connection forever and starve other pipeline stages.
    - prepare_threshold=None (psycopg3 only): disables server-side prepared
      statements. Required for the transaction pooler (port 6543) — pgbouncer
      in transaction mode reassigns the underlying server connection between
      statements, so a prepared statement from one "session" can point at the
      wrong backend on the next call. This is the #1 cause of cryptic
      "prepared statement already exists" / "does not exist" errors against
      Supabase poolers. The direct/session connection (port 5432, used for
      migrations) doesn't need this, but it's harmless there too.
    """
    args: dict = {
        "sslmode": settings.db_sslmode,
        "connect_timeout": settings.db_connect_timeout_seconds,
        "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
    }
    if "+psycopg://" in url:  # psycopg3 dialect only
        args["prepare_threshold"] = None
    return args


def _make_engine(url: str, *, pooled: bool):
    """
    pooled=True  -> runtime traffic against the Supabase transaction pooler.
                    SQLAlchemy's own pool is set to NullPool: pgbouncer is
                    already doing connection pooling in front of Postgres, so
                    stacking SQLAlchemy's QueuePool on top just adds a second,
                    redundant pooling layer that tends to hide/confuse pooler
                    errors. Each `with SessionLocal()` opens a short-lived
                    pooler connection and hands it straight back.
    pooled=False -> migrations / DDL against the direct or session connection,
                    where a small persistent pool is fine and faster for a
                    sequence of migration statements.
    """
    if pooled:
        return create_engine(
            url,
            poolclass=NullPool,
            pool_pre_ping=True,
            connect_args=_connect_args(url),
            future=True,
        )
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args=_connect_args(url),
        future=True,
    )


def _migration_url() -> str:
    """
    database_migration_url if explicitly set, otherwise database_url with the
    Supabase pooler port (6543) swapped for the direct/session port (5432).
    Only meaningful for Supabase pooler URLs; for a plain local Postgres URL
    this is a no-op (same host/port either way).
    """
    if settings.database_migration_url:
        return settings.database_migration_url
    return settings.database_url.replace(":6543", ":5432")


engine = _make_engine(settings.database_url, pooled=True)
migration_engine = _make_engine(_migration_url(), pooled=False)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- Bronze ---

class BronzeListing(Base):
    """Raw data, exactly as scraped, append-only.

    The (external_id, scraped_at) unique constraint makes ingestion
    idempotent — re-running the same JSONL file is a no-op instead of
    creating duplicate rows that inflate downstream counts.
    """
    __tablename__ = "bronze_listings"
    __table_args__ = (
        UniqueConstraint("external_id", "scraped_at", name="uq_bronze_external_scraped"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, default="portal_eg")
    external_id: Mapped[str] = mapped_column(String, index=True)
    raw_data: Mapped[dict] = mapped_column(JSONB)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    run_id: Mapped[str] = mapped_column(String, nullable=True, index=True)


# ---------------------------------------------------------------- Silver ---

class SilverListing(Base):
    """Cleaned, validated, deduped by ad (external_id). Still one row per AD, not per property."""
    __tablename__ = "silver_listings"

    external_id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=True)
    price_egp: Mapped[float] = mapped_column(Float, nullable=True)
    down_payment_egp: Mapped[float] = mapped_column(Float, nullable=True)
    rooms: Mapped[float] = mapped_column(Float, nullable=True)
    baths: Mapped[float] = mapped_column(Float, nullable=True)
    area_m2: Mapped[float] = mapped_column(Float, nullable=True)
    plot_area_m2: Mapped[float] = mapped_column(Float, nullable=True)
    property_type: Mapped[str] = mapped_column(String, nullable=True)
    city: Mapped[str] = mapped_column(String, nullable=True)
    neighbourhood: Mapped[str] = mapped_column(String, nullable=True)
    province: Mapped[str] = mapped_column(String, nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=True)
    lng: Mapped[float] = mapped_column(Float, nullable=True)
    furnishing_status: Mapped[str] = mapped_column(String, nullable=True)
    completion_status: Mapped[str] = mapped_column(String, nullable=True)
    agency_name: Mapped[str] = mapped_column(String, nullable=True)
    photo_count: Mapped[int] = mapped_column(Integer, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ------------------------------------------------------------ Gold model ---

class Location(Base):
    """Normalized city/neighbourhood — one row per unique combination we've seen."""
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("city", "neighbourhood", name="uq_location_city_neighbourhood"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String)
    neighbourhood: Mapped[str] = mapped_column(String, nullable=True)
    province: Mapped[str] = mapped_column(String, nullable=True)


class Building(Base):
    """
    Placeholder for building-level grouping (e.g. a specific compound/tower).
    The source's search index doesn't expose reliable building identity today,
    so this stays mostly unused until we have a real source for it —
    it exists now so Property doesn't need a schema migration later.
    """
    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=True)


class Property(Base):
    """
    The canonical PHYSICAL unit — our best guess at "this is one real apartment",
    resolved from one or more Listings. See src/gold/matcher.py for how
    listings get matched (or not) to an existing Property.
    """
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"), nullable=True)
    property_type: Mapped[str] = mapped_column(String, nullable=True)
    rooms: Mapped[float] = mapped_column(Float, nullable=True)
    baths: Mapped[float] = mapped_column(Float, nullable=True)
    area_m2: Mapped[float] = mapped_column(Float, nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=True)
    lng: Mapped[float] = mapped_column(Float, nullable=True)

    # Transparency for the (imperfect) matching heuristic:
    # new | scored_match | split — how the property was created, last
    # matched to an existing ad, or repaired by the split pass.
    match_method: Mapped[str] = mapped_column(String, default="new")
    match_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    # Computed by src/gold/build.py from the property's active listings
    current_price_egp: Mapped[float] = mapped_column(Float, nullable=True)
    price_per_m2: Mapped[float] = mapped_column(Float, nullable=True)
    is_outlier: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    listings: Mapped[list["Listing"]] = relationship(back_populates="property")


class Listing(Base):
    """One AD for a property. Many Listings can point to the same Property."""
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    source: Mapped[str] = mapped_column(String, default="portal_eg")
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))

    url: Mapped[str] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=True)
    furnishing_status: Mapped[str] = mapped_column(String, nullable=True)
    completion_status: Mapped[str] = mapped_column(String, nullable=True)
    agency_name: Mapped[str] = mapped_column(String, nullable=True)
    photo_count: Mapped[int] = mapped_column(Integer, nullable=True)

    current_price_egp: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")  # active | stale (not seen in latest scrape)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    property: Mapped["Property"] = relationship(back_populates="listings")
    price_history: Mapped[list["PriceHistory"]] = relationship(back_populates="listing")


class PriceHistory(Base):
    """Append-only. One row every time we observe a listing's price (first time, or a change)."""
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    price_egp: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    listing: Mapped["Listing"] = relationship(back_populates="price_history")


# ------------------------------------------------------------- Monitoring ---

class PipelineRun(Base):
    """One row per pipeline stage execution — see src/monitoring.py."""
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    stage: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")  # running | success | failed
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(String, nullable=True)


class AnalysisProperty(Base):
    """One grouping key (location, property_type, rooms) for dashboard
    analytics — stable identity across pipeline runs, so dashboards can
    track groups over time without id collisions. Created by
    src/gold/analysis_matcher.py on first sight of a key."""
    __tablename__ = "analysis_properties"
    __table_args__ = (
        UniqueConstraint("location_id", "property_type", "rooms", name="uq_analysis_property_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    property_type: Mapped[str] = mapped_column(String, nullable=True)
    rooms: Mapped[float] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PropertyAnalysisMatch(Base):
    """One row per (run, silver listing): maps a Silver listing to the
    analysis Property it was grouped under (or NULL if it created a new one).

    Unlike the strict gold-layer Property matcher, the analysis matcher
    groups on (location, property_type, rooms) alone — higher recall, some
    over-merging acceptable. Useful for dashboard aggregates where a bit of
    over-merging is fine. (run_id, silver_external_id) is unique so a
    re-run upserts instead of duplicating rows.
    """
    __tablename__ = "property_analysis_matches"
    __table_args__ = (
        UniqueConstraint("run_id", "silver_external_id", name="uq_run_silver_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    silver_external_id: Mapped[str] = mapped_column(String, index=True)
    analysis_property_id: Mapped[int] = mapped_column(ForeignKey("analysis_properties.id"), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String, default="new")  # new | scored_match
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def init_db():
    """
    DEV CONVENIENCE ONLY — creates all tables directly from the current models,
    with no migration history. Fine for a throwaway local Postgres.

    For Supabase / staging / production, use Alembic instead so schema changes
    are versioned and reviewable:

        alembic upgrade head

    Alembic is configured to run against migration_engine (the direct/session
    connection), not the pooled runtime engine — see alembic/env.py.
    """
    Base.metadata.create_all(migration_engine)