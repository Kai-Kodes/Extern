"""Tests for Chunky Tasks interval math, range calculations, and coverage logic."""

import pytest

from todo.services.chunky_service import (
    calculate_coverage,
    format_human_duration,
    format_range,
    format_remaining,
    merge_intervals,
    propose_next_chunk,
    seconds_to_hms,
)


def test_merge_intervals_empty_and_single():
    assert merge_intervals([]) == []
    assert merge_intervals([(0.0, 10.0)]) == [(0.0, 10.0)]
    assert merge_intervals([(10.0, 5.0)]) == []  # Invalid (start >= end)
    assert merge_intervals([(5.0, 5.0)]) == []


def test_merge_intervals_overlapping_and_adjacent():
    # Adjacent
    assert merge_intervals([(0.0, 10.0), (10.0, 20.0)]) == [(0.0, 20.0)]
    # Overlapping
    assert merge_intervals([(0.0, 15.0), (10.0, 25.0)]) == [(0.0, 25.0)]
    # Subsumed
    assert merge_intervals([(0.0, 30.0), (5.0, 15.0)]) == [(0.0, 30.0)]
    # Discontinuous out-of-order
    assert merge_intervals([(40.0, 60.0), (0.0, 20.0), (20.0, 30.0)]) == [
        (0.0, 30.0),
        (40.0, 60.0),
    ]


def test_merge_intervals_clamping():
    assert merge_intervals([(-10.0, 50.0), (80.0, 120.0)], total_range=100.0) == [
        (0.0, 50.0),
        (80.0, 100.0),
    ]


def test_calculate_coverage_no_double_counting():
    # 90-minute video (5400s) with 0-1200 and 1200-2400
    covered, pct = calculate_coverage([(0.0, 1200.0), (1200.0, 2400.0)], total_range=5400.0)
    assert covered == 2400.0
    assert pct == 44  # 2400 / 5400 = 44.4%

    # Overlapping intervals should not double count:
    # 0-1200 and 600-1800 should be 1800, NOT 2400
    covered_ov, pct_ov = calculate_coverage(
        [(0.0, 1200.0), (600.0, 1800.0)], total_range=5400.0
    )
    assert covered_ov == 1800.0
    assert pct_ov == 33

    # Identical interval repeated (retry scenario)
    covered_rep, pct_rep = calculate_coverage(
        [(0.0, 1200.0), (0.0, 1200.0)], total_range=5400.0
    )
    assert covered_rep == 1200.0
    assert pct_rep == 22


def test_calculate_coverage_full_completion_override():
    # When is_fully_completed is True, coverage is 100% regardless of chunk coverage
    covered, pct = calculate_coverage([(0.0, 600.0)], total_range=5400.0, is_fully_completed=True)
    assert covered == 5400.0
    assert pct == 100


def test_propose_next_chunk_from_zero():
    # 90m video, first chunk of 20m (1200s)
    proposed = propose_next_chunk([], total_range=5400.0, chunk_size=1200.0)
    assert proposed == (0.0, 1200.0)


def test_propose_next_chunk_sequential():
    # After [0, 1200] is complete, propose [1200, 2400]
    proposed = propose_next_chunk([(0.0, 1200.0)], total_range=5400.0, chunk_size=1200.0)
    assert proposed == (1200.0, 2400.0)


def test_propose_next_chunk_final_short_chunk():
    # Total range 2500, completed [0, 2400], requested chunk 1200 -> clamps to 2500
    proposed = propose_next_chunk([(0.0, 2400.0)], total_range=2500.0, chunk_size=1200.0)
    assert proposed == (2400.0, 2500.0)


def test_propose_next_chunk_discontinuous_earliest_gap():
    # User completed chunk 1 [0, 1200] and chunk 3 [2400, 3600], leaving gap [1200, 2400]
    proposed = propose_next_chunk(
        [(0.0, 1200.0), (2400.0, 3600.0)], total_range=5400.0, chunk_size=1200.0
    )
    assert proposed == (1200.0, 2400.0)


def test_propose_next_chunk_gap_smaller_than_chunk_size():
    # Gap from 1000 to 1500 (size 500), but requested chunk_size is 1200
    # Must propose [1000, 1500] and not overshoot into already completed range
    proposed = propose_next_chunk(
        [(0.0, 1000.0), (1500.0, 3000.0)], total_range=5400.0, chunk_size=1200.0
    )
    assert proposed == (1000.0, 1500.0)


def test_propose_next_chunk_already_completed():
    # When resource is 100% complete, returns None
    assert propose_next_chunk([(0.0, 5400.0)], total_range=5400.0, chunk_size=1200.0) is None
    # When fully completed override is True, returns None
    assert propose_next_chunk([(0.0, 100.0)], total_range=5400.0, chunk_size=1200.0, is_fully_completed=True) is None


def test_format_hms_and_ranges():
    assert seconds_to_hms(0) == "00:00:00"
    assert seconds_to_hms(125) == "00:02:05"
    assert seconds_to_hms(3665) == "01:01:05"

    assert format_range("seconds", 1200, 2400) == "00:20:00–00:40:00"
    assert format_range("pages", 1, 10) == "p. 1–10"
    assert format_range("pages", 11, 20) == "p. 11–20"


def test_format_human_duration_46m40s_example():
    """Verify the 46m 40s video example from product specifications:
    - Total duration: 46m 40s (2800s)
    - Completed: 20m (1200s)
    - Remaining: 26m 40s (1600s)
    - Next chunk: 20m (1200s), covering 20:00–40:00
    - Final chunk: 6m 40s (400s), covering 40:00–46:40
    """
    total = 2800.0
    completed = 1200.0
    remaining = total - completed
    next_chunk = 1200.0
    final_chunk = 400.0

    assert format_human_duration(total) == "46m 40s"
    assert format_human_duration(completed) == "20m"
    assert format_human_duration(remaining) == "26m 40s"
    assert format_human_duration(next_chunk) == "20m"
    assert format_human_duration(final_chunk) == "6m 40s"


def test_format_human_duration_whole_and_partial_minutes():
    # Whole minutes
    assert format_human_duration(60) == "1m"
    assert format_human_duration(300) == "5m"
    assert format_human_duration(900) == "15m"
    assert format_human_duration(1200) == "20m"
    assert format_human_duration(1800) == "30m"
    assert format_human_duration(2700) == "45m"
    assert format_human_duration(3600) == "1h"
    assert format_human_duration(5400) == "1h 30m"
    assert format_human_duration(7200) == "2h"

    # Partial minutes (< 60 minutes includes seconds, >= 60 minutes includes hours and minutes)
    assert format_human_duration(45) == "45s"
    assert format_human_duration(90) == "1m 30s"
    assert format_human_duration(3665) == "1h 1m"
    assert format_human_duration(7245) == "2h"


def test_format_human_duration_zero_and_precision():
    # Zero remaining duration
    assert format_human_duration(0) == "0s"
    assert format_human_duration(0.0) == "0s"
    assert format_human_duration(-10) == "0s"

    # Float rounding precision
    assert format_human_duration(2799.999) == "46m 40s"
    assert format_human_duration(1199.9) == "20m"
    assert format_human_duration(0.0001) == "0s"


def test_format_human_duration_standardized_policy():
    """Verify standardized duration policy:
    - Less than 60 seconds: seconds, e.g. 45s
    - Less than 60 minutes: minutes and remaining seconds, e.g. 26m 40s
    - At least 60 minutes: hours and remaining minutes, e.g. 1h 18m
    - Exact hours: omit zero-valued smaller units, e.g. 2h
    - Durations above 24 hours remain expressed in hours, e.g. 36h 20m, 168h
    - Zero duration: 0s
    - No accidental rounding up to 1h when below 60m
    """
    # Zero and sub-minute durations
    assert format_human_duration(0) == "0s"
    assert format_human_duration(0.0) == "0s"
    assert format_human_duration(-10) == "0s"
    assert format_human_duration(0.0001) == "0s"
    assert format_human_duration(1) == "1s"
    assert format_human_duration(45) == "45s"
    assert format_human_duration(59) == "59s"

    # Exactly 59 minutes, 59 minutes 59 seconds, and exactly 60 minutes
    assert format_human_duration(3540) == "59m"
    assert format_human_duration(3599) == "59m 59s"
    assert format_human_duration(3600) == "1h"

    # 78 minutes displaying as 1h 18m
    assert format_human_duration(78 * 60) == "1h 18m"

    # Exact hours and durations with remaining minutes
    assert format_human_duration(3600) == "1h"
    assert format_human_duration(7200) == "2h"
    assert format_human_duration(5400) == "1h 30m"
    assert format_human_duration(3665) == "1h 1m"
    assert format_human_duration(7245) == "2h"
    assert format_human_duration(23 * 3600 + 59 * 60) == "23h 59m"

    # 24 hours, 36 hours, and 168 hours staying in hour notation (never days/weeks)
    assert format_human_duration(24 * 3600) == "24h"
    assert format_human_duration(36 * 3600) == "36h"
    assert format_human_duration(36 * 3600 + 20 * 60) == "36h 20m"
    assert format_human_duration(168 * 3600) == "168h"
    assert format_human_duration(168 * 3600 + 45 * 60) == "168h 45m"

    # Floating-point durations and boundary rounding
    assert format_human_duration(2799.999) == "46m 40s"
    assert format_human_duration(1199.9) == "20m"
    assert format_human_duration(3599.4) == "59m 59s"
    assert format_human_duration(3599.9) == "59m 59s"  # Protected from rounding to 1h
    assert format_human_duration(3600.0) == "1h"
    assert format_human_duration(3600.4) == "1h"
    assert format_human_duration(4680.2) == "1h 18m"


def test_exact_interval_timestamps_and_pages_unchanged():
    # Exact media interval timestamps formatted as HH:MM:SS
    assert seconds_to_hms(0) == "00:00:00"
    assert seconds_to_hms(1200) == "00:20:00"
    assert seconds_to_hms(2400) == "00:40:00"
    assert seconds_to_hms(2800) == "00:46:40"
    assert seconds_to_hms(86400) == "24:00:00"
    assert seconds_to_hms(130800) == "36:20:00"
    assert format_range("seconds", 1200, 2400) == "00:20:00–00:40:00"
    assert format_range("seconds", 2400, 2800) == "00:40:00–00:46:40"

    # Existing page-based displays remaining unchanged
    assert format_range("pages", 1, 10) == "p. 1–10"
    assert format_range("pages", 11, 20) == "p. 11–20"


def test_format_remaining_units():
    # Media duration (seconds)
    assert format_remaining("seconds", 2800) == "46m 40s"
    assert format_remaining("seconds", 1600) == "26m 40s"
    assert format_remaining("seconds", 1200) == "20m"
    assert format_remaining("seconds", 0) == "0s"

    # Document pages
    assert format_remaining("pages", 750) == "750 pages"
    assert format_remaining("pages", 1) == "1 page"
    assert format_remaining("pages", 0) == "0 pages"
    assert format_remaining("pages", 15.0) == "15 pages"

