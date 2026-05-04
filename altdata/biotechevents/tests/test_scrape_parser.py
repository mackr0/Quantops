"""Tests for ClinicalTrials.gov v2 JSON parser. Uses a tiny synthetic
study record matching the real API shape."""

from biotechevents.scrape_clinicaltrials import parse_study


def _study(**overrides):
    base = {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT99999999",
                "briefTitle": "A Study of Test Drug",
            },
            "sponsorCollaboratorsModule": {
                "leadSponsor": {
                    "name": "Moderna",
                    "class": "INDUSTRY",
                },
            },
            "statusModule": {
                "overallStatus": "RECRUITING",
                "primaryCompletionDateStruct": {"date": "2025-12-31"},
                "completionDateStruct": {"date": "2026-06-30"},
                "startDateStruct": {"date": "2024-01-15"},
                "lastUpdatePostDateStruct": {"date": "2026-04-01"},
            },
            "designModule": {
                "phases": ["PHASE2"],
                "enrollmentInfo": {"count": 250},
            },
            "conditionsModule": {
                "conditions": ["COVID-19", "Influenza"],
            },
            "armsInterventionsModule": {
                "interventions": [
                    {"name": "mRNA-1234"},
                    {"name": "Placebo"},
                ],
            },
        },
    }
    # Apply overrides at protocolSection level for simplicity
    for k, v in overrides.items():
        base["protocolSection"][k] = v
    return base


class TestParseStudy:
    def test_extracts_id_and_title(self):
        out = parse_study(_study())
        assert out["nct_id"] == "NCT99999999"
        assert "Test Drug" in out["brief_title"]

    def test_maps_sponsor_to_ticker(self):
        out = parse_study(_study())
        assert out["sponsor_name"] == "Moderna"
        assert out["ticker"] == "MRNA"

    def test_normalizes_phase(self):
        out = parse_study(_study())
        assert out["phase"] == "PHASE2"

    def test_combined_phase(self):
        s = _study(designModule={
            "phases": ["PHASE1", "PHASE2"],
            "enrollmentInfo": {"count": 50},
        })
        out = parse_study(s)
        # Combined phases are joined with underscore (PHASE1_PHASE2)
        assert "PHASE1" in out["phase"] and "PHASE2" in out["phase"]

    def test_normalizes_status(self):
        out = parse_study(_study())
        assert out["overall_status"] == "RECRUITING"

    def test_extracts_dates(self):
        out = parse_study(_study())
        assert out["primary_completion_date"] == "2025-12-31"
        assert out["completion_date"] == "2026-06-30"
        assert out["start_date"] == "2024-01-15"

    def test_extracts_conditions_and_interventions(self):
        out = parse_study(_study())
        assert "COVID-19" in out["conditions"]
        assert "mRNA-1234" in out["interventions"]
        assert "Placebo" in out["interventions"]

    def test_handles_missing_optional_fields(self):
        """A real-world study with no enrollment count or interventions
        should parse cleanly with None / [] in those fields."""
        s = _study()
        del s["protocolSection"]["designModule"]["enrollmentInfo"]
        s["protocolSection"]["armsInterventionsModule"] = {}
        out = parse_study(s)
        assert out["enrollment_count"] is None
        assert out["interventions"] == []

    def test_handles_empty_protocol_section(self):
        """A malformed study record shouldn't crash parse — we get
        an empty-ish dict back."""
        out = parse_study({})
        assert out["nct_id"] == ""
        assert out["sponsor_name"] is None
        assert out["phase"] is None

    def test_unknown_sponsor_no_ticker(self):
        s = _study(sponsorCollaboratorsModule={
            "leadSponsor": {"name": "Random University Hospital", "class": "OTHER"},
        })
        out = parse_study(s)
        assert out["ticker"] is None
        assert out["sponsor_class"] == "OTHER"
