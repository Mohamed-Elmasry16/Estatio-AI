"""
The actual pipeline stage: Feature Store -> Document Builder -> Embedding -> pgvector.

property_vectors is a DEDICATED table — the chatbot will query this,
never gold/property_features directly.

Embeddings are generated in batches to avoid one API request per property.

Re-embedding: by default a property is re-embedded when its features were
recomputed after it was last embedded (computed_at > embedded_at), so
feature changes and matcher upgrades propagate — not just brand-new rows.
"""
from datetime import datetime, timezone

from sqlalchemy import select, String, DateTime, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from src.db import Base, SessionLocal, migration_engine
from src.config import get_settings
from src.feature_store.build import PropertyFeatures
from src.vector.document_builder import build_document
from src.vector.embedder import embed_texts
from src.monitoring import track_stage, new_run_id
from src.logging_config import get_logger
from src.retry import with_db_retry


settings = get_settings()
log = get_logger(__name__)


# Number of properties sent to the embedding API in one request.
BATCH_SIZE = 64


class PropertyVector(Base):
    __tablename__ = "property_vectors"

    property_id: Mapped[int] = mapped_column(primary_key=True)

    document_text: Mapped[str] = mapped_column(String)

    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dim)
    )

    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )

    # Serve-time dedup key: (city, neighbourhood, type, rooms, area rounded to
    # 10m²). Properties sharing a key are near-identical units — a search
    # layer can keep at most one result per key to avoid duplicate-looking
    # answers for the same spec.
    dedup_key: Mapped[str] = mapped_column(String, nullable=True)


def _dedup_key(f: PropertyFeatures) -> str:
    area_rounded = round((f.area_m2 or 0) / 10) * 10
    return "|".join([
        str(f.city or ""),
        str(f.neighbourhood or ""),
        str(f.property_type or ""),
        str(f.rooms or ""),
        str(area_rounded),
    ])


@with_db_retry
def build_vectors(reembed_changed: bool = True) -> dict:
    run_id = new_run_id()

    with track_stage(run_id, "vector_build") as tracked:

        # Extension + DDL need the direct/session connection,
        # not the pooler.
        with migration_engine.connect() as conn:
            conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS vector")
            )
            conn.commit()

        Base.metadata.create_all(
            migration_engine,
            tables=[PropertyVector.__table__],
        )

        with SessionLocal() as session:

            features = session.execute(
                select(PropertyFeatures)
            ).scalars().all()

            embedded_at_by_pid = {
                pid: embedded_at
                for (pid, embedded_at) in session.execute(
                    select(PropertyVector.property_id, PropertyVector.embedded_at)
                )
            }

            if reembed_changed:
                features = [
                    f
                    for f in features
                    if f.property_id not in embedded_at_by_pid
                    or f.computed_at > embedded_at_by_pid[f.property_id]
                ]
            else:
                features = [
                    f
                    for f in features
                    if f.property_id not in embedded_at_by_pid
                ]

            total = len(features)

            log.info(
                f"Embedding {total} properties "
                f"in batches of {BATCH_SIZE}"
            )

            embedded = 0

            for batch_start in range(0, total, BATCH_SIZE):

                batch = features[
                    batch_start:batch_start + BATCH_SIZE
                ]

                documents = []
                for f in batch:
                    doc = build_document({
                        "property_type": f.property_type,
                        "rooms": f.rooms,
                        "baths": f.baths,
                        "area_m2": f.area_m2,
                        "city": f.city,
                        "neighbourhood": f.neighbourhood,
                        "province": f.province,
                        "price_egp": f.price_egp,
                        "price_per_m2": f.price_per_m2,
                        "is_outlier": f.is_outlier,
                        "representative_title": f.representative_title,
                        "furnishing_status": f.furnishing_status,
                        "completion_status": f.completion_status,
                        "url": f.url,
                        "agency_name": f.agency_name,
                        "photo_count": f.photo_count,
                        "days_on_market": f.days_on_market,
                        "neighbourhood_avg_price_per_m2": f.neighbourhood_avg_price_per_m2,
                    })
                    documents.append(doc)

                vectors = embed_texts(
                    documents,
                    input_type="passage",
                )

                if len(vectors) != len(batch):
                    raise RuntimeError(
                        f"Embedding count mismatch: "
                        f"expected {len(batch)}, "
                        f"received {len(vectors)}"
                    )

                now = datetime.now(timezone.utc)

                records = []
                for f, doc, vector in zip(batch, documents, vectors):
                    records.append({
                        "property_id": f.property_id,
                        "document_text": doc,
                        "embedding": vector,
                        "embedded_at": now,
                        "dedup_key": _dedup_key(f),
                    })

                stmt = insert(PropertyVector).values(records)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["property_id"],
                    set_={
                        "document_text": stmt.excluded.document_text,
                        "embedding": stmt.excluded.embedding,
                        "embedded_at": stmt.excluded.embedded_at,
                        "dedup_key": stmt.excluded.dedup_key,
                    },
                )
                session.execute(stmt)

                embedded += len(records)

                # Commit each batch instead of waiting for all rows.
                session.commit()

                log.info(
                    f"Embedded {embedded}/{total} properties "
                    f"({embedded / total * 100:.1f}%)"
                )

            tracked["embedded"] = embedded

            log.info(
                f"Vector build complete — "
                f"{embedded} properties embedded"
            )

            return {
                "embedded": embedded,
                "batch_size": BATCH_SIZE,
            }


if __name__ == "__main__":
    build_vectors()
