import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.vector.document_builder import build_document


def test_builds_readable_document():
    features = {
        "property_type": "Apartment", "rooms": 3, "baths": 2, "area_m2": 150,
        "city": "Cairo", "neighbourhood": "Maadi",
        "price_egp": 3000000, "price_per_m2": 20000, "is_outlier": False,
    }
    doc = build_document(features)
    assert "Apartment" in doc
    assert "3 rooms" in doc
    assert "Maadi" in doc
    assert "3,000,000 EGP" in doc


def test_handles_missing_fields_without_crashing():
    doc = build_document({})
    assert isinstance(doc, str)
    assert len(doc) > 0


def test_flags_outliers_in_text():
    features = {"property_type": "Villa", "is_outlier": True}
    doc = build_document(features)
    assert "outlier" in doc.lower()


def test_includes_arabic_title_and_signals():
    features = {
        "property_type": "Apartment", "rooms": 3, "baths": 2, "area_m2": 133,
        "city": "Cairo", "neighbourhood": "El Rehab",
        "price_egp": 5000000, "price_per_m2": 37600, "is_outlier": False,
        "representative_title": "شقة ارضي بجاردن في مدينتي بلوك B6",
    }
    doc = build_document(features)
    assert "شقة ارضي بجاردن" in doc          # raw Arabic title kept
    assert "أرضي" in doc and "جاردن" in doc  # signals rendered in Arabic
    assert "B6" in doc                       # building code
    assert "Apartment with 3 rooms" in doc   # English summary still present


def test_ignores_missing_title_gracefully():
    features = {"property_type": "Apartment", "rooms": 2}
    doc = build_document(features)
    assert "Apartment" in doc


def test_includes_all_rich_property_features():
    features = {
        "property_type": "Chalet", "rooms": 2, "baths": 2, "area_m2": 100,
        "city": "Ras El Bar", "neighbourhood": "Golf", "province": "Damietta",
        "price_egp": 2500000, "price_per_m2": 25000, "is_outlier": False,
        "representative_title": "شاليه للبيع في راس البر مفروش",
        "furnishing_status": "furnished",
        "completion_status": "ready",
        "agency_name": "Prime Real Estate",
        "url": "https://www.bayut.eg/property/details-123456.html",
        "days_on_market": 14,
        "neighbourhood_avg_price_per_m2": 20000,
    }
    doc = build_document(features)
    assert "مفروش" in doc
    assert "جاهز للتسليم" in doc
    assert "Damietta" in doc
    assert "Furnished furnishing" in doc
    assert "Ready status" in doc
    assert "Listed by Prime Real Estate" in doc
    assert "25% above neighbourhood average" in doc
    assert "On market for 14 days" in doc
    assert "URL: https://www.bayut.eg/property/details-123456.html" in doc

