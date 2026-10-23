"""Tests for Chunky Tasks interval math, range calculations, and coverage logic."""

import pytest

from todo.services.chunky_service import (
    calculate_coverage,
    format_range,
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
