"""
Turns one property_features row into a natural-language document for
embedding. Bilingual by design:

  - An ARABIC section from the representative title plus the unit-level
    signals extracted from it (floor type / view / building / phase). The
    source listings are Arabic, so user queries in Arabic match much better
    when the document carries the original Arabic wording.
  - An ENGLISH structured summary of the numeric features.

Pure function — no DB, no API calls — so it's trivial to unit test and
to tweak the wording without touching the embedding pipeline.
"""

from src.gold.matcher import extract_title_signals

# Signal -> Arabic rendering, for the Arabic description segment.
_FLOOR_AR = {
    "penthouse": "بنتهاوس",
    "duplex": "دوبلكس",
    "roof": "روف",
    "garden": "جاردن",
    "ground": "أرضي",
    "corner": "كورنر",
    "typical": "دور متكرر",
}
_VIEW_AR = {
    "sea": "فيو بحر",
    "lagoon": "فيو لاجون",
    "golf": "فيو جولف",
    "club": "كلوب هاوس",
    "landscape": "لاند سكيب",
}


_FURNISHING_AR = {
    "furnished": "مفروش",
    "unfurnished": "غير مفروش",
    "partly furnished": "مفروش جزئيا",
    "semi-furnished": "نصف مفروش",
}

_COMPLETION_AR = {
    "ready": "جاهز للتسليم",
    "completed": "مكتمل البناء",
    "off-plan": "تحت الإنشاء / على الخريطة",
    "under construction": "قيد الإنشاء",
}


def _arabic_segment(title: str, furnishing: str | None = None, completion: str | None = None) -> str:
    """Arabic description: the raw title plus unit-level signals, e.g.
    "شقة للبيع في مدينتي ارضي بجاردن" + "وحدة: أرضي بجاردن، بلوك B6، فيو بحر، مفروش، جاهز للتسليم"."""
    parts = [title]
    signals = extract_title_signals(title)
    details = []
    if signals.has_garden and signals.is_ground:
        details.append("أرضي بجاردن")
    elif signals.floor_type:
        details.append(_FLOOR_AR.get(signals.floor_type, signals.floor_type))
    if signals.view_type:
        details.append(_VIEW_AR.get(signals.view_type, signals.view_type))
    if signals.building:
        details.append(f"بلوك {signals.building}")
    if signals.phase:
        details.append(f"مرحلة {signals.phase}")
    if furnishing:
        f_norm = furnishing.lower().strip()
        details.append(_FURNISHING_AR.get(f_norm, furnishing))
    if completion:
        c_norm = completion.lower().strip()
        details.append(_COMPLETION_AR.get(c_norm, completion))
    if details:
        parts.append("وحدة: " + "، ".join(details))
    return " — ".join(parts)


def build_document(features: dict) -> str:
    """
    features: a dict shaped like a PropertyFeatures row with all property attributes.
    """
    parts = []

    title = features.get("representative_title")
    furnishing = features.get("furnishing_status")
    completion = features.get("completion_status")

    if title:
        parts.append(_arabic_segment(title, furnishing=furnishing, completion=completion))

    prop_type = features.get("property_type") or "Property"
    rooms = features.get("rooms")
    baths = features.get("baths")
    area = features.get("area_m2")
    neighbourhood = features.get("neighbourhood")
    city = features.get("city")
    province = features.get("province")
    price = features.get("price_egp")
    price_per_m2 = features.get("price_per_m2")
    agency = features.get("agency_name")
    url = features.get("url")
    days_on_market = features.get("days_on_market")
    avg_m2 = features.get("neighbourhood_avg_price_per_m2")

    header = f"{prop_type}"
    if rooms:
        header += f" with {int(rooms)} rooms"
    if baths:
        header += f" and {int(baths)} bathrooms"
    if area:
        header += f", {area:.0f} square meters"
    parts.append(header + ".")

    # Location summary
    loc_parts = [p for p in [neighbourhood, city, province] if p]
    if loc_parts:
        parts.append(f"Located in {', '.join(loc_parts)}.")

    # Status details
    status_details = []
    if furnishing:
        status_details.append(f"{furnishing.title()} furnishing")
    if completion:
        status_details.append(f"{completion.title()} status")
    if status_details:
        parts.append("Status: " + ", ".join(status_details) + ".")

    if agency:
        parts.append(f"Listed by {agency}.")

    if price:
        parts.append(f"Listed at {price:,.0f} EGP.")
    if price_per_m2:
        m2_str = f"That's about {price_per_m2:,.0f} EGP per square meter."
        if avg_m2 and avg_m2 > 0:
            diff_pct = ((price_per_m2 - avg_m2) / avg_m2) * 100
            if abs(diff_pct) >= 5:
                direction = "above" if diff_pct > 0 else "below"
                m2_str += f" ({abs(diff_pct):.0f}% {direction} neighbourhood average of {avg_m2:,.0f} EGP/m²)."
        parts.append(m2_str)

    if days_on_market is not None:
        parts.append(f"On market for {days_on_market} days.")

    if features.get("is_outlier"):
        parts.append("Note: this price is a statistical outlier compared to similar properties.")

    if url:
        parts.append(f"URL: {url}")

    return " ".join(parts)
