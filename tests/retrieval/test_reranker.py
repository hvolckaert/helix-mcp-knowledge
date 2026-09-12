from __future__ import annotations

import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import yaml

import helix_mcp_knowledge.retrieval.search_engine as search_engine_module
from helix_mcp_knowledge.application import _LiveRerankerRuntimeProvider
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.models.search import SearchRequest
from helix_mcp_knowledge.reranker_client import RERANKER_COMPONENT_VERSION
from helix_mcp_knowledge.reranker_install_worker import RERANKER_INSTALL_JOB
from helix_mcp_knowledge.retrieval.fusion import FusedCandidate
from helix_mcp_knowledge.retrieval.reranker import (
    RERANKER_MAX_CANDIDATES,
    InvalidRerankerResponse,
    RerankerRuntime,
    RerankScore,
    apply_reranker_scores,
)
from helix_mcp_knowledge.retrieval.search_engine import SearchEngine
from helix_mcp_knowledge.storage.automation import AutomationStore


def test_reranker_contract_import_does_not_load_server_model_stack() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import helix_mcp_knowledge.retrieval.reranker; "
                "assert 'helix_mcp_knowledge.models' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _candidate(chunk_id: str, score: float) -> FusedCandidate:
    return FusedCandidate(
        chunk_id=chunk_id,
        lexical=True,
        semantic=False,
        fused_score=score,
    )


def test_scores_preserve_exact_priority_stable_ties_and_unsubmitted_tail() -> None:
    baseline = [
        _candidate("exact", 4.0),
        _candidate("lower", 3.0),
        _candidate("higher", 2.0),
        _candidate("tie", 1.5),
        _candidate("tail", 1.0),
    ]

    reranked, scored = apply_reranker_scores(
        baseline,
        [
            RerankScore(chunk_id="exact", score=-100.0),
            RerankScore(chunk_id="lower", score=5.0),
            RerankScore(chunk_id="higher", score=10.0),
            RerankScore(chunk_id="tie", score=10.0),
        ],
        exact_terms_by_id={"exact": ["BMC_ComputerSystem"]},
        candidate_limit=4,
    )

    assert [candidate.chunk_id for candidate in reranked] == [
        "exact",
        "higher",
        "lower",
        "tie",
        "tail",
    ]
    assert scored == {"exact", "lower", "higher", "tie"}


def test_partial_single_score_does_not_gain_an_artificial_top_rank() -> None:
    baseline = [_candidate("one", 3.0), _candidate("two", 2.0), _candidate("three", 1.0)]

    reranked, scored = apply_reranker_scores(
        baseline,
        [RerankScore(chunk_id="two", score=1.0)],
        exact_terms_by_id={},
        candidate_limit=3,
    )

    assert [candidate.chunk_id for candidate in reranked] == ["one", "two", "three"]
    assert scored == {"two"}


def test_rank_blend_limits_a_model_reversal_and_ignores_score_magnitude() -> None:
    baseline = [
        _candidate("one", 3.0),
        _candidate("two", 2.0),
        _candidate("three", 1.0),
    ]

    modest, _ = apply_reranker_scores(
        baseline,
        [
            RerankScore(chunk_id="one", score=0.2),
            RerankScore(chunk_id="two", score=0.1),
            RerankScore(chunk_id="three", score=0.3),
        ],
        exact_terms_by_id={},
        candidate_limit=3,
    )
    extreme, _ = apply_reranker_scores(
        baseline,
        [
            RerankScore(chunk_id="one", score=-1_000_000.0),
            RerankScore(chunk_id="two", score=-2_000_000.0),
            RerankScore(chunk_id="three", score=1_000_000.0),
        ],
        exact_terms_by_id={},
        candidate_limit=3,
    )

    assert [candidate.chunk_id for candidate in modest] == ["one", "three", "two"]
    assert [candidate.chunk_id for candidate in extreme] == ["one", "three", "two"]


@pytest.mark.parametrize(
    "scores",
    [
        [RerankScore(chunk_id="unknown", score=1.0)],
        [RerankScore(chunk_id="one", score=1.0), RerankScore(chunk_id="one", score=0.0)],
        [RerankScore(chunk_id="one", score=float("nan"))],
        [RerankScore(chunk_id="one", score=float("inf"))],
        [RerankScore(chunk_id="one", score=True)],
    ],
)
def test_invalid_scores_are_rejected(scores: list[RerankScore]) -> None:
    with pytest.raises(InvalidRerankerResponse):
        apply_reranker_scores(
            [_candidate("one", 1.0)],
            scores,
            exact_terms_by_id={},
            candidate_limit=1,
        )


class PreferProjectReranker:
    def rerank(self, *, query, candidates):
        return [
            RerankScore(
                chunk_id=candidate.chunk_id,
                score=10.0 if candidate.chunk_id == "chk_example_project" else -10.0,
            )
            for candidate in candidates
        ]


class FailingReranker:
    def rerank(self, *, query, candidates):
        raise RuntimeError(f"private payload: {query} {candidates[0].text}")


class InvalidReranker:
    def rerank(self, *, query, candidates):
        return [RerankScore(chunk_id="not-submitted", score=100.0)]


class CapturingReranker:
    def __init__(self) -> None:
        self.candidate_counts: list[int] = []

    def rerank(self, *, query, candidates):
        self.candidate_counts.append(len(candidates))
        return [RerankScore(chunk_id=candidate.chunk_id, score=0.0) for candidate in candidates]


def _reranking_engine(app, reranker) -> SearchEngine:
    app.config.retrieval.reranker.enabled = True
    return SearchEngine(
        app.database,
        app.config,
        app.catalog,
        app.project_context,
        reranker=reranker,
    )


def _set_persisted_reranker_enabled(config_path, enabled: bool) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["enabled"] = enabled
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_search_blends_after_scope_revalidation_and_marks_scored_results(app) -> None:
    engine = _reranking_engine(app, PreferProjectReranker())

    response = engine.search(SearchRequest(query="reconciliation", project_id="example_project"))

    # A two-item reversal is an exact 50/50 rank tie, resolved by baseline order.
    assert [result.chunk_id for result in response.results] == [
        "chk_official",
        "chk_example_project",
    ]
    assert all(result.match.reranked for result in response.results)
    assert "chk_atlas" not in {result.chunk_id for result in response.results}


def test_search_does_not_allow_reranker_to_demote_exact_technical_evidence(app) -> None:
    engine = _reranking_engine(app, PreferProjectReranker())

    response = engine.search(
        SearchRequest(query="BMC_ComputerSystem reconciliation", project_id="example_project")
    )

    assert [result.chunk_id for result in response.results] == [
        "chk_official",
        "chk_example_project",
    ]
    assert response.results[0].match.exact_terms == ["BMC_ComputerSystem"]


def test_disabled_exact_match_does_not_protect_candidates_from_reranker(
    app,
    monkeypatch,
) -> None:
    observed_exact_terms: list[dict[str, object]] = []
    original = search_engine_module.apply_reranker_scores

    def capture_exact_terms(candidates, scores, *, exact_terms_by_id, candidate_limit):
        observed_exact_terms.append(dict(exact_terms_by_id))
        return original(
            candidates,
            scores,
            exact_terms_by_id=exact_terms_by_id,
            candidate_limit=candidate_limit,
        )

    monkeypatch.setattr(search_engine_module, "apply_reranker_scores", capture_exact_terms)
    app.config.retrieval.exact_match.enabled = False
    engine = _reranking_engine(app, PreferProjectReranker())

    engine.search(
        SearchRequest(query="BMC_ComputerSystem reconciliation", project_id="example_project")
    )

    assert observed_exact_terms == [{}]


def test_search_scores_at_least_top_k_when_candidate_setting_is_lower(app) -> None:
    reranker = CapturingReranker()
    app.config.retrieval.reranker.candidates = 1
    engine = _reranking_engine(app, reranker)

    response = engine.search(
        SearchRequest(query="reconciliation", project_id="example_project", top_k=2)
    )

    assert reranker.candidate_counts == [2]
    assert len(response.results) == 2
    assert all(result.match.reranked for result in response.results)


def test_candidate_retrieval_ignores_disabled_and_caps_enabled_reranker_setting(app) -> None:
    limits: list[int] = []
    engine = app.search_engine
    engine.reranker_runtime_provider = None
    engine._lexical_candidates = (  # type: ignore[method-assign]
        lambda _query, _where, _parameters, limit: limits.append(limit) or []
    )
    app.config.retrieval.reranker.candidates = 1_000_000

    engine.search(SearchRequest(query="reconciliation", top_k=2))
    app.config.retrieval.reranker.enabled = True
    engine.reranker = CapturingReranker()
    engine.search(SearchRequest(query="reconciliation", top_k=2))

    assert limits == [8, RERANKER_MAX_CANDIDATES]


def test_live_runtime_clamps_a_legacy_candidate_pool_without_breaking_search(app) -> None:
    app.search_engine.reranker_runtime_provider = lambda: RerankerRuntime(
        enabled=False,
        candidates=33,
        backend=None,
    )

    runtime = app.search_engine._resolve_reranker_runtime()

    assert runtime.enabled is False
    assert runtime.candidates == RERANKER_MAX_CANDIDATES


def test_reranker_candidate_payload_is_bounded_and_tail_is_preserved(app) -> None:
    reranker = CapturingReranker()
    engine = _reranking_engine(app, reranker)
    candidates = [_candidate(f"chunk-{index:02d}", 100.0 - index) for index in range(40)]
    rows = {
        candidate.chunk_id: {
            "title": "Title",
            "heading_path_json": "[]",
            "text": "Evidence",
        }
        for candidate in candidates
    }

    reranked, scored = engine._rerank_candidates(
        "bounded query",
        candidates,
        rows,
        {},
        top_k=40,
    )

    assert reranker.candidate_counts == [RERANKER_MAX_CANDIDATES]
    assert [candidate.chunk_id for candidate in reranked] == [
        candidate.chunk_id for candidate in candidates
    ]
    assert scored == {f"chunk-{index:02d}" for index in range(RERANKER_MAX_CANDIDATES)}


def test_backend_failure_preserves_baseline_and_does_not_log_content(app, caplog) -> None:
    baseline = app.search_engine.search(
        SearchRequest(query="reconciliation", project_id="example_project")
    )
    engine = _reranking_engine(app, FailingReranker())

    with caplog.at_level(logging.WARNING):
        response = engine.search(
            SearchRequest(query="reconciliation", project_id="example_project")
        )

    assert response.model_dump() == baseline.model_dump()
    assert "reconciliation" not in caplog.text
    assert "Official BMC" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_invalid_backend_response_preserves_baseline_and_reranked_flags(app) -> None:
    baseline = app.search_engine.search(
        SearchRequest(query="reconciliation", project_id="example_project")
    )
    response = _reranking_engine(app, InvalidReranker()).search(
        SearchRequest(query="reconciliation", project_id="example_project")
    )

    assert response.model_dump() == baseline.model_dump()
    assert all(not result.match.reranked for result in response.results)


def test_long_lived_session_detects_persisted_enable_and_disable_thread_safely(
    app,
    config_path,
    monkeypatch,
) -> None:
    backend = CapturingReranker()
    service_start_calls: list[None] = []
    service_stop_calls: list[None] = []
    service_started = threading.Event()
    service_stopped = threading.Event()

    class ReadyManager:
        def __init__(self, _config) -> None:
            pass

        def status(self):
            return SimpleNamespace(
                installed=True,
                status="ready",
                component_version=RERANKER_COMPONENT_VERSION,
            )

        def client(self):
            return backend

        def request_service_start(self):
            return None

        def request_service_stop(self):
            return None

        def ensure_service(self):
            service_start_calls.append(None)
            service_started.set()
            return backend

        def stop_service(self):
            service_stop_calls.append(None)
            service_stopped.set()

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        ReadyManager,
    )
    request = SearchRequest(query="reconciliation", project_id="example_project", top_k=2)
    baseline = app.search_engine.search(request)

    _set_persisted_reranker_enabled(config_path, True)
    with ThreadPoolExecutor(max_workers=8) as executor:
        enabled_responses = list(
            executor.map(lambda _index: app.search_engine.search(request), range(8))
        )

    assert service_started.wait(timeout=2)
    assert service_start_calls == [None]
    assert len(backend.candidate_counts) == 8
    assert all(
        all(result.match.reranked for result in response.results) for response in enabled_responses
    )

    _set_persisted_reranker_enabled(config_path, False)
    disabled = app.search_engine.search(request)

    assert disabled.model_dump() == baseline.model_dump()
    assert service_stopped.wait(timeout=2)
    assert service_stop_calls == [None]
    assert AutomationStore(app.database).state(RERANKER_INSTALL_JOB)["desired_enabled"] is False
    assert len(backend.candidate_counts) == 8


def test_invalid_live_config_fails_open_without_logging_search_content(
    app,
    config_path,
    caplog,
) -> None:
    request = SearchRequest(query="reconciliation", project_id="example_project", top_k=2)
    baseline = app.search_engine.search(request)
    config_path.write_text("retrieval: [", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        response = app.search_engine.search(request)

    assert response.model_dump() == baseline.model_dump()
    assert "configuration refresh failed" in caplog.text
    assert "reconciliation" not in caplog.text
    assert "example_project" not in caplog.text


def test_live_search_does_not_cancel_an_install_that_owns_disabled_config(
    app,
    config_path,
    monkeypatch,
) -> None:
    store = AutomationStore(app.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"status": "installing", "desired_enabled": True},
    )
    stop_requests: list[None] = []

    class Manager:
        def __init__(self, _config) -> None:
            pass

        def request_service_stop(self) -> None:
            stop_requests.append(None)

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        Manager,
    )
    provider = _LiveRerankerRuntimeProvider(load_config(config_path), None, store)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["candidates"] = 10
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    runtime = provider()

    assert runtime.enabled is False
    assert runtime.backend is None
    assert stop_requests == []
    assert provider._manager is None


def test_live_provider_recovers_a_dead_service_in_background(
    app,
    config_path,
    monkeypatch,
) -> None:
    request = SearchRequest(query="reconciliation", project_id="example_project", top_k=2)
    baseline = app.search_engine.search(request)
    available = threading.Event()
    recovery_started = threading.Event()
    allow_recovery = threading.Event()

    class RecoveringBackend:
        def rerank(self, *, query, candidates):
            if not available.is_set():
                raise RuntimeError("service stopped")
            return [RerankScore(chunk_id=candidate.chunk_id, score=0.0) for candidate in candidates]

    backend = RecoveringBackend()

    class RecoveringManager:
        def __init__(self, _config) -> None:
            pass

        def request_service_start(self):
            return None

        def ensure_service(self):
            recovery_started.set()
            assert allow_recovery.wait(timeout=2)
            available.set()
            return backend

        def stop_service(self):
            available.clear()

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        RecoveringManager,
    )
    _set_persisted_reranker_enabled(config_path, True)
    provider = _LiveRerankerRuntimeProvider(load_config(config_path), backend)
    provider._next_service_reconcile = 0.0
    app.search_engine.reranker_runtime_provider = provider

    while_service_is_down = app.search_engine.search(request)

    assert while_service_is_down.model_dump() == baseline.model_dump()
    assert recovery_started.wait(timeout=2)
    allow_recovery.set()
    assert available.wait(timeout=2)

    recovered = app.search_engine.search(request)

    assert all(result.match.reranked for result in recovered.results)


def test_obsolete_enable_is_compensated_during_enable_disable_race(
    app,
    config_path,
    monkeypatch,
) -> None:
    request = SearchRequest(query="reconciliation", project_id="example_project", top_k=2)
    baseline = app.search_engine.search(request)
    backend = CapturingReranker()
    ensure_started = threading.Event()
    allow_ensure_to_finish = threading.Event()
    both_stops_finished = threading.Event()
    state_lock = threading.Lock()
    service_active = False
    stop_count = 0

    class RacingManager:
        def __init__(self, _config) -> None:
            pass

        def status(self):
            return SimpleNamespace(
                installed=True,
                status="ready",
                component_version=RERANKER_COMPONENT_VERSION,
            )

        def client(self):
            return backend

        def request_service_start(self):
            return None

        def request_service_stop(self):
            return None

        def ensure_service(self):
            nonlocal service_active
            ensure_started.set()
            assert allow_ensure_to_finish.wait(timeout=2)
            with state_lock:
                service_active = True
            return backend

        def stop_service(self):
            nonlocal service_active, stop_count
            with state_lock:
                service_active = False
                stop_count += 1
                if stop_count == 2:
                    both_stops_finished.set()

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        RacingManager,
    )

    _set_persisted_reranker_enabled(config_path, True)
    app.search_engine.search(request)
    assert ensure_started.wait(timeout=2)

    _set_persisted_reranker_enabled(config_path, False)
    disabled = app.search_engine.search(request)

    # Disabling does not wait for the in-flight start operation.
    assert disabled.model_dump() == baseline.model_dump()
    allow_ensure_to_finish.set()
    assert both_stops_finished.wait(timeout=2)
    with state_lock:
        assert service_active is False
        assert stop_count == 2


def test_new_enable_clears_stop_marker_created_by_obsolete_disable(
    app,
    config_path,
    monkeypatch,
) -> None:
    backend = CapturingReranker()
    stop_started = threading.Event()
    allow_stop = threading.Event()
    service_started = threading.Event()
    state_lock = threading.Lock()
    stop_requested = False
    start_request_count = 0

    class RacingManager:
        def __init__(self, _config) -> None:
            pass

        def client(self):
            return backend

        def request_service_stop(self):
            nonlocal stop_requested
            with state_lock:
                stop_requested = True

        def request_service_start(self):
            nonlocal stop_requested, start_request_count
            with state_lock:
                stop_requested = False
                start_request_count += 1

        def stop_service(self):
            nonlocal stop_requested
            stop_started.set()
            assert allow_stop.wait(timeout=2)
            with state_lock:
                stop_requested = True

        def ensure_service(self):
            with state_lock:
                if stop_requested:
                    raise RuntimeError("startup cancelled by a stale stop marker")
            service_started.set()
            return backend

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        RacingManager,
    )
    provider = _LiveRerankerRuntimeProvider(load_config(config_path), None)

    _set_persisted_reranker_enabled(config_path, False)
    provider()
    assert stop_started.wait(timeout=2)

    _set_persisted_reranker_enabled(config_path, True)
    provider()
    allow_stop.set()

    assert service_started.wait(timeout=2)
    with state_lock:
        assert stop_requested is False
        # One request is emitted while observing config and a second, fenced
        # request is emitted immediately before starting the serialized service.
        assert start_request_count >= 2
