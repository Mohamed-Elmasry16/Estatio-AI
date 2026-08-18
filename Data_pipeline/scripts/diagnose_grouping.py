"""
One-off diagnostic for why (location_id, property_type, rooms) grouping
is producing fewer merges than expected.

Usage: python scripts/diagnose_grouping.py
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from src.db import SessionLocal, SilverListing


def run():
    with SessionLocal() as session:
        rows = session.execute(select(SilverListing)).scalars().all()

        print(f"Total Silver rows: {len(rows)}")

        # 1. property_type cardinality — should be a handful of categories.
        #    A long tail here (hundreds/thousands of distinct values) means
        #    property_type isn't normalized and is fragmenting every key.
        type_counts = Counter(r.property_type for r in rows)
        print(f"\nDistinct property_type values: {len(type_counts)}")
        for val, n in type_counts.most_common(15):
            print(f"  {val!r}: {n}")
        if len(type_counts) > 30:
            print("  ... property_type looks UNNORMALIZED (too many distinct values) — likely culprit.")

        # 2. rooms cardinality — should be a small set (0-6ish, maybe with .5s).
        rooms_counts = Counter(r.rooms for r in rows)
        print(f"\nDistinct rooms values: {len(rooms_counts)}")
        for val, n in sorted(rooms_counts.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {val!r}: {n}")
        if len(rooms_counts) > 30:
            print("  ... rooms looks UNNORMALIZED (too many distinct values) — likely culprit.")

        # 3. (city, neighbourhood) cardinality via Silver's own city/neighbourhood
        #    fields (not yet resolved to a Location id) — checks for
        #    inconsistent spelling/whitespace fragmenting locations.
        loc_counts = Counter((r.city, r.neighbourhood) for r in rows)
        print(f"\nDistinct (city, neighbourhood) pairs in Silver: {len(loc_counts)}")
        for val, n in sorted(loc_counts.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {val!r}: {n}")

        # 4. The actual grouping key's distribution — this is the real answer.
        key_counts = Counter((r.city, r.neighbourhood, r.property_type, r.rooms) for r in rows)
        sizes = sorted(key_counts.values(), reverse=True)
        singleton_groups = sum(1 for s in sizes if s == 1)
        print(f"\nDistinct (city, neighbourhood, property_type, rooms) keys: {len(key_counts)}")
        print(f"Groups with exactly 1 listing (no merge possible): {singleton_groups} "
              f"({singleton_groups / len(key_counts):.1%} of all groups)")
        print(f"Largest 10 group sizes: {sizes[:10]}")
        print(f"Average group size: {len(rows) / len(key_counts):.2f}")


if __name__ == "__main__":
    run()
