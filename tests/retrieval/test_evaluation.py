import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix_mcp_knowledge.errors import ConfigurationError
from helix_mcp_knowledge.evaluation import evaluate_retrieval, load_evaluation_cases


class FakeEngine:
    def __init__(
        self,
        *,
        reranker_available: bool = True,
        empty_queries: set[str] | None = None,
    ) -> None:
        self.reranker = object() if reranker_available else None
        self.reranker_available = reranker_available
        self.empty_queries = empty_queries or set()
        self.calls: list[str] = []
        self.config = SimpleNamespace(
            retrieval=SimpleNamespace(
                semantic=SimpleNamespace(enabled=True),
                reranker=SimpleNamespace(enabled=False),
            )
        )

    def search(self, request, *, reranker_enabled=None):
        mode = (
            "reranked"
            if self.config.retrieval.reranker.enabled
            else "baseline"
            if self.config.retrieval.semantic.enabled
            else "lexical"
        )
        self.calls.append(mode)
        if request.query in self.empty_queries:
            return SimpleNamespace(results=[])
        expected = SimpleNamespace(
            document_id="doc-expected",
            title="Expected",
            heading_path=[],
            text="normalization and reconciliation",
            match=SimpleNamespace(reranked=self.config.retrieval.reranker.enabled),
        )
        unrelated = SimpleNamespace(
            document_id="doc-other",
            title="Other",
            heading_path=[],
            text="unrelated",
            match=SimpleNamespace(reranked=self.config.retrieval.reranker.enabled),
        )
        reranker_applied = self.config.retrieval.reranker.enabled and self.reranker_available
        expected.match.reranked = reranker_applied
        unrelated.match.reranked = reranker_applied
        if reranker_applied:
            results = [unrelated, expected]
        elif self.config.retrieval.semantic.enabled:
            results = [expected, unrelated]
        else:
            results = [unrelated]
        return SimpleNamespace(results=results)


def test_evaluation_compares_lexical_and_hybrid_rankings(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "name": "conceptual wording",
                    "query": "data consistency jobs",
                    "product": "cmdb",
                    "version": "26.1",
                    "expected_terms": ["normalization", "reconciliation"],
                }
            ]
        ),
        encoding="utf-8",
    )

    timings = iter([0.0, 0.001, 1.0, 1.002, 2.0, 2.004])
    result = evaluate_retrieval(
        FakeEngine(),
        load_evaluation_cases(dataset, top_k=5),
        timer=lambda: next(timings),
    )

    summary = result["summary"]
    assert {
        key: summary[key]
        for key in ("total", "lexical_hits", "hybrid_hits", "improved", "lexical_mrr", "hybrid_mrr")
    } == {
        "total": 1,
        "lexical_hits": 0,
        "hybrid_hits": 1,
        "improved": 1,
        "lexical_mrr": 0.0,
        "hybrid_mrr": 1.0,
    }
    assert summary["reranker"] == {
        "semantic_enabled": True,
        "backend_present": True,
        "status": "evaluated",
        "comparison_valid": True,
        "guidance": None,
        "applicable_cases": 1,
        "applied_cases": 1,
        "scored_results": 2,
        "returned_results": 2,
        "baseline_hits": 1,
        "reranked_hits": 1,
        "improved": 0,
        "regressed": 1,
        "baseline_mrr": 1.0,
        "reranked_mrr": 0.5,
        "baseline_recall_at_k": 1.0,
        "reranked_recall_at_k": 1.0,
        "baseline_ndcg_at_k": 1.0,
        "reranked_ndcg_at_k": 0.6309,
    }
    assert result["cases"][0]["latency_ms"] == {
        "lexical": 1.0,
        "baseline": 2.0,
        "reranked": 4.0,
    }
    assert summary["latency_ms"] == {
        "lexical": {"p50": 1.0, "p95": 1.0},
        "baseline": {"p50": 2.0, "p95": 2.0},
        "reranked": {"p50": 4.0, "p95": 4.0},
    }
    assert result["cases"][0]["measurement_order"] == [
        "lexical",
        "baseline",
        "reranked",
    ]
    assert summary["measurement"] == {
        "warmup": True,
        "warmup_searches": 3,
        "order_strategy": "rotating",
        "modes": ["lexical", "baseline", "reranked"],
    }


def test_evaluation_can_compare_reranker_with_semantic_disabled(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "query": "data consistency jobs",
                    "expected_document_ids": ["doc-expected"],
                }
            ]
        ),
        encoding="utf-8",
    )
    engine = FakeEngine()
    engine.config.retrieval.semantic.enabled = False

    result = evaluate_retrieval(engine, load_evaluation_cases(dataset, top_k=5))

    assert result["summary"]["reranker"]["semantic_enabled"] is False
    assert engine.config.retrieval.semantic.enabled is False
    assert engine.config.retrieval.reranker.enabled is False


def test_evaluation_exposes_when_no_reranker_was_actually_applied(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.json"
    dataset.write_text(
        json.dumps([{"query": "data consistency", "expected_terms": ["normalization"]}]),
        encoding="utf-8",
    )

    engine = FakeEngine(reranker_available=False)
    result = evaluate_retrieval(engine, load_evaluation_cases(dataset, top_k=5))

    reranker = result["summary"]["reranker"]
    assert reranker["backend_present"] is False
    assert reranker["status"] == "skipped_backend_unavailable"
    assert reranker["comparison_valid"] is False
    assert "Enable and install" in reranker["guidance"]
    assert reranker["applicable_cases"] == 0
    assert reranker["applied_cases"] == 0
    assert reranker["scored_results"] == 0
    assert reranker["reranked_hits"] is None
    assert reranker["reranked_mrr"] is None
    assert reranker["reranked_recall_at_k"] is None
    assert reranker["reranked_ndcg_at_k"] is None
    assert result["cases"][0]["reranker_applied"] is False
    assert result["cases"][0]["reranked_rank"] is None
    assert result["cases"][0]["reranked_recall_at_k"] is None
    assert result["cases"][0]["reranked_ndcg_at_k"] is None
    assert result["cases"][0]["latency_ms"]["reranked"] is None
    assert result["summary"]["latency_ms"]["reranked"] == {"p50": None, "p95": None}
    assert result["summary"]["measurement"]["modes"] == ["lexical", "baseline"]
    assert "reranked" not in engine.calls


def test_evaluation_invalidates_comparison_when_configured_backend_never_applies(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "evaluation.json"
    dataset.write_text(
        json.dumps([{"query": "data consistency", "expected_terms": ["normalization"]}]),
        encoding="utf-8",
    )
    engine = FakeEngine()
    # Preserve a configured backend object while simulating a cold or failed service.
    engine.reranker_available = False

    result = evaluate_retrieval(engine, load_evaluation_cases(dataset, top_k=5))

    reranker = result["summary"]["reranker"]
    assert reranker["backend_present"] is True
    assert reranker["status"] == "backend_not_applied"
    assert reranker["comparison_valid"] is False
    assert reranker["applicable_cases"] == 1
    assert reranker["applied_cases"] == 0
    assert reranker["reranked_hits"] is None
    assert reranker["improved"] is None
    assert reranker["regressed"] is None
    assert result["cases"][0]["reranker_comparison_valid"] is False
    assert result["cases"][0]["reranked_rank"] is None
    assert result["cases"][0]["reranked_recall_at_k"] is None
    assert result["cases"][0]["reranked_ndcg_at_k"] is None


def test_evaluation_counts_empty_cases_as_zero_in_valid_reranker_metrics(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "evaluation.json"
    dataset.write_text(
        json.dumps(
            [
                {"query": "no matching documents", "expected_terms": ["missing"]},
                {"query": "data consistency", "expected_terms": ["normalization"]},
            ]
        ),
        encoding="utf-8",
    )

    result = evaluate_retrieval(
        FakeEngine(empty_queries={"no matching documents"}),
        load_evaluation_cases(dataset, top_k=5),
    )

    reranker = result["summary"]["reranker"]
    assert reranker["comparison_valid"] is True
    assert reranker["applicable_cases"] == 1
    assert reranker["applied_cases"] == 1
    assert reranker["reranked_recall_at_k"] == 0.5
    assert reranker["reranked_ndcg_at_k"] == 0.3155
    assert result["cases"][0]["reranker_comparison_valid"] is False
    assert result["cases"][0]["reranked_recall_at_k"] == 0.0
    assert result["cases"][0]["reranked_ndcg_at_k"] == 0.0


def test_evaluation_reports_missing_dataset_without_raw_os_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not read evaluation dataset"):
        load_evaluation_cases(tmp_path / "missing.json", top_k=5)


def test_evaluation_reports_invalid_json(tmp_path: Path) -> None:
    dataset = tmp_path / "invalid.json"
    dataset.write_text("not-json", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_evaluation_cases(dataset, top_k=5)


def test_evaluation_rejects_ambiguous_expected_evidence(tmp_path: Path) -> None:
    dataset = tmp_path / "ambiguous.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "query": "reconciliation",
                    "expected_document_ids": ["doc-expected"],
                    "expected_terms": ["normalization"],
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="or expected_terms, not both"):
        load_evaluation_cases(dataset, top_k=5)


def test_evaluation_normalizes_and_deduplicates_expected_terms(tmp_path: Path) -> None:
    dataset = tmp_path / "normalized.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "query": "reconciliation",
                    "expected_terms": [" Normalization ", "normalization", "RECONCILIATION"],
                }
            ]
        ),
        encoding="utf-8",
    )

    cases = load_evaluation_cases(dataset, top_k=5)

    assert cases[0].expected_terms == ["normalization", "reconciliation"]


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_evaluation_rejects_empty_expected_term_values(tmp_path: Path, value: str) -> None:
    dataset = tmp_path / "empty-term.json"
    dataset.write_text(
        json.dumps([{"query": "reconciliation", "expected_terms": [value]}]),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="empty expected_terms"):
        load_evaluation_cases(dataset, top_k=5)


def test_evaluation_rotates_timed_mode_order_after_warmup(tmp_path: Path) -> None:
    dataset = tmp_path / "rotating.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "name": f"case-{index}",
                    "query": "consistency",
                    "expected_terms": ["normalization"],
                }
                for index in range(3)
            ]
        ),
        encoding="utf-8",
    )
    engine = FakeEngine()

    report = evaluate_retrieval(engine, load_evaluation_cases(dataset, top_k=5))

    assert engine.calls == [
        "lexical",
        "baseline",
        "reranked",
        "lexical",
        "baseline",
        "reranked",
        "baseline",
        "reranked",
        "lexical",
        "reranked",
        "lexical",
        "baseline",
    ]
    assert [case["measurement_order"] for case in report["cases"]] == [
        ["lexical", "baseline", "reranked"],
        ["baseline", "reranked", "lexical"],
        ["reranked", "lexical", "baseline"],
    ]
