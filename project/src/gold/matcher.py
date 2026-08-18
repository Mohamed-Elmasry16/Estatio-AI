"""
Entity resolution: matches a Silver listing to an existing canonical
Property, or decides it's a new one.

============================================================================
DESIGN HISTORY (read this before changing thresholds)
============================================================================
v1: exact property_type + rooms, area within 5%, geo within 50m.
    -> The source portal's coordinates for large developer compounds are often the
       compound's marketing pin, not the unit's — so hundreds of genuinely
       different apartments in one compound share near-identical geo,
       and v1 merged all of them into one Property.

v2: added a price-similarity guard (two ads for the same physical unit
    should cost about the same). Reduced but did not fix over-merging —
    developer compounds sell many different real units at similar prices
    too (same floor plan = same list price band), so price alone doesn't
    distinguish "Roof unit in Building B1" from "Garden unit in Building B1".

v3: scores ALL candidates instead of returning the first acceptable one,
    and adds TITLE UNDERSTANDING as a first-class signal. Titles routinely
    contain the one thing specs+coordinates can't capture for a
    compound-level listing — which physical unit this is (roof / garden /
    corner / ground / phase / building code). A "Roof, Building B1" and
    "Garden, Building B1" listing can have identical type/rooms/area/price/
    geo and still be different apartments; the title is often the only
    place that distinguishes them. See extract_title_signals().

v4 (current): fixes two ways v3 still over-merged in production:

    (1) v3 compared titles against ONE representative title per property
        (the first listing's), so a hard conflict was missed whenever that
        title happened not to carry the distinguishing signal — a property
        whose first ad said nothing about a building code then absorbed
        B6, B8, B11 and B12 ads one after another. Candidates now carry
        AGGREGATED title signals — the union of every listing's signals
        (see aggregate_signals()) — and a hard conflict fires if the new
        ad disagrees with ANY existing ad on the property. build.py also
        runs a one-time split pass that repairs properties whose
        aggregated signals already conflict (historical over-merges).

    (2) geo carried only 10% of the weight, so a candidate 20km away with
        matching type/rooms/area/price still cleared the 0.75 confidence
        bar. A hard ceiling (GEO_MAX_METERS) now rejects candidates beyond
        it outright — two ads for the same physical unit cannot be
        kilometres apart, even if every other signal agrees.

Still an heuristic, not a guarantee — there is no unique unit ID in the
source data. The design goal throughout has been to err toward creating a
new Property over wrongly merging two different apartments; a missed
duplicate is cheap (a bit of redundant training data), a false merge
corrupts the property's price history and features permanently.
============================================================================
"""
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# THRESHOLDS — every one is a named constant, documented, no magic numbers.
# ---------------------------------------------------------------------------

# --- Hard gates: fail any of these and the candidate is skipped entirely,
#     never scored. These are "physically impossible to be the same unit"
#     checks, not close calls — so they short-circuit rather than just
#     costing points. This is what actually fixes the over-merging (a small
#     score penalty is easy for other high-scoring features to outweigh; an
#     outright reject on "the title says Roof vs Garden" is not).
AREA_TOLERANCE_PCT = 0.05          # areas more than 5% apart -> not the same unit
PRICE_HARD_REJECT_PCT = 0.50       # prices more than 50% apart -> not the same unit,
                                    # regardless of everything else (sanity backstop)
MAX_LISTINGS_PER_PROPERTY = 8      # enforced in build.py's candidate indexing —
                                    # documented here since it's part of the same
                                    # anti-over-merging design; a real unit realistically
                                    # collects a handful of duplicate ads, not dozens.

# --- Soft, weighted features. Weights sum to 100; each candidate's final
#     confidence is the weighted average of its per-feature scores (each
#     0.0-1.0). property_type and rooms are included in the weighted sum
#     for a transparent, auditable breakdown even though build.py's
#     candidate indexing (see index_key()) already guarantees they're an
#     exact match before a candidate is even considered here — so their
#     score is always 1.0 in practice. If find_match is ever called with
#     unfiltered candidates, they act as an additional hard gate.
WEIGHT_PROPERTY_TYPE = 25
WEIGHT_ROOMS = 15
WEIGHT_AREA = 20
WEIGHT_GEO = 10                    # deliberately the lowest weight: the source portal's coordinates
                                    # for compound listings are often a marketing pin,
                                    # not the unit, so geo must never dominate the score.
WEIGHT_PRICE = 15
WEIGHT_TITLE = 15
_TOTAL_WEIGHT = (WEIGHT_PROPERTY_TYPE + WEIGHT_ROOMS + WEIGHT_AREA
                 + WEIGHT_GEO + WEIGHT_PRICE + WEIGHT_TITLE)  # == 100

PRICE_TOLERANCE_PCT = 0.15         # price score reaches 0 at this %% difference
GEO_TOLERANCE_METERS = 50          # geo score reaches 0 at this distance
GEO_MAX_METERS = 1000              # hard ceiling: same-unit ads can't be further
                                   # apart than this. Without it a far-away candidate
                                   # with matching type/rooms/area/price still clears
                                   # the confidence bar, because geo only carries 10%
                                   # of the weight.
GEO_MISSING_COORDS_SCORE = 0.5     # neutral score when either side lacks lat/lng —
                                    # absence of data isn't evidence of a mismatch

MIN_CONFIDENCE_THRESHOLD = 0.80    # the highest-scoring candidate must clear this
                                     # bar to be accepted as a match; otherwise the
                                     # listing becomes a new Property. Raised from 0.75
                                     # for higher precision (fewer false merges) — better
                                     # to leave duplicates than merge different units.

# View-type token mismatch is treated as suspicious but not as certain as a
# floor-type/phase/building mismatch (agents sometimes mislabel views, but
# essentially never mislabel "Roof" as "Garden"), so it's a heavy penalty on
# the title score rather than a hard reject.
VIEW_MISMATCH_PENALTY = 0.6

# Hard gate: mutually exclusive view types for chalets/villas (agents rarely
# mislabel sea vs lagoon vs golf — these are structural orientation differences).
# New ad's view type conflicts with ANY existing listing's view type on the property.
_VIEW_CONFLICT_GROUPS = [
    {"sea", "sea_front"},      # direct sea vs lagoon/golf
    {"lagoon", "lagoon_view"}, # lagoon vs sea/golf
    {"golf", "golf_view"},     # golf vs sea/lagoon
]

# Garden + ground-floor is a structural unit attribute (ground-floor private garden).
# If the new ad has BOTH garden keywords AND ground-floor keywords, it describes
# a specific unit type that cannot match a property whose listings have NEITHER.
_GROUND_FLOOR_TYPES = {"ground", "garden"}  # floor_type values that imply ground level

# Upper-floor types are structurally incompatible with a ground-floor garden unit.
UPPER_FLOOR_TYPES = {"roof", "penthouse"}

# Property types where view orientation is a structural attribute (chalets,
# villas, duplexes...) — sea vs lagoon vs golf are physical orientations that
# a single unit cannot combine, so a view conflict hard-rejects there.
_CHALET_TYPES = {"شاليهات", "فيلات", "دوبليكس", "تاون هاوس", "توين هاوس"}

# Below this distance (meters) coordinates are effectively the compound
# centroid — hundreds of different units share it, so a match requires
# title-level evidence (building/phase) unless BOTH sides are silent.
TITLE_REQUIRED_THRESHOLD = 5  # meters


# ---------------------------------------------------------------------------
# Title understanding
# ---------------------------------------------------------------------------
# These are heuristic keyword/regex dictionaries built from patterns observed
# in real portal titles (see src/gold/audit_duplicates.py output). This
# is intentionally simple substring/regex matching, not NLP — see "Future
# improvements" in the project notes for embeddings-based alternatives.

_ARABIC_DIACRITICS = re.compile(r'[\u064B-\u065F\u0670\u06D6-\u06ED]')
_TATWEEL = '\u0640'


def normalize_text(text: Optional[str]) -> str:
    """Lowercase, strip Arabic diacritics/tatweel, collapse whitespace, and
    unify a handful of common spelling variants so keyword matching isn't
    defeated by trivial spelling differences (e.g. "رووف" vs "روف").
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace(_TATWEEL, "")
    text = text.lower()
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# Ordered (first match wins) so more specific phrases can be listed before
# generic ones if that's ever needed. Canonical name -> list of substrings
# to look for in the normalized title.
_FLOOR_TYPE_KEYWORDS: dict[str, list[str]] = {
    "penthouse": ["بنتهاوس", "بنت هاوس", "penthouse"],
    "duplex": ["دوبلكس", "دبلكس", "duplex"],
    "roof": ["روف", "رووف", "روووف", "roof"],
    "garden": ["بجاردن", "جاردن", "بحديقة", "حديقة خاصة", "garden"],
    "ground": ["ارضي", "أرضي", "ارضى", "أرضى", "ground floor", " ground "],
    "corner": ["كورنر", "ناصية", "corner"],
    "typical": ["دور متكرر", "دور تكرار", "typical floor"],
}

_VIEW_TYPE_KEYWORDS: dict[str, list[str]] = {
    "lagoon": ["لاجون", "لجون", "lagoon"],
    "sea": ["سي فيو", "بحري صريح", "علي البحر", "sea view", "seaview",
             "بحر", "شايف بحر", "فيو بحر", "على البحر", "بحرى", "ناصية البحر"],
    "golf": ["جولف", "golf"],
    "club": ["كلوب هاوس", "club house", "clubhouse"],
    "landscape": ["لاند سكيب", "landscape", "بارك فيو", "park view"],
}

# "Phase 7", "المرحلة السابعة", "Phase B" — captures the token right after
# the phase keyword. Deliberately loose; false negatives (missing a phase
# mention) just fall back to "unknown", which is neutral, not penalized.
# Also catches group/block/cluster/zone identifiers common in large
# compounds (Rehab, Madinaty, etc.): "مجموعة ١١١", "بلوك ب", "كلاستر ٣".
_PHASE_PATTERN = re.compile(
    r'(?:المرحلة|منطقة|مرحلة|phase|مجموعة|group|بلوك|block|كلاستر|cluster|زون|zone)'
    r'\s*([\w\u0600-\u06FF]+)',
    re.IGNORECASE,
)

# Compound "zone"/"building" codes like B1, B6, C1 as commonly used in
# Egyptian compound listings (e.g. "B1", "B6" in Madinaty/El Rehab titles).
# Requires a word boundary on both sides to avoid matching inside e.g. "B12م".
_BUILDING_PATTERN = re.compile(r'(?<![a-zA-Z0-9])([a-zA-Z]\d{1,3})(?![a-zA-Z0-9])')


@dataclass
class TitleSignals:
    floor_type: Optional[str] = None
    view_type: Optional[str] = None
    phase: Optional[str] = None
    building: Optional[str] = None
    has_garden: bool = False
    is_ground: bool = False


@dataclass
class AggregatedTitleSignals:
    """Union of title signals across a property's LISTINGS, not one ad.

    Hard conflicts are checked against these sets, so a new ad only
    conflicts if it disagrees with *any* existing ad on the same property.
    A single representative title (the pre-v4 behaviour) routinely missed
    the signal that should have rejected a false merge.
    """
    floor_types: set = field(default_factory=set)
    view_types: set = field(default_factory=set)
    phases: set = field(default_factory=set)
    buildings: set = field(default_factory=set)
    has_garden: bool = False
    is_ground: bool = False


def aggregate_signals(signals: list[TitleSignals]) -> AggregatedTitleSignals:
    """Collapse many per-listing TitleSignals into one AggregatedTitleSignals."""
    agg = AggregatedTitleSignals()
    for s in signals:
        if s.floor_type:
            agg.floor_types.add(s.floor_type)
        if s.view_type:
            agg.view_types.add(s.view_type)
        if s.phase:
            agg.phases.add(s.phase)
        if s.building:
            agg.buildings.add(s.building)
        if s.has_garden:
            agg.has_garden = True
        if s.is_ground:
            agg.is_ground = True
    return agg


def extract_title_signals(title: Optional[str]) -> TitleSignals:
    """Pull out the distinguishing-unit signals a title tends to carry that
    specs/geo can't: which floor type, which view, which phase, which
    building/zone code. Each field is None when not detected — absence is
    treated as "unknown", never as a mismatch.
    """
    normalized = normalize_text(title)
    if not normalized:
        return TitleSignals()

    floor_type = next(
        (name for name, keywords in _FLOOR_TYPE_KEYWORDS.items()
         if any(kw in normalized for kw in keywords)),
        None
    )
    view_type = next(
        (name for name, keywords in _VIEW_TYPE_KEYWORDS.items()
         if any(kw in normalized for kw in keywords)),
        None
    )
    phase_match = _PHASE_PATTERN.search(normalized)
    phase = phase_match.group(1) if phase_match else None
    building_match = _BUILDING_PATTERN.search(normalized)
    building = building_match.group(1).upper() if building_match else None

    # Arabic transliterated building codes: "بالبى 12" (B12), "بالبي 10" (B10)
    if building is None:
        arabic_building = re.search(r'بالب[يىى]\s*(\d+)', normalized)
        if arabic_building:
            building = "B" + arabic_building.group(1)

    # Garden + ground-floor detection for structural unit type gate
    has_garden = any(kw in normalized for kw in _FLOOR_TYPE_KEYWORDS["garden"])
    is_ground = any(kw in normalized for kw in _FLOOR_TYPE_KEYWORDS["ground"])

    return TitleSignals(
        floor_type=floor_type, view_type=view_type, phase=phase, building=building,
        has_garden=has_garden, is_ground=is_ground
    )


def _has_hard_title_conflict(new_signals: TitleSignals, candidate: AggregatedTitleSignals) -> Optional[str]:
    """Returns a human-readable reason if the new ad describes a unit that is
    physically incompatible with ANY ad already on the candidate property
    (e.g. the property has "Roof, B6" and "Garden, B8" ads and the new ad
    says "B11"), else None. Only fires when the new side HAS a detected
    value for a signal the candidate also has — one side being silent
    about a signal is not a conflict.
    """
    if new_signals.floor_type and candidate.floor_types and new_signals.floor_type not in candidate.floor_types:
        return f"floor type differs ({new_signals.floor_type} not in {sorted(candidate.floor_types)})"
    if new_signals.phase and candidate.phases and new_signals.phase not in candidate.phases:
        return f"phase differs ({new_signals.phase} not in {sorted(candidate.phases)})"
    if new_signals.building and candidate.buildings and new_signals.building not in candidate.buildings:
        return f"building/zone differs ({new_signals.building} not in {sorted(candidate.buildings)})"

    # NEW: garden + ground-floor vs upper-floor conflict
    # If new ad says garden+ground, reject candidates whose listings have
    # explicitly upper-floor types (roof/penthouse) — these are structurally
    # incompatible with a ground-floor garden unit. Only fires when candidate
    # HAS an explicit floor type; "unknown" (no floor mention) is compatible.
    if new_signals.has_garden and new_signals.is_ground:
        if candidate.floor_types & UPPER_FLOOR_TYPES:
            return "garden+ground unit cannot match roof/penthouse property"
    if candidate.has_garden and candidate.is_ground:
        if new_signals.floor_type in UPPER_FLOOR_TYPES:
            return "roof/penthouse unit cannot match garden+ground property"

    return None


def _area_tolerance(area: float, has_coords: bool) -> float:
    """Dynamic area tolerance: tighter for larger units where a few m² is a
    bigger relative difference. Only tightens when coordinates are available
    on both sides — when coords are missing, the matcher has fewer signals
    to work with, so area must stay lenient to avoid false negatives."""
    if not has_coords:
        return 0.05  # no coords -> fewer signals, keep default tolerance
    if area <= 100:
        return 0.05   # 5% for small apartments/chalets
    if area <= 200:
        return 0.03   # 3% for mid-size units
    return 0.02       # 2% for large villas/duplexes


def _views_conflict(new_view: str, candidate_views: set) -> bool:
    """Check if new_view is in a mutually-exclusive conflict group with any
    of the candidate's views (sea vs lagoon vs golf are structural
    orientations — a unit can't face both sea and lagoon)."""
    for group in _VIEW_CONFLICT_GROUPS:
        if new_view in group:
            for other_group in _VIEW_CONFLICT_GROUPS:
                if other_group is group:
                    continue
                if candidate_views & other_group:
                    return True
    return False


def _title_similarity_score(new_signals: TitleSignals, candidate: AggregatedTitleSignals) -> float:
    """Soft score for everything that isn't a hard conflict. Starts at 1.0
    (nothing to disagree about) and takes a heavy penalty when the new ad's
    view type conflicts with ANY of the candidate's listings' view types —
    agents occasionally mislabel views, so it's suspicious but not
    disqualifying the way a floor-type mismatch is.
    """
    score = 1.0
    if new_signals.view_type and candidate.view_types and new_signals.view_type not in candidate.view_types:
        score -= VIEW_MISMATCH_PENALTY
    return max(0.0, score)


# ---------------------------------------------------------------------------
# Geo
# ---------------------------------------------------------------------------

def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    property_id: Optional[int]
    method: str                          # "scored_match" | "no_match"
    confidence: float                    # 0.0-1.0, weighted overall score
    feature_scores: dict = field(default_factory=dict)   # per-feature 0.0-1.0
    explanation: str = ""                # human-readable summary
    candidates_considered: int = 0        # how many candidates were scored


def _score_candidate(new_listing: dict, new_signals: TitleSignals, c: dict) -> Optional[dict]:
    """Score one candidate against the new listing. Returns None if a hard
    gate fails (candidate is not eligible at all), otherwise a dict with
    the overall confidence, per-feature scores, and an explanation string.
    """
    # Hard gates — property_type/rooms are re-checked defensively even
    # though build.py's indexing already guarantees equality, so this
    # function is safe to call with unfiltered candidates too.
    if c.get("property_type") != new_listing.get("property_type"):
        return None
    if c.get("rooms") != new_listing.get("rooms"):
        return None

    c_area, n_area = c.get("area_m2"), new_listing.get("area_m2")
    if not c_area or not n_area:
        return None
    area_diff_pct = abs(c_area - n_area) / max(c_area, 1)
    # Check coords availability before area tolerance (tighter tolerance
    # only applies when both sides have coordinates — otherwise we have
    # fewer signals and must stay lenient to avoid false negatives)
    c_lat, c_lng = c.get("lat"), c.get("lng")
    n_lat, n_lng = new_listing.get("lat"), new_listing.get("lng")
    has_coords = bool(c_lat and c_lng and n_lat and n_lng)
    area_tol = _area_tolerance(max(c_area, n_area), has_coords)
    if area_diff_pct > area_tol:
        return None

    c_price, n_price = c.get("price_egp"), new_listing.get("price_egp")
    if c_price and n_price:
        price_diff_pct = abs(c_price - n_price) / max(c_price, n_price)
        if price_diff_pct > PRICE_HARD_REJECT_PCT:
            return None
    else:
        price_diff_pct = None

    candidate_signals = c.get("signals")
    if candidate_signals is None:
        # Back-compat with callers that pass plain dicts (unit tests):
        # aggregate the single title they provide.
        candidate_signals = aggregate_signals([extract_title_signals(c.get("title"))])
    conflict_reason = _has_hard_title_conflict(new_signals, candidate_signals)
    if conflict_reason:
        return None

    # View-type hard conflict for chalets/villas (sea vs lagoon vs golf
    # are structural orientations — a chalet can't face both sea and lagoon)
    if (new_signals.view_type and candidate_signals.view_types
            and new_listing.get("property_type") in _CHALET_TYPES
            and _views_conflict(new_signals.view_type, candidate_signals.view_types)):
        return None

    # --- Soft scores, each 0.0-1.0 ---
    property_type_score = 1.0  # guaranteed by the hard gate above
    rooms_score = 1.0          # guaranteed by the hard gate above
    area_score = max(0.0, 1.0 - area_diff_pct / area_tol)

    if price_diff_pct is None:
        price_score = GEO_MISSING_COORDS_SCORE  # no price data on one side -> neutral
    else:
        price_score = max(0.0, 1.0 - price_diff_pct / PRICE_TOLERANCE_PCT)

    if has_coords:
        distance = haversine_meters(c_lat, c_lng, n_lat, n_lng)
        if distance > GEO_MAX_METERS:
            return None
        geo_score = max(0.0, 1.0 - distance / GEO_TOLERANCE_METERS)
    else:
        distance = None
        geo_score = GEO_MISSING_COORDS_SCORE  # missing coords isn't evidence of a mismatch

    title_score = _title_similarity_score(new_signals, candidate_signals)

    # For ML training data: when coordinates are within the same compound
    # (< TITLE_REQUIRED_THRESHOLD meters, often the compound centroid), require
    # title-level evidence (building/phase) for a match. Without it, hundreds
    # of different units in one compound merge into one property.
    if distance is not None and distance < TITLE_REQUIRED_THRESHOLD:
        new_has_title_evidence = bool(new_signals.building or new_signals.phase)
        cand_has_title_evidence = bool(candidate_signals.buildings or candidate_signals.phases)
        if not new_has_title_evidence and not cand_has_title_evidence:
            return None
        # If candidate has building/phase signals but new listing doesn't,
        # reject — the new listing is likely a different unit in the same compound
        if cand_has_title_evidence and not new_has_title_evidence:
            return None

    confidence = (
        WEIGHT_PROPERTY_TYPE * property_type_score
        + WEIGHT_ROOMS * rooms_score
        + WEIGHT_AREA * area_score
        + WEIGHT_GEO * geo_score
        + WEIGHT_PRICE * price_score
        + WEIGHT_TITLE * title_score
    ) / _TOTAL_WEIGHT

    feature_scores = {
        "property_type": round(property_type_score, 3),
        "rooms": round(rooms_score, 3),
        "area": round(area_score, 3),
        "geo": round(geo_score, 3),
        "price": round(price_score, 3),
        "title": round(title_score, 3),
    }
    explanation = (
        f"rooms {rooms_score:.0%}, area {area_score:.0%}, "
        f"price {price_score:.0%}, geo {geo_score:.0%}"
        + (f" ({distance:.0f}m)" if distance is not None else " (no coords)")
        + f", title {title_score:.0%} -> overall confidence {confidence:.2f}"
    )

    return {
        "id": c["id"],
        "confidence": confidence,
        "feature_scores": feature_scores,
        "explanation": explanation,
    }


def find_match(new_listing: dict, candidates: list[dict]) -> MatchResult:
    """
    new_listing: {"property_type", "rooms", "area_m2", "lat", "lng",
                  "price_egp", "title"}
    candidates: existing Property rows narrowed by build.py's indexing
                (see index_key() there) — same keys plus "id", plus
                "signals": an AggregatedTitleSignals built from ALL of the
                property's listings (see aggregate_signals()). Callers
                that only have one title can omit it — the matcher then
                falls back to extracting signals from the "title" key.

    Evaluates every candidate (never returns the first acceptable one),
    scores each with a weighted-average confidence, and returns only the
    highest-scoring candidate — provided it clears MIN_CONFIDENCE_THRESHOLD.
    Below that bar, or with zero eligible candidates, this is a new Property.

    Performance: candidates is expected to already be narrowed to a small
    bucket by build.py's (location_id, property_type, rooms) index, so this
    is O(candidates in bucket), not O(all properties) — see build.py for
    the indexing strategy that keeps this from becoming O(N^2) pipeline-wide.
    """
    new_signals = extract_title_signals(new_listing.get("title"))

    scored = []
    for c in candidates:
        result = _score_candidate(new_listing, new_signals, c)
        if result is not None:
            scored.append(result)

    if not scored:
        return MatchResult(
            property_id=None, method="no_match", confidence=0.0,
            candidates_considered=len(candidates),
            explanation="No eligible candidates (all failed a hard gate, or none provided).",
        )

    best = max(scored, key=lambda r: r["confidence"])

    if best["confidence"] < MIN_CONFIDENCE_THRESHOLD:
        return MatchResult(
            property_id=None, method="no_match", confidence=best["confidence"],
            feature_scores=best["feature_scores"], candidates_considered=len(candidates),
            explanation=(
                f"Best candidate scored {best['confidence']:.2f}, below the "
                f"{MIN_CONFIDENCE_THRESHOLD} threshold ({best['explanation']})."
            ),
        )

    return MatchResult(
        property_id=best["id"], method="scored_match", confidence=best["confidence"],
        feature_scores=best["feature_scores"], candidates_considered=len(candidates),
        explanation=f"Matched because: {best['explanation']}",
    )