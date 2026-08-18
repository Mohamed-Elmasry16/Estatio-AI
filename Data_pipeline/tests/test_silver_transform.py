import sys
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.silver.transform import _flatten, _rejection_reason, build_listing_url


SAMPLE_HIT = {
    "id": 123, "externalID": "abc-123", "title": "Nice apartment",
    "price": 3000000, "rooms": 3, "baths": 2, "area": 150,
    "location": [{"type": "city", "name": "Cairo"}, {"type": "neighbourhood", "name": "Maadi"}],
    "category": [{"name": "Apartments"}],
    "geography": {"lat": 30.06, "lng": 31.25},
    "furnishingStatus": "unfurnished", "completionStatus": "ready",
}


def test_flatten_extracts_expected_fields():
    flat = _flatten(SAMPLE_HIT, datetime.now(timezone.utc))
    assert flat["external_id"] == "abc-123"
    assert flat["price_egp"] == 3000000
    assert flat["city"] == "Cairo"
    assert flat["neighbourhood"] == "Maadi"
    assert flat["property_type"] == "Apartments"
    assert flat["lat"] == 30.06


def test_flatten_handles_missing_location_gracefully():
    hit = {"id": 1, "externalID": "no-loc", "price": 100000, "area": 50}
    flat = _flatten(hit, datetime.now(timezone.utc))
    assert flat["city"] is None
    assert flat["neighbourhood"] is None


def test_valid_row_has_no_rejection_reason():
    flat = _flatten(SAMPLE_HIT, datetime.now(timezone.utc))
    assert _rejection_reason(flat) is None


def test_rejects_missing_price():
    hit = {"id": 1, "externalID": "bad-1", "price": None, "area": 100}
    flat = _flatten(hit, datetime.now(timezone.utc))
    assert _rejection_reason(flat) == "missing_price"


def test_rejects_missing_area():
    hit = {"id": 1, "externalID": "bad-2", "price": 100000, "area": None}
    flat = _flatten(hit, datetime.now(timezone.utc))
    assert _rejection_reason(flat) == "missing_area"


def test_rejects_zero_price():
    hit = {"id": 1, "externalID": "bad-3", "price": 0, "area": 100}
    flat = _flatten(hit, datetime.now(timezone.utc))
    assert _rejection_reason(flat) == "missing_price"


def test_build_listing_url_from_template():
    template = "https://example.com/property/details-{external_id}.html"
    assert build_listing_url("503869179", template) == "https://example.com/property/details-503869179.html"


def test_build_listing_url_none_when_input_missing():
    assert build_listing_url("503869179", "") is None
    assert build_listing_url(None, "https://example.com/{external_id}") is None
    assert build_listing_url("", "https://example.com/{external_id}") is None


def test_flatten_includes_url_key():
    flat = _flatten(SAMPLE_HIT, datetime.now(timezone.utc))
    assert "url" in flat


def test_rejects_suspicious_price_bounds():
    low_price_hit = {**SAMPLE_HIT, "price": 5000}
    high_price_hit = {**SAMPLE_HIT, "price": 600000000}
    assert _rejection_reason(_flatten(low_price_hit, datetime.now(timezone.utc))) == "price_suspiciously_low"
    assert _rejection_reason(_flatten(high_price_hit, datetime.now(timezone.utc))) == "price_suspiciously_high"


def test_rejects_suspicious_area():
    large_area_hit = {**SAMPLE_HIT, "area": 15000}
    assert _rejection_reason(_flatten(large_area_hit, datetime.now(timezone.utc))) == "area_suspiciously_large"


def test_rejects_invalid_room_counts():
    neg_rooms_hit = {**SAMPLE_HIT, "rooms": -1}
    high_rooms_hit = {**SAMPLE_HIT, "rooms": 25}
    assert _rejection_reason(_flatten(neg_rooms_hit, datetime.now(timezone.utc))) == "negative_rooms"
    assert _rejection_reason(_flatten(high_rooms_hit, datetime.now(timezone.utc))) == "rooms_suspiciously_high"


def test_rejects_coordinates_outside_egypt():
    bad_coords_hit = {**SAMPLE_HIT, "geography": {"lat": 51.5074, "lng": -0.1278}}  # London
    assert _rejection_reason(_flatten(bad_coords_hit, datetime.now(timezone.utc))) == "coordinates_outside_egypt"

