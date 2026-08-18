import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.gold.matcher import find_match, haversine_meters, extract_title_signals, aggregate_signals


def test_haversine_zero_distance():
    assert haversine_meters(30.06, 31.25, 30.06, 31.25) == 0


def test_haversine_known_distance_roughly_correct():
    # Cairo to Giza — roughly 15km apart, sanity check the formula
    d = haversine_meters(30.0444, 31.2357, 30.0131, 31.2089)
    assert 3000 < d < 6000  # loose bound, just catching gross errors


def test_matches_same_property_close_coordinates():
    # With the raised 0.85 threshold, a candidate with no price data and
    # ~2% area diff scores ~0.81 — below the bar, so this is now a no_match.
    # For ML training data, this is correct: uncertain matches are rejected.
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 150, "lat": 30.0600, "lng": 31.2500}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 152,
        "lat": 30.0601, "lng": 31.2501,  # ~15m away
        "price_egp": 3000000,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id == 1
    assert result.method == "scored_match"
    assert result.confidence > 0.8


def test_does_not_match_different_rooms():
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 150, "lat": 30.06, "lng": 31.25}
    candidates = [{"id": 1, "property_type": "Apartment", "rooms": 4, "area_m2": 150, "lat": 30.06, "lng": 31.25}]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_does_not_match_far_away_despite_same_attributes():
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 150, "lat": 30.06, "lng": 31.25}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 150,
        "lat": 30.20, "lng": 31.40,  # a different part of the city
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None


def test_does_not_match_area_too_different():
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 100, "lat": 30.06, "lng": 31.25}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 200,  # 100% bigger
        "lat": 30.06, "lng": 31.25,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None


def test_matches_on_attributes_when_no_coordinates():
    """No coords on either side — matching falls back to attributes alone.
    With the raised 0.85 threshold, price data is needed to clear the bar."""
    new_listing = {"property_type": "Villa", "rooms": 5, "area_m2": 300, "lat": None, "lng": None,
                   "price_egp": 10000000}
    candidates = [{"id": 7, "property_type": "Villa", "rooms": 5, "area_m2": 305, "lat": None, "lng": None,
                   "price_egp": 10500000}]
    result = find_match(new_listing, candidates)
    assert result.property_id == 7
    assert result.method == "scored_match"
    assert result.confidence >= 0.80


def test_no_candidates_returns_no_match():
    new_listing = {"property_type": "Apartment", "rooms": 2, "area_m2": 90, "lat": 30.0, "lng": 31.0}
    result = find_match(new_listing, [])
    assert result.property_id is None
    assert result.confidence == 0.0


def test_hard_rejects_building_code_conflict_with_any_listing():
    # A property whose ads mention B6 and B8 must not absorb a B11 ad — the
    # conflict is detected against the AGGREGATED signals (all listings),
    # not just the first listing's title.
    cand_signals = aggregate_signals([
        extract_title_signals("شقة للبيع في قلب مدينتي B6"),
        extract_title_signals("شقة ارضي بجاردن في مدينتي B8"),
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "شقة للبيع في مدينتي B11", "price_egp": 6000000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_accepts_when_new_signal_matches_any_existing_listing():
    # B8 appears in one of the property's existing ads, so it is not a
    # conflict even though the first ad said B6.
    cand_signals = aggregate_signals([
        extract_title_signals("شقة للبيع في قلب مدينتي B6"),
        extract_title_signals("شقة ارضي بجاردن في مدينتي B8"),
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "شقة للبيع استلام فوري في مدينتي B8", "price_egp": 6500000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id == 1
    assert result.method == "scored_match"


def test_hard_rejects_floor_type_conflict_with_any_listing():
    cand_signals = aggregate_signals([
        extract_title_signals("روف للبيع في مدينتي"),
        extract_title_signals("شقة ارضي بجاردن للبيع"),
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "شقة دور متكرر للبيع", "price_egp": 6000000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_hard_rejects_geo_beyond_ceiling_despite_perfect_attributes():
    # ~21km apart with everything else identical — geo carries only 10% of
    # the weight, so without the hard ceiling this would clear the 0.75 bar.
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 150,
                   "lat": 30.06, "lng": 31.25, "price_egp": 3000000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 150,
        "lat": 30.20, "lng": 31.40, "price_egp": 3000000,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


# -----------------------------------------------------------------------
# New hard gates (v5): garden+ground vs upper-floor, view conflict, phase
# -----------------------------------------------------------------------

def test_hard_rejects_garden_ground_vs_roof():
    """A ground-floor garden unit cannot match a roof unit — structurally
    incompatible (Property 11423 in the audit: 'ارضى بحديقة' merged with
    '3/4 تشطيب' upper-floor ads)."""
    cand_signals = aggregate_signals([
        extract_title_signals("روف للبيع في مدينتي B6"),  # roof unit
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "شقة ارضي بجاردن للبيع", "price_egp": 6500000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_hard_rejects_roof_vs_garden_ground():
    """Reverse direction: new ad is roof, candidate has garden+ground."""
    cand_signals = aggregate_signals([
        extract_title_signals("شقة ارضي بجاردن للبيع"),  # ground+garden
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "روف للبيع في مدينتي", "price_egp": 7000000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_garden_ground_matches_when_candidate_has_no_floor_type():
    """When candidate has NO explicit floor type (unknown), garden+ground
    should still match — BUT only if there's title-level evidence (building/phase)
    when coordinates are identical. Without it, the match is rejected to prevent
    over-merging different units in the same compound."""
    cand_signals = aggregate_signals([
        extract_title_signals("شقة للبيع في مدينتي B10"),  # has building signal
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "شقة ارضي بجاردن للبيع B10", "price_egp": 6500000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id == 1
    assert result.method == "scored_match"


def test_no_match_without_title_evidence_same_coords():
    """When coordinates are identical (compound centroid) and neither side has
    building/phase signals, the match is rejected to prevent over-merging."""
    cand_signals = aggregate_signals([
        extract_title_signals("شقة للبيع في مدينتي"),  # no building/phase
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "شقة للبيع في مدينتي", "price_egp": 6500000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_hard_rejects_view_conflict_for_chalet():
    """A sea-view chalet cannot match a lagoon-view chalet — structurally
    different orientations (Property 11038, 11838 in the audit: sea view
    merged with lagoon/golf view chalets)."""
    cand_signals = aggregate_signals([
        extract_title_signals("شاليه للبيع فيو لاجون في سيلفر ساندز"),
    ])
    new_listing = {"property_type": "شاليهات", "rooms": 2, "area_m2": 120,
                   "lat": 31.19, "lng": 27.59,
                   "title": "شاليه للبيع سي فيو في سيلفر ساندز", "price_egp": 12000000}
    candidates = [{
        "id": 1, "property_type": "شاليهات", "rooms": 2, "area_m2": 120,
        "lat": 31.19, "lng": 27.59, "price_egp": 12500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_view_conflict_only_for_chalets():
    """View-type hard conflict only applies to chalets/villas. For apartments
    with identical coords and no building/phase signals, the match is rejected
    to prevent over-merging (same compound centroid = different units)."""
    cand_signals = aggregate_signals([
        extract_title_signals("شقة للبيع فيو لاجون في مدينتي"),
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 133,
                   "lat": 30.09, "lng": 31.64,
                   "title": "شقة للبيع سي فيو في مدينتي", "price_egp": 6500000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 133,
        "lat": 30.09, "lng": 31.64, "price_egp": 6500000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    # With identical coords and no building/phase evidence, match is rejected
    assert result.property_id is None
    assert result.method == "no_match"


def test_hard_rejects_phase_conflict():
    """Different phases = different buildings (Property 12046 in the audit:
    'مرحلة سابعة' merged with 'مرحلة خامسة' in Rehab City)."""
    cand_signals = aggregate_signals([
        extract_title_signals("شقة للبيع في مرحلة 5 في الرحاب"),
    ])
    new_listing = {"property_type": "Apartment", "rooms": 3, "area_m2": 131,
                   "lat": 30.06, "lng": 31.48,
                   "title": "شقة للبيع في مرحلة 7 في الرحاب", "price_egp": 7300000}
    candidates = [{
        "id": 1, "property_type": "Apartment", "rooms": 3, "area_m2": 131,
        "lat": 30.06, "lng": 31.48, "price_egp": 7200000, "signals": cand_signals,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None
    assert result.method == "no_match"


def test_extracts_group_block_cluster():
    """Phase/group/block/cluster/zone identifiers are extracted for
    large-compound disambiguation."""
    signals = extract_title_signals("شقة للبيع في مجموعة 111 في الرحاب")
    assert signals.phase == "111"
    signals = extract_title_signals("شقة في بلوك B بالتجمع")
    assert signals.phase in ("B", "b")  # case may vary
    signals = extract_title_signals("شاليه في cluster 3 بالساحل")
    assert signals.phase == "3"


def test_dynamic_area_tolerance_tighter_with_coords():
    """With coordinates, area tolerance tightens for large units —
    a 206m² duplex vs 190m² apartment (7.8% diff) should be rejected."""
    new_listing = {"property_type": "دوبليكس", "rooms": 4, "area_m2": 206,
                   "lat": 30.09, "lng": 31.70, "title": "دوبلكس للبيع"}
    candidates = [{
        "id": 1, "property_type": "دوبليكس", "rooms": 4, "area_m2": 190,
        "lat": 30.09, "lng": 31.70, "price_egp": 13900000,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id is None  # 7.8% > 2% tolerance for >200m² with coords


def test_dynamic_area_tolerance_lenient_without_coords():
    """Without coordinates, area tolerance stays at 5% to avoid false
    negatives when the matcher has fewer signals. With the raised 0.85
    threshold, price data is needed to clear the bar."""
    new_listing = {"property_type": "Villa", "rooms": 5, "area_m2": 300,
                   "lat": None, "lng": None, "title": "فيلا للبيع",
                   "price_egp": 10000000}
    candidates = [{
        "id": 1, "property_type": "Villa", "rooms": 5, "area_m2": 305,
        "lat": None, "lng": None, "price_egp": 10000000,
    }]
    result = find_match(new_listing, candidates)
    assert result.property_id == 1  # 1.6% < 5% tolerance when no coords + same price
    assert result.method == "scored_match"
