import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.vector.store import _dedup_key
from src.feature_store.build import PropertyFeatures


def _f(**kwargs) -> PropertyFeatures:
    defaults = dict(
        property_id=1, property_type="Apartment", rooms=3, baths=2,
        area_m2=133, city="Cairo", neighbourhood="El Rehab",
    )
    defaults.update(kwargs)
    return PropertyFeatures(**defaults)


def test_dedup_key_identical_for_same_spec():
    a = _f(area_m2=133)
    b = _f(area_m2=131)
    assert _dedup_key(a) == _dedup_key(b)


def test_dedup_key_differs_across_neighbourhoods():
    a = _f(neighbourhood="El Rehab")
    b = _f(neighbourhood="Maadi")
    assert _dedup_key(a) != _dedup_key(b)


def test_dedup_key_ignores_small_area_differences():
    a = _f(area_m2=131)
    b = _f(area_m2=134)
    assert _dedup_key(a) == _dedup_key(b)
