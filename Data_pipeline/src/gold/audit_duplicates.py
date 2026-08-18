"""
Audit tool for the duplicate-ad matcher (src/gold/matcher.py).

For each Property that has more than one Listing pointing at it, prints the
listings side by side (title, price, rooms, area, lat/lng, agency) so you can
eyeball whether the merge actually makes sense, or whether the matcher is
being too aggressive.

Also writes a CSV with every merged group for closer review in Excel/Sheets.

Usage:
    python -m src.gold.audit_duplicates                 # top 30 most-merged properties
    python -m src.gold.audit_duplicates --top 50         # top 50
    python -m src.gold.audit_duplicates --min-listings 3 # only show groups with 3+ ads
"""
import argparse
import csv

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.db import SessionLocal, Property, Location
from src.retry import with_db_retry


@with_db_retry
def run(top: int, min_listings: int, csv_path: str):
    with SessionLocal() as session:
        properties = session.execute(
            select(Property).options(selectinload(Property.listings))
        ).scalars().all()
        locations_by_id = {loc.id: loc for loc in session.execute(select(Location)).scalars()}

        counts = [len(p.listings) for p in properties]
        n_total = len(properties)
        n_single = sum(1 for c in counts if c == 1)
        n_merged = sum(1 for c in counts if c > 1)
        total_listings = sum(counts)

        print("=== Overview ===")
        print(f"Total properties: {n_total}")
        print(f"  - matched to exactly 1 listing: {n_single} ({n_single / n_total:.1%})")
        print(f"  - matched to 2+ listings (merged): {n_merged} ({n_merged / n_total:.1%})")
        print(f"Total listings across all properties: {total_listings}")
        print(f"Average listings per property: {total_listings / n_total:.2f}")
        print(f"Max listings on a single property: {max(counts)}")
        print()

        merged = sorted((p for p in properties if len(p.listings) >= min_listings),
                         key=lambda p: len(p.listings), reverse=True)

        print(f"=== Top {min(top, len(merged))} most-merged properties (min {min_listings} listings) ===\n")
        rows_for_csv = []
        for p in merged[:top]:
            loc = locations_by_id.get(p.location_id)
            print(f"Property #{p.id} — {p.property_type}, {p.rooms} rooms, {p.area_m2} m2, "
                  f"lat/lng ({p.lat}, {p.lng}), {loc.neighbourhood if loc else '?'}, {loc.city if loc else '?'}"
                  f" — {len(p.listings)} listings:")
            for l in p.listings:
                print(f"    - [{l.external_id}] \"{l.title}\" — {l.current_price_egp} EGP, "
                      f"agency: {l.agency_name}, first_seen: {l.first_seen_at}")
                rows_for_csv.append({
                    "property_id": p.id, "property_type": p.property_type, "rooms": p.rooms,
                    "area_m2": p.area_m2, "lat": p.lat, "lng": p.lng,
                    "neighbourhood": loc.neighbourhood if loc else None,
                    "city": loc.city if loc else None,
                    "listing_external_id": l.external_id, "listing_title": l.title,
                    "listing_price_egp": l.current_price_egp, "listing_agency": l.agency_name,
                    "listing_first_seen_at": l.first_seen_at,
                })
            print()

        if rows_for_csv:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows_for_csv[0].keys()))
                writer.writeheader()
                writer.writerows(rows_for_csv)
            print(f"Full merged groups written to {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=30, help="How many merged properties to print (default 30)")
    parser.add_argument("--min-listings", type=int, default=2, help="Only show groups with at least this many listings (default 2)")
    parser.add_argument("--csv", type=str, default="duplicate_audit.csv", help="Where to write the full CSV")
    args = parser.parse_args()
    run(top=args.top, min_listings=args.min_listings, csv_path=args.csv)