from datetime import UTC, datetime, timedelta

import pytest

from poe_craft_mcp.engine.pricing import percentile, summarize
from poe_craft_mcp.engine.types import Listing

RATES = {"chaos": 1.0, "divine": 200.0}
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def chaos(*amounts: float) -> list[Listing]:
    return [Listing(a, "chaos", None) for a in amounts]


def test_percentile_linear_interpolation_matches_numpy_default() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 0.10) == pytest.approx(1.3)
    assert percentile(values, 0.25) == pytest.approx(1.75)
    assert percentile(values, 0.50) == pytest.approx(2.5)
    assert percentile(values, 1.0) == 4.0
    assert percentile([7.0], 0.25) == 7.0


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_empty_input_gives_empty_summary() -> None:
    s = summarize([], RATES, NOW)
    assert (s.p10, s.p25, s.median) == (None, None, None)
    assert s.listing_count == 0 and s.raw_count == 0
    assert s.cheapest_listing_age_seconds is None


def test_outlier_cleanup_drops_price_fixing_listings() -> None:
    listings = [
        *chaos(1, 1, 1, 21, 22, 23, 24, 25, 26, 30, 40),
        Listing(0.1, "divine", None),
    ]
    s = summarize(listings, RATES, NOW)
    # cheapest 10 (after conversion): 1,1,1,20,21,22,23,24,25,26 -> median 23.5 -> floor 11.75
    assert s.raw_count == 12
    assert s.dropped_outliers == 3
    assert s.listing_count == 9
    assert s.p10 == pytest.approx(20.8)
    assert s.p25 == pytest.approx(22.0)
    assert s.median == pytest.approx(24.0)
    assert s.currency_mix == {"chaos": 8, "divine": 1}


def test_unknown_currency_is_dropped_and_counted() -> None:
    listings = [
        *chaos(10, 12, 14),
        Listing(1, "mirror", None),
        Listing(2, "mirror", None),
        Listing(5, "alch", None),
        Listing(5, "bad-rate", None),
    ]
    s = summarize(listings, {**RATES, "bad-rate": 0.0}, NOW)
    assert s.dropped_unknown_currency == {"mirror": 2, "alch": 1, "bad-rate": 1}
    assert s.raw_count == 7
    assert s.listing_count == 3
    assert s.median == 12.0


def test_all_unknown_currency_returns_none_prices() -> None:
    s = summarize([Listing(1, "mirror", None)], RATES, NOW)
    assert s.median is None and s.listing_count == 0
    assert s.dropped_unknown_currency == {"mirror": 1}


def test_zero_priced_listings_are_outliers() -> None:
    s = summarize(chaos(0, 10, 10, 10), RATES, NOW)
    assert s.dropped_outliers == 1 and s.listing_count == 3


def test_fewer_than_ten_listings_use_all_for_median() -> None:
    s = summarize(chaos(2, 100, 100), RATES, NOW)
    assert s.dropped_outliers == 1  # floor is 50% of median(2, 100, 100) = 50
    assert s.listing_count == 2


def test_cheapest_listing_age() -> None:
    listings = [
        Listing(10, "chaos", NOW - timedelta(seconds=90)),
        Listing(20, "chaos", NOW - timedelta(seconds=5)),
    ]
    assert summarize(listings, RATES, NOW).cheapest_listing_age_seconds == 90
    assert summarize(listings, RATES).cheapest_listing_age_seconds is None
    naive = [Listing(10, "chaos", datetime(2026, 1, 1, 11, 59))]
    assert summarize(naive, RATES, NOW).cheapest_listing_age_seconds == 60
