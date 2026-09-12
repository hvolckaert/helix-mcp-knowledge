"""Repeatable lexical-versus-hybrid retrieval evaluation."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .errors import ConfigurationError
from .models.search import SearchRequest, SearchResponse, SearchResult
from .retrieval.search_engine import SearchEngine

_EVALUATION_MODES = ("lexical", "baseline", "reranked")
_EVALUATION_SCHEMA_VERSION = 1
_DIFFICULTIES = {"easy", "medium", "hard"}
_CASE_METADATA_FIELDS = {
    "case_id",
    "name",
    "category",
    "difficulty",
    "language",
    "expected_document_ids",
    "expected_terms",
}


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    name: str
    request: SearchRequest
    expected_document_ids: set[str]
    expected_terms: list[str]
    category: str = "uncategorized"
    difficulty: str = "unspecified"
    language: str = "und"


@dataclass(frozen=True)
class EvaluationDataset:
    """Frozen evaluation cases plus provenance for reproducible reports."""

    name: str
    description: str
    schema_version: int
    sha256: str
    source: Path
    cases: list[EvaluationCase]


def load_evaluation_dataset(path: str | Path, *, top_k: int) -> EvaluationDataset:
    source = Path(path).expanduser().resolve()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"could not read evaluation dataset {source}: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"evaluation dataset is not UTF-8: {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"evaluation dataset is not valid JSON: {source}: {exc}") from exc

    if isinstance(payload, list):
        schema_version = _EVALUATION_SCHEMA_VERSION
        name = source.stem
        description = "Legacy list-format evaluation dataset"
        case_payload = payload
    elif isinstance(payload, dict):
        schema_version = payload.get("schema_version")
        if schema_version != _EVALUATION_SCHEMA_VERSION:
            raise ConfigurationError(
                "evaluation dataset schema_version must be "
                f"{_EVALUATION_SCHEMA_VERSION}, got {schema_version!r}"
            )
        name = _required_text(payload.get("name"), "evaluation dataset name")
        description_value = payload.get("description", "")
        if not isinstance(description_value, str):
            raise ConfigurationError("evaluation dataset description must be a string")
        description = description_value.strip()
        case_payload = payload.get("cases")
    else:
        raise ConfigurationError("evaluation dataset must be a JSON object or list")

    if not isinstance(case_payload, list) or not case_payload:
        raise ConfigurationError("evaluation dataset cases must be a non-empty list")

    cases = []
    seen_case_ids: set[str] = set()
    for index, item in enumerate(case_payload, start=1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"evaluation case {index} must be an object")
        expected_ids = item.get("expected_document_ids", [])
        expected_terms = item.get("expected_terms", [])
        if not isinstance(expected_ids, list) or not all(
            isinstance(value, str) for value in expected_ids
        ):
            raise ConfigurationError(f"evaluation case {index} has invalid expected_document_ids")
        normalized_ids = {value.strip() for value in expected_ids}
        if "" in normalized_ids:
            raise ConfigurationError(
                f"evaluation case {index} has an empty expected_document_ids value"
            )
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
        if not normalized_ids and not normalized_terms:
            raise ConfigurationError(f"evaluation case {index} requires expected evidence")
        if normalized_ids and normalized_terms:
            raise ConfigurationError(
                f"evaluation case {index} must use expected_document_ids "
                "or expected_terms, not both"
            )
        case_id = _required_text(item.get("case_id", f"case-{index}"), f"case {index} case_id")
        if case_id in seen_case_ids:
            raise ConfigurationError(f"evaluation case_id must be unique: {case_id}")
        seen_case_ids.add(case_id)
        name_value = item.get("name", case_id)
        name_value = _required_text(name_value, f"evaluation case {index} name")
        category = _optional_label(item.get("category"), default="uncategorized", field="category")
        difficulty = _optional_label(
            item.get("difficulty"), default="unspecified", field="difficulty"
        )
        if difficulty != "unspecified" and difficulty not in _DIFFICULTIES:
            raise ConfigurationError(
                f"evaluation case {index} difficulty must be easy, medium, or hard"
            )
        language = _optional_label(item.get("language"), default="und", field="language")
        request_payload = {
            key: value for key, value in item.items() if key not in _CASE_METADATA_FIELDS
        }
        request_payload["top_k"] = top_k
        cases.append(
            EvaluationCase(
                case_id=case_id,
                name=name_value,
                request=SearchRequest.model_validate(request_payload),
                expected_document_ids=normalized_ids,
                expected_terms=normalized_terms,
                category=category,
                difficulty=difficulty,
                language=language,
            )
        )
    return EvaluationDataset(
        name=name,
        description=description,
        schema_version=schema_version,
        sha256=sha256(raw).hexdigest(),
        source=source,
        cases=cases,
    )


def load_evaluation_cases(path: str | Path, *, top_k: int) -> list[EvaluationCase]:
    """Load cases while preserving the original public API."""

    return load_evaluation_dataset(path, top_k=top_k).cases


def evaluate_retrieval(
    engine: SearchEngine,
    cases: list[EvaluationCase] | EvaluationDataset,
    *,
    timer: Callable[[], float] | None = None,
) -> dict[str, object]:
    """Compare lexical, configured baseline, and optional reranked retrieval.

    Existing lexical/hybrid fields are retained for callers of the original evaluator.
    The baseline/reranked comparison is independent of whether semantic retrieval is
    enabled, which permits lightweight reranker evaluation without a vector backend.
    """

    dataset = cases if isinstance(cases, EvaluationDataset) else None
    if dataset is not None:
        cases = dataset.cases
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
                "case_id": case.case_id,
                "name": case.name,
                "query": case.request.query,
                "category": case.category,
                "difficulty": case.difficulty,
                "language": case.language,
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
                "lexical_recall_at_k": _recall_at_k(lexical, case),
                "lexical_ndcg_at_k": _ndcg_at_k(lexical, case),
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
    quality = {
        "lexical": _quality_metrics(results, "lexical"),
        "baseline": _quality_metrics(results, "baseline"),
        "reranked": (_quality_metrics(results, "reranked") if reranker_comparison_valid else None),
    }
    report: dict[str, object] = {
        "cases": results,
        "summary": {
            "total": len(results),
            "top_k": cases[0].request.top_k if cases else None,
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
            "quality": quality,
            "slices": {
                field: _slice_metrics(
                    results,
                    field,
                    reranker_comparison_valid=reranker_comparison_valid,
                )
                for field in ("category", "difficulty", "language")
            },
        },
    }
    if dataset is not None:
        report["dataset"] = {
            "name": dataset.name,
            "description": dataset.description,
            "schema_version": dataset.schema_version,
            "sha256": dataset.sha256,
            "source": dataset.source.name,
        }
    return report


def render_evaluation_markdown(report: dict[str, object]) -> str:
    """Render a compact, shareable report without retrieved source content."""

    summary = report["summary"]
    assert isinstance(summary, dict)
    dataset = report.get("dataset")
    dataset_name = "ad hoc evaluation"
    lines: list[str] = []
    if isinstance(dataset, dict):
        dataset_name = str(dataset["name"])
    lines.extend([f"# Retrieval evaluation: {_markdown_cell(dataset_name)}", ""])
    if isinstance(dataset, dict):
        description = str(dataset.get("description") or "")
        if description:
            lines.extend([description, ""])
        lines.extend(
            [
                f"- Dataset SHA-256: `{dataset['sha256']}`",
                f"- Schema version: `{dataset['schema_version']}`",
                f"- Source file: `{dataset['source']}`",
            ]
        )
    lines.extend(
        [
            f"- Cases: `{summary['total']}`",
            f"- Top-k: `{summary['top_k']}`",
            "",
            "## Aggregate quality",
            "",
            "| Mode | Hits | Hit rate@k | MRR | Recall@k | nDCG@k | p50 ms | p95 ms |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    quality = summary["quality"]
    latency = summary["latency_ms"]
    assert isinstance(quality, dict)
    assert isinstance(latency, dict)
    for mode in _EVALUATION_MODES:
        metrics = quality.get(mode)
        timing = latency[mode]
        assert isinstance(timing, dict)
        if not isinstance(metrics, dict):
            cells = [mode, "n/a", "n/a", "n/a", "n/a", "n/a"]
        else:
            cells = [
                mode,
                str(metrics["hits"]),
                _format_metric(metrics["hit_rate_at_k"]),
                _format_metric(metrics["mrr"]),
                _format_metric(metrics["recall_at_k"]),
                _format_metric(metrics["ndcg_at_k"]),
            ]
        cells.extend([_format_metric(timing["p50"]), _format_metric(timing["p95"])])
        lines.append("| " + " | ".join(cells) + " |")

    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | Category | Difficulty | Language | Lexical rank | "
            "Baseline rank | Reranked rank |",
            "|---|---|---|---|---:|---:|---:|",
        ]
    )
    cases = report["cases"]
    assert isinstance(cases, list)
    for case in cases:
        assert isinstance(case, dict)
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(str(case["case_id"])),
                    _markdown_cell(str(case["category"])),
                    _markdown_cell(str(case["difficulty"])),
                    _markdown_cell(str(case["language"])),
                    _format_rank(case["lexical_rank"]),
                    _format_rank(case["baseline_rank"]),
                    _format_rank(case["reranked_rank"]),
                ]
            )
            + " |"
        )

    reranker = summary["reranker"]
    assert isinstance(reranker, dict)
    runtime = report.get("runtime")
    if isinstance(runtime, dict):
        index = runtime.get("index")
        retrieval = runtime.get("retrieval")
        lines.extend(["", "## Runtime", ""])
        lines.append(f"- Helix MCP Knowledge: `{runtime['helix_mcp_knowledge_version']}`")
        if isinstance(index, dict):
            counts = index.get("counts")
            lines.append(f"- SQLite schema: `{index['schema_version']}`")
            lines.append(f"- SQLite bytes: `{index.get('sqlite_bytes', 'n/a')}`")
            if isinstance(counts, dict):
                lines.append(
                    f"- Indexed documents/chunks: `{counts.get('documents', 'n/a')}` / "
                    f"`{counts.get('chunks', 'n/a')}`"
                )
        if isinstance(retrieval, dict):
            lines.append(
                "- Retrieval enabled (lexical/semantic/reranker): "
                f"`{retrieval.get('lexical_enabled')}` / "
                f"`{retrieval.get('semantic_enabled')}` / "
                f"`{retrieval.get('reranker_enabled')}`"
            )
    lines.extend(["", "## Measurement notes", ""])
    lines.append(f"- Reranker status: `{reranker['status']}`")
    guidance = reranker.get("guidance")
    if guidance:
        lines.append(f"- Reranker guidance: {_markdown_cell(str(guidance))}")
    lines.append(
        "- Every mode is warmed once and timed in rotating order; retrieved text is not "
        "included in this report."
    )
    return "\n".join(lines) + "\n"


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


def _quality_metrics(results: list[dict[str, object]], mode: str) -> dict[str, object]:
    rank_key = f"{mode}_rank"
    ranks = [item[rank_key] for item in results]
    return {
        "hits": sum(rank is not None for rank in ranks),
        "hit_rate_at_k": round(sum(rank is not None for rank in ranks) / len(results), 4)
        if results
        else 0.0,
        "mrr": _mrr(ranks),
        "recall_at_k": _mean_metric(results, f"{mode}_recall_at_k"),
        "ndcg_at_k": _mean_metric(results, f"{mode}_ndcg_at_k"),
    }


def _slice_metrics(
    results: list[dict[str, object]],
    field: str,
    *,
    reranker_comparison_valid: bool,
) -> dict[str, object]:
    slices: dict[str, object] = {}
    for value in sorted({str(item[field]) for item in results}):
        selected = [item for item in results if str(item[field]) == value]
        slices[value] = {
            "total": len(selected),
            "quality": {
                "lexical": _quality_metrics(selected, "lexical"),
                "baseline": _quality_metrics(selected, "baseline"),
                "reranked": (
                    _quality_metrics(selected, "reranked") if reranker_comparison_valid else None
                ),
            },
        }
    return slices


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_label(value: object, *, default: str, field: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"evaluation case {field} must be a non-empty string")
    return value.strip().casefold()


def _format_metric(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _format_rank(value: object) -> str:
    return str(value) if isinstance(value, int) else "—"


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
