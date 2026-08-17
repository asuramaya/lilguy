"""Covers every posted_at format actually observed in the live corpus,
plus the failure modes that would silently produce a wrong date.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from posted_at import parse_posted_at  # noqa: E402

SEEN = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_iso_with_offset_is_converted_to_utc():
    # Real greenhouse value from the live corpus.
    ts, approx = parse_posted_at("2026-07-09T10:58:08-04:00", SEEN)
    assert ts == datetime(2026, 7, 9, 14, 58, 8, tzinfo=timezone.utc)
    assert approx is False


def test_iso_with_trailing_z_is_accepted():
    ts, _ = parse_posted_at("2026-07-09T10:58:08Z", SEEN)
    assert ts == datetime(2026, 7, 9, 10, 58, 8, tzinfo=timezone.utc)


def test_naive_iso_is_treated_as_utc_not_local():
    # Guessing a local zone here would shift dates by up to a day.
    ts, _ = parse_posted_at("2026-07-09T10:58:08", SEEN)
    assert ts == datetime(2026, 7, 9, 10, 58, 8, tzinfo=timezone.utc)


def test_date_only_is_parsed_but_flagged_approximate():
    ts, approx = parse_posted_at("2026-07-09", SEEN)
    assert ts == datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    assert approx is True  # midnight is the provider's precision, not ours


def test_lever_epoch_milliseconds():
    # Real lever value from the live corpus -- 2021, while the feed was
    # displaying it as "1h ago" off first_seen.
    ts, approx = parse_posted_at("1625176335503", SEEN)
    assert ts.year == 2021 and ts.month == 7
    assert approx is False


def test_epoch_seconds_and_milliseconds_are_discriminated_by_magnitude():
    secs = parse_posted_at("1625176335", SEEN)[0]
    millis = parse_posted_at("1625176335503", SEEN)[0]
    assert secs.year == 2021
    # Same instant expressed either way must land on the same second.
    assert abs((secs - millis).total_seconds()) < 1


def test_workday_relative_days_resolve_against_scrape_time_not_now():
    # Anchoring on seen_at is what makes a backfill reproducible: an old
    # row re-parsed later must yield the date it meant at scrape time.
    ts, approx = parse_posted_at("Posted 2 Days Ago", SEEN)
    assert ts == SEEN - timedelta(days=2)
    assert approx is True


def test_workday_today_and_yesterday():
    assert parse_posted_at("Posted Today", SEEN)[0] == SEEN
    assert parse_posted_at("Posted Yesterday", SEEN)[0] == SEEN - timedelta(days=1)


def test_workday_30_plus_is_a_lower_bound_and_says_so():
    # The most common workday value in the corpus (141 open postings).
    # "30+" is a ceiling Workday stops counting at, so the true age may
    # be far greater -- it must not read as exactly 30 days old.
    ts, approx = parse_posted_at("Posted 30+ Days Ago", SEEN)
    assert ts == SEEN - timedelta(days=30)
    assert approx is True


def test_plus_pattern_wins_over_the_plain_days_pattern():
    # Regression guard: a plain \d+ match would also match "30+ Days
    # Ago" and silently drop the bound, reporting it as exact.
    assert parse_posted_at("Posted 30+ Days Ago", SEEN)[1] is True


def test_missing_and_unparseable_values_degrade_to_unknown():
    for bad in [None, "", "   ", "sometime last spring", "Posted whenever"]:
        ts, approx = parse_posted_at(bad, SEEN)
        assert ts is None
        assert approx is False


def test_a_datetime_passes_through_normalized():
    ts, _ = parse_posted_at(datetime(2026, 7, 9, tzinfo=timezone.utc), SEEN)
    assert ts == datetime(2026, 7, 9, tzinfo=timezone.utc)


def test_absurd_epoch_does_not_raise():
    # A provider sending garbage must cost one row's date, not the scrape.
    ts, _ = parse_posted_at("999999999999999999999", SEEN)
    assert ts is None
