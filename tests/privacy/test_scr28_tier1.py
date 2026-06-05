"""Tests for the SCR-28 Tier-1 orchestration (`tier1_pii_masking.py`) and the
footprint helpers (`measure_footprint.py`) — the pure, behavior-bearing logic
that decides Tier-1 numbers and the U7 tables. Model-free and dataset-free: the
``datasets`` import is deferred, so these never touch the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCR28_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "scr28"
if str(_SCR28_DIR) not in sys.path:
    sys.path.insert(0, str(_SCR28_DIR))

import measure_footprint as mf  # noqa: E402
import scorer  # noqa: E402
import tier1_pii_masking as t1  # noqa: E402
from label_maps import OUT_OF_SCOPE  # noqa: E402
from schema import CasePrediction, PredictedSpan, PredictionFile  # noqa: E402

from screencap.privacy import EntityType, normalize_text  # noqa: E402

pytestmark = pytest.mark.privacy

# The 27 native classes harvested from the ai4privacy English validation split.
# If the dataset adds a class, this list (and the map) should be revisited — a
# guard against silently dropping a future high-priority type as "unknown".
HARVESTED_300K_LABELS = [
    "TIME", "USERNAME", "PASSPORT", "GIVENNAME1", "SEX", "BOD", "LASTNAME1",
    "TEL", "IDCARD", "EMAIL", "TITLE", "DRIVERLICENSE", "DATE", "IP", "STATE",
    "POSTCODE", "BUILDING", "CITY", "STREET", "SOCIALNUMBER", "COUNTRY", "PASS",
    "GIVENNAME2", "SECADDRESS", "LASTNAME2", "GEOCOORD", "LASTNAME3",
]


class TestPii300kMapping:
    def test_every_harvested_label_is_mapped(self):
        """No harvested class falls through to None (silent-drop guard)."""
        unmapped = [lab for lab in HARVESTED_300K_LABELS if t1.map_pii300k_label(lab) is None]
        assert unmapped == []

    @pytest.mark.parametrize(
        "native,expected",
        [
            ("GIVENNAME1", EntityType.PERSON),
            ("GIVENNAME2", EntityType.PERSON),
            ("LASTNAME1", EntityType.PERSON),
            ("LASTNAME3", EntityType.PERSON),
            ("EMAIL", EntityType.EMAIL),
            ("TEL", EntityType.PHONE),
            ("SOCIALNUMBER", EntityType.SSN),
            ("STREET", EntityType.ADDRESS),
            ("CITY", EntityType.ADDRESS),
            ("POSTCODE", EntityType.ADDRESS),
            ("SECADDRESS", EntityType.ADDRESS),
        ],
    )
    def test_shared_types_map_to_entitytype(self, native, expected):
        assert t1.map_pii300k_label(native) == expected

    @pytest.mark.parametrize(
        "native", ["PASS", "PASSPORT", "IDCARD", "DRIVERLICENSE", "TITLE", "USERNAME", "IP", "TIME", "DATE"]
    )
    def test_deliberate_drops_are_out_of_scope_not_unknown(self, native):
        # OUT_OF_SCOPE (visible in audit) is distinct from None (unknown).
        assert t1.map_pii300k_label(native) == OUT_OF_SCOPE

    def test_unknown_label_is_none(self):
        assert t1.map_pii300k_label("CREDITCARDNUMBER") is None
        assert t1.map_pii300k_label("NOTACLASS") is None


class TestRecordToCase:
    def test_in_scope_spans_become_expected_drops_tallied(self):
        rec = {
            "id": "r1",
            "language": "English",
            "source_text": "Mr Jane Roe, jane@x.com, 12 Oak St",
            "privacy_mask": [
                {"value": "Mr", "start": 0, "end": 2, "label": "TITLE"},
                {"value": "Jane", "start": 3, "end": 7, "label": "GIVENNAME1"},
                {"value": "Roe", "start": 8, "end": 11, "label": "LASTNAME1"},
                {"value": "jane@x.com", "start": 13, "end": 23, "label": "EMAIL"},
                {"value": "12 Oak St", "start": 25, "end": 34, "label": "STREET"},
            ],
        }
        case, dropped = t1.record_to_case(rec, 0)
        kinds = sorted((e.entity_type, e.substring) for e in case.expected)
        assert kinds == [
            ("ADDRESS", "12 Oak St"),
            ("EMAIL", "jane@x.com"),
            ("PERSON", "Jane"),
            ("PERSON", "Roe"),
        ]
        assert dropped["TITLE"] == 1
        assert case.is_false_positive is False

    def test_all_out_of_scope_record_is_false_positive(self):
        """A record whose every span dropped is a no-PII (FP) case, not skipped."""
        rec = {
            "id": "r2",
            "language": "English",
            "source_text": "Logged in at 10:30 as alice99",
            "privacy_mask": [
                {"value": "10:30", "start": 13, "end": 18, "label": "TIME"},
                {"value": "alice99", "start": 22, "end": 29, "label": "USERNAME"},
            ],
        }
        case, dropped = t1.record_to_case(rec, 1)
        assert case.expected == []
        assert case.is_false_positive is True
        assert dropped["TIME"] == 1 and dropped["USERNAME"] == 1


class TestBinaryPiiF1:
    def _case(self):
        from tests.privacy.fixtures.test_corpus import CorpusCase, ExpectedEntity

        text = "Email Jane Roe at jane@x.com"
        return CorpusCase(
            id="c1",
            description="t",
            text=text,
            expected=[
                ExpectedEntity(EntityType.PERSON, "Jane Roe", None),
                ExpectedEntity(EntityType.EMAIL, "jane@x.com", None),
            ],
            is_false_positive=False,
            frequency=None,
        )

    def _span(self, norm, sub, label):
        i = norm.find(sub)
        return PredictedSpan(start=i, end=i + len(sub), label=label)

    def test_perfect_prediction_scores_one(self):
        case = self._case()
        norm = normalize_text(case.text)
        pf = PredictionFile(
            "gliner", "tier1",
            [CasePrediction("c1", [self._span(norm, "Jane Roe", "PERSON"),
                                   self._span(norm, "jane@x.com", "EMAIL")])],
        )
        m = t1.binary_pii_f1(pf, [case])
        assert m["precision"] == 1.0 and m["recall"] == 1.0 and m["f1"] == 1.0

    def test_empty_prediction_recall_zero(self):
        case = self._case()
        pf = PredictionFile("gliner", "tier1", [CasePrediction("c1", [])])
        m = t1.binary_pii_f1(pf, [case])
        assert m["recall"] == 0.0 and m["fn_chars"] > 0

    def test_out_of_scope_prediction_does_not_count_as_pii(self):
        """A privacy-filter `private_url` span is not a PII positive (no FP)."""
        case = self._case()
        norm = normalize_text(case.text)
        # predict the two real spans + one OUT_OF_SCOPE url-ish span over "at"
        pf = PredictionFile(
            "privacy-filter", "tier1",
            [CasePrediction("c1", [
                self._span(norm, "Jane Roe", "private_person"),
                self._span(norm, "jane@x.com", "private_email"),
                self._span(norm, "at", "private_url"),  # OUT_OF_SCOPE -> ignored
            ])],
        )
        m = t1.binary_pii_f1(pf, [case])
        assert m["fp_chars"] == 0
        assert m["f1"] == 1.0


class TestFootprintHelpers:
    @pytest.mark.parametrize(
        "values,pct,expected",
        [
            ([10], 90, 10),
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90, 9),
            ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 100, 10),
            ([5, 1, 3, 2, 4], 50, 3),  # unsorted input
        ],
    )
    def test_percentile_nearest_rank(self, values, pct, expected):
        assert mf._percentile([float(v) for v in values], pct) == expected

    def test_human_readable_sizes(self):
        assert mf._human(0) == "0.0 B"
        assert mf._human(1024) == "1.0 KB"
        assert mf._human(196 * 1024 * 1024).endswith("MB")

    def test_coverage_delta_surfaces_ssn_creditcard_gap(self):
        """GLiNER NER reaches SSN + CREDIT_CARD; privacy-filter NER does not."""
        rows = mf.coverage_delta_rows()
        pf_types = {m for b, _n, m in rows if b == "privacy-filter"}
        gl_types = {m for b, _n, m in rows if b == "gliner"}
        gap = gl_types - pf_types
        assert EntityType.SSN in gap
        assert EntityType.CREDIT_CARD in gap

    def test_disk_uncached_repo_reports_not_cached(self):
        out = mf.measure_disk("openai/definitely-not-a-real-repo-xyz")
        assert out["cached"] is False and out["bytes"] == 0


class TestFullPipelineUnion:
    """U6 union: privacy-filter native NER ∪ GLiNER EntityType regex/secrets."""

    def test_remap_drops_out_of_scope_and_relabels_to_entitytype(self):
        pf = PredictionFile("privacy-filter", "tier2", [CasePrediction("c1", [
            PredictedSpan(0, 5, "private_person"),
            PredictedSpan(6, 9, "private_url"),  # OUT_OF_SCOPE -> dropped
        ])])
        out = scorer.remap_file_to_entitytype(pf)
        assert out.model == "gliner"  # so a later union maps by identity
        spans = out.predictions[0].spans
        assert [s.label for s in spans] == [EntityType.PERSON]

    def test_union_concatenates_spans_per_case_across_vocabularies(self):
        # privacy-filter NER (native) provides PERSON; GLiNER regex/secrets provides SSN.
        pf_ner = scorer.remap_file_to_entitytype(
            PredictionFile("privacy-filter", "tier2",
                           [CasePrediction("c1", [PredictedSpan(0, 5, "private_person")])])
        )
        regex = PredictionFile("gliner", "tier2",
                               [CasePrediction("c1", [PredictedSpan(10, 21, "SSN", source="regex")])])
        union = scorer.union_prediction_files([pf_ner, scorer.remap_file_to_entitytype(regex)])
        labels = sorted(s.label for s in union.predictions[0].spans)
        assert labels == [EntityType.PERSON, EntityType.SSN]
