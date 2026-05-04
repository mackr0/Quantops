"""Pure-function tests for normalize.py."""

from biotechevents.normalize import (
    NEGATIVE_STATUS,
    POSITIVE_STATUS,
    normalize_date,
    normalize_phase,
    normalize_status,
    sponsor_to_ticker,
)


class TestPhase:
    def test_modern_format(self):
        assert normalize_phase("PHASE1") == "PHASE1"
        assert normalize_phase("PHASE3") == "PHASE3"

    def test_with_space(self):
        assert normalize_phase("Phase 2") == "PHASE2"

    def test_roman_numeral(self):
        assert normalize_phase("PHASE II") == "PHASE2"
        assert normalize_phase("PHASE III") == "PHASE3"

    def test_combined(self):
        assert normalize_phase("PHASE1_PHASE2") == "PHASE1_PHASE2"
        assert normalize_phase("Phase 1/Phase 2") == "PHASE1_PHASE2"

    def test_na_variants(self):
        assert normalize_phase("Not Applicable") == "NA"
        assert normalize_phase("NA") == "NA"

    def test_empty(self):
        assert normalize_phase(None) is None
        assert normalize_phase("") is None


class TestStatus:
    def test_recruiting(self):
        assert normalize_status("RECRUITING") == "RECRUITING"

    def test_active_not_recruiting(self):
        assert normalize_status("Active, not recruiting") == "ACTIVE_NOT_RECRUITING"
        assert normalize_status("ACTIVE_NOT_RECRUITING") == "ACTIVE_NOT_RECRUITING"

    def test_terminated(self):
        assert normalize_status("Terminated") == "TERMINATED"

    def test_categorization_buckets(self):
        # Used by downstream signal layer to classify status changes
        assert "TERMINATED" in NEGATIVE_STATUS
        assert "WITHDRAWN" in NEGATIVE_STATUS
        assert "SUSPENDED" in NEGATIVE_STATUS
        assert "COMPLETED" in POSITIVE_STATUS


class TestDate:
    def test_iso_passthrough(self):
        assert normalize_date("2025-09-30") == "2025-09-30"

    def test_year_month(self):
        # Many ClinicalTrials dates are YYYY-MM (estimated month)
        assert normalize_date("2025-09") == "2025-09-01"

    def test_year_only(self):
        assert normalize_date("2025") == "2025-01-01"

    def test_garbage(self):
        assert normalize_date("not a date") is None
        assert normalize_date(None) is None
        assert normalize_date("") is None


class TestSponsorToTicker:
    def test_exact_match(self):
        assert sponsor_to_ticker("Moderna") == "MRNA"

    def test_with_corporate_suffix(self):
        assert sponsor_to_ticker("Moderna Inc") == "MRNA"
        assert sponsor_to_ticker("Pfizer Inc.") == "PFE"

    def test_case_insensitive(self):
        assert sponsor_to_ticker("PFIZER") == "PFE"

    def test_substring_fallback(self):
        # "Eli Lilly and Company Limited" → strip suffix → still maps via substring
        assert sponsor_to_ticker("Eli Lilly and Company Limited") == "LLY"

    def test_unknown_returns_none(self):
        # Strict — small no-name biotech we haven't mapped yet
        assert sponsor_to_ticker("XYZ Therapeutics") is None

    def test_empty_returns_none(self):
        assert sponsor_to_ticker(None) is None
        assert sponsor_to_ticker("") is None

    def test_janssen_maps_to_jnj(self):
        # Real-world: Janssen is J&J's pharma division but files as Janssen
        assert sponsor_to_ticker("Janssen Research & Development") == "JNJ"
        assert sponsor_to_ticker("Janssen Pharmaceuticals") == "JNJ"

    def test_genentech_maps_to_roche(self):
        assert sponsor_to_ticker("Genentech") == "ROG.SW"
