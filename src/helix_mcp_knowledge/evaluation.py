"""Repeatable lexical-versus-hybrid retrieval evaluation."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError
from .models.search import SearchRequest, SearchResponse, SearchResult
from .retrieval.search_engine import SearchEngine

_EVALUATION_MODES = ("lexical", "baseline", "reranked")


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    request: SearchRequest
    expected_document_ids: set[str]
    expected_terms: list[str]


def load_evaluation_cases(path: str | Path, *, top_k: int) -> list[EvaluationCase]:
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"could not read evaluation dataset {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"evaluation dataset is not valid JSON: {source}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ConfigurationError("evaluation dataset must be a non-empty JSON list")
    cases = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"evaluation case {index} must be an object")
        expected_ids = item.get("expected_document_ids", [])
        expected_terms = item.get("expected_terms", [])
        if not isinstance(expected_ids, list) or not all(
            isinstance(value, str) for value in expected_ids
        ):
            raise ConfigurationError(f"evaluation case {index} has invalid expected_document_ids")
        if not isinstance(expected_terms, list) or not all(
            isinstance(value, str) for value in expected_terms
        ):
            raise ConfigurationError(f"evaluation case {index} has invalid expected_terms")
        normalized_terms: list[str] = []
        seen_terms: set[str] = set()
        for value in expected_terms:
            term = value.strip().casefold()
            if not term:
                raise ConfigurationError(
                    f"evaluation case {index} has an empty expected_terms value"
                )
            if term not in seen_terms:
                seen_terms.add(term)
                normalized_terms.append(term)
        if not expected_ids and not normalized_terms:
            raise ConfigurationError(f"evaluation case {index} requires expected evidence")
        if expected_ids and normalized_terms:
            raise ConfigurationError(
                f"evaluation case {index} must use expected_document_ids "
                "or expected_terms, not both"
            )
        request_payload = {
            key: value
            for key, value in item.items()
            if key not in {"name", "expected_document_ids", "expected_terms"}
        }
        request_payload["top_k"] = top_k
        cases.append(
            EvaluationCase(
                name=str(item.get("name") or f"case-{index}"),
                request=SearchRequest.model_validate(request_payload),
                expected_document_ids=set(expected_ids),
                expected_terms=normalized_terms,
            )
        )
    return cases


def evaluate_retrieval(
    engine: SearchEngine,
    cases: list[EvaluationCase],
    *,
    timer: Callable[[], float] | None = None,
) -> dict[str, object]:
    """Compare lexical, configured baseline, and optional reranked retrieval.

    Existing lexical/hybrid fields are retained for callers of the original evaluator.
    The baseline/reranked comparison is independent of whether semantic retrieval is
    enabled, which permits lightweight reranker evaluation without a vector backend.
    """

    semantic_enabled = bool(engine.config.retrieval.semantic.enabled)
    reranker_settings = getattr(engine.config.retrieval, "reranker", None)
    reranker_backend_present = getattr(engine, "reranker", None) is not None
    reranker_comparison_available = reranker_settings is not None and reranker_backend_present
    clock = timer or time.perf_counter
    measured_modes = _EVALUATION_MODES if reranker_comparison_available else _EVALUATION_MODES[:2]
    if cases:
        # Warm every mode outside the timer so cold model and SQLite startup do not
        # dominate the first measured case.
        for mode in measured_modes:
            _search_mode(
                engine,
                cases[0].request,
                mode=mode,
                semantic_enabled=semantic_enabled,
            )
    results = []
    for case_index, case in enumerate(cases):
        offset = case_index % len(measured_modes)
        measurement_order = (*measured_modes[offset:], *measured_modes[:offset])
        measured: dict[str, tuple[SearchResponse, float]] = {}
        for mode in measurement_order:
            measured[mode] = _timed_mode_search(
                clock,
                engine,
                case.request,
                mode=mode,
                semantic_enabled=semantic_enabled,
            )
        lexical, lexical_latency_ms = measured["lexical"]
        baseline, baseline_latency_ms = measured["baseline"]
        if reranker_comparison_available:
            reranked, reranked_latency_ms = measured["reranked"]
            reranked_result_count = _reranked_result_count(reranked)
            returned_result_count = len(reranked.results)
            reranker_case_valid = (
                returned_result_count > 0 and reranked_result_count == returned_result_count
            )
            reranked_rank = _expected_rank(reranked, case) if reranker_case_valid else None
            if returned_result_count == 0:
                # A genuinely empty retrieval case is not evidence of a reranker
                # backend failure, but it must still count as zero in quality
                # aggregates rather than disappearing and inflating the result.
                reranked_recall_at_k: float | None = 0.0
                reranked_ndcg_at_k: float | None = 0.0
            elif reranker_case_valid:
                reranked_recall_at_k = _recall_at_k(reranked, case)
                reranked_ndcg_at_k = _ndcg_at_k(reranked, case)
            else:
                reranked_recall_at_k = None
                reranked_ndcg_at_k = None
        else:
            reranked_latency_ms = None
            reranked_rank = None
            reranked_result_count = 0
            returned_result_count = 0
            reranker_case_valid = False
            reranked_recall_at_k = None
            reranked_ndcg_at_k = None
        lexical_rank = _expected_rank(lexical, case)
        baseline_rank = _expected_rank(baseline, case)
        results.append(
            {
                "name": case.name,
                "query": case.request.query,
                "lexical_rank": lexical_rank,
                # ``hybrid`` remains the configured non-reranked baseline for compatibility.
                "hybrid_rank": baseline_rank,
                "improved": baseline_rank is not None
                and (lexical_rank is None or baseline_rank < lexical_rank),
                "baseline_rank": baseline_rank,
                "reranked_rank": reranked_rank,
                "reranker_comparison_valid": reranker_case_valid,
                "reranker_improved": reranker_case_valid
                and reranked_rank is not None
                and (baseline_rank is None or reranked_rank < baseline_rank),
                "reranker_regressed": reranker_case_valid
                and baseline_rank is not None
                and (reranked_rank is None or reranked_rank > baseline_rank),
                "reranker_applied": reranked_result_count > 0,
                "reranked_result_count": reranked_result_count,
                "returned_result_count": returned_result_count,
                "baseline_recall_at_k": _recall_at_k(baseline, case),
                "reranked_recall_at_k": reranked_recall_at_k,
                "baseline_ndcg_at_k": _ndcg_at_k(baseline, case),
                "reranked_ndcg_at_k": reranked_ndcg_at_k,
                "latency_ms": {
                    "lexical": lexical_latency_ms,
                    "baseline": baseline_latency_ms,
                    "reranked": reranked_latency_ms,
                },
                "measurement_order": list(measurement_order),
            }
        )
    lexical_ranks = [item["lexical_rank"] for item in results]
    baseline_ranks = [item["baseline_rank"] for item in results]
    reranked_ranks = [item["reranked_rank"] for item in results]
    reranker_applied_cases = sum(bool(item["reranker_comparison_valid"]) for item in results)
    reranker_applicable_cases = sum(int(item["returned_result_count"]) > 0 for item in results)
    reranker_comparison_valid = (
        reranker_comparison_available
        and reranker_applicable_cases > 0
        and reranker_applied_cases == reranker_applicable_cases
    )
    if not reranker_backend_present:
        reranker_status = "skipped_backend_unavailable"
        reranker_guidance = (
            "Enable and install result reranking, then start a new evaluation process."
        )
    elif reranker_comparison_valid:
        reranker_status = "evaluated"
        reranker_guidance = None
    elif reranker_applied_cases:
        reranker_status = "partial_backend_failure"
        reranker_guidance = (
            "The reranker did not score every non-empty case; verify that its service "
            "is healthy and repeat the evaluation."
        )
    else:
        reranker_status = "backend_not_applied"
        reranker_guidance = (
            "The reranker backend was configured but did not score any non-empty case; "
            "wait until its service is ready and repeat the evaluation."
        )
    return {
        "cases": results,
        "summary": {
            "total": len(results),
            "lexical_hits": sum(rank is not None for rank in lexical_ranks),
            "hybrid_hits": sum(rank is not None for rank in baseline_ranks),
            "improved": sum(bool(item["improved"]) for item in results),
            "lexical_mrr": _mrr(lexical_ranks),
            "hybrid_mrr": _mrr(baseline_ranks),
            "reranker": {
                "semantic_enabled": semantic_enabled,
                "backend_present": reranker_backend_present,
                "status": reranker_status,
                "comparison_valid": reranker_comparison_valid,
                "guidance": reranker_guidance,
                "applicable_cases": reranker_applicable_cases,
                "applied_cases": reranker_applied_cases,
                "scored_results": sum(int(item["reranked_result_count"]) for item in results),
                "returned_results": sum(int(item["returned_result_count"]) for item in results),
                "baseline_hits": sum(rank is not None for rank in baseline_ranks),
                "reranked_hits": (
                    sum(rank is not None for rank in reranked_ranks)
                    if reranker_comparison_valid
                    else None
                ),
                "improved": (
                    sum(bool(item["reranker_improved"]) for item in results)
                    if reranker_comparison_valid
                    else None
                ),
                "regressed": (
                    sum(bool(item["reranker_regressed"]) for item in results)
                    if reranker_comparison_valid
                    else None
                ),
                "baseline_mrr": _mrr(baseline_ranks),
                "reranked_mrr": (_mrr(reranked_ranks) if reranker_comparison_valid else None),
                "baseline_recall_at_k": _mean_metric(results, "baseline_recall_at_k"),
                "reranked_recall_at_k": (
                    _mean_metric(results, "reranked_recall_at_k")
                    if reranker_comparison_valid
                    else None
                ),
                "baseline_ndcg_at_k": _mean_metric(results, "baseline_ndcg_at_k"),
                "reranked_ndcg_at_k": (
                    _mean_metric(results, "reranked_ndcg_at_k")
                    if reranker_comparison_valid
                    else None
                ),
            },
            "latency_ms": {
                mode: (
                    {
                        "p50": _percentile(
                            [float(item["latency_ms"][mode]) for item in results],
                            0.50,
                        ),
                        "p95": _percentile(
                            [float(item["latency_ms"][mode]) for item in results],
                            0.95,
                        ),
                    }
                    if mode in measured_modes
                    else {"p50": None, "p95": None}
                )
                for mode in ("lexical", "baseline", "reranked")
            },
            "measurement": {
                "warmup": bool(cases),
                "warmup_searches": len(measured_modes) if cases else 0,
                "order_strategy": "rotating",
                "modes": list(measured_modes),
            },
        },
    }


def _timed_mode_search(
    timer: Callable[[], float],
    engine: SearchEngine,
    request: SearchRequest,
    *,
    mode: str,
    semantic_enabled: bool,
) -> tuple[SearchResponse, float]:
    started = timer()
    response = _search_mode(
        engine,
        request,
        mode=mode,
        semantic_enabled=semantic_enabled,
    )
    return response, round(max(0.0, timer() - started) * 1000, 3)


def _search_mode(
    engine: SearchEngine,
    request: SearchRequest,
    *,
    mode: str,
    semantic_enabled: bool,
) -> SearchResponse:
    if mode == "lexical":
        return _search_with_modes(
            engine,
            request,
            semantic_enabled=False,
            reranker_enabled=False,
        )
    if mode == "baseline":
        return _search_with_modes(
            engine,
            request,
            semantic_enabled=semantic_enabled,
            reranker_enabled=False,
        )
    if mode == "reranked":
        return _search_with_modes(
            engine,
            request,
            semantic_enabled=semantic_enabled,
            reranker_enabled=True,
        )
    raise ValueError(f"unknown evaluation mode: {mode}")


def _search_with_modes(
    engine: SearchEngine,
    request: SearchRequest,
    *,
    semantic_enabled: bool,
    reranker_enabled: bool,
) -> SearchResponse:
    semantic = engine.config.retrieval.semantic
    reranker = getattr(engine.config.retrieval, "reranker", None)
    previous_semantic = semantic.enabled
    previous_reranker = reranker.enabled if reranker is not None else None
    semantic.enabled = semantic_enabled
    if reranker is not None:
        reranker.enabled = reranker_enabled
    try:
        return engine.search(request, reranker_enabled=reranker_enabled)
    finally:
        semantic.enabled = previous_semantic
        if reranker is not None:
            reranker.enabled = previous_reranker


def _expected_rank(response: SearchResponse, case: EvaluationCase) -> int | None:
    for rank, result in enumerate(response.results, start=1):
        if _matches_expected(result, case):
            return rank
    return None


def _reranked_result_count(response: SearchResponse) -> int:
    return sum(
        bool(getattr(getattr(result, "match", None), "reranked", False))
        for result in response.results
    )


def _matches_expected(result: SearchResult, case: EvaluationCase) -> bool:
    document_id = result.document_id
    if document_id in case.expected_document_ids:
        return True
    haystack = "\n".join(
        [
            result.title,
            *result.heading_path,
            result.text,
        ]
    ).casefold()
    return bool(case.expected_terms) and all(term in haystack for term in case.expected_terms)


def _recall_at_k(response: SearchResponse, case: EvaluationCase) -> float:
    if case.expected_document_ids:
        found = {
            result.document_id
            for result in response.results
            if result.document_id in case.expected_document_ids
        }
        return round(len(found) / len(case.expected_document_ids), 4)
    return 1.0 if any(_matches_expected(result, case) for result in response.results) else 0.0


def _ndcg_at_k(response: SearchResponse, case: EvaluationCase) -> float:
    seen_documents: set[str] = set()
    relevance: list[int] = []
    for result in response.results:
        if case.expected_document_ids:
            relevant = result.document_id in case.expected_document_ids
        else:
            relevant = _matches_expected(result, case)
        if relevant and case.expected_document_ids:
            relevant = result.document_id not in seen_documents
            seen_documents.add(result.document_id)
        elif relevant and not case.expected_document_ids:
            # Term evidence represents one relevant answer, not every duplicate chunk.
            relevant = not seen_documents
            seen_documents.add("__term_evidence__")
        relevance.append(1 if relevant else 0)
    dcg = sum(value / math.log2(rank + 1) for rank, value in enumerate(relevance, start=1))
    relevant_count = len(case.expected_document_ids) if case.expected_document_ids else 1
    ideal_hits = min(relevant_count, len(response.results))
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return round(dcg / ideal_dcg, 4) if ideal_dcg else 0.0


def _mean_metric(results: list[dict[str, object]], key: str) -> float:
    values = [float(item[key]) for item in results]
    return round(sum(values) / len(values), 4) if values else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _mrr(ranks: list[object]) -> float:
    values = [1.0 / rank if isinstance(rank, int) and rank > 0 else 0.0 for rank in ranks]
    return round(sum(values) / len(values), 4) if values else 0.0
