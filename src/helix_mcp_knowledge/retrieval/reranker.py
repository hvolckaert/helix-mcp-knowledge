"""Transport-neutral contracts and deterministic ordering for optional reranking."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .fusion import FusedCandidate

RERANKER_MAX_CANDIDATES = 32


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """A bounded, already-authorized candidate sent to a local reranker."""

    chunk_id: str
    title: str
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class RerankScore:
    """A relevance score returned for one submitted chunk."""

    chunk_id: str
    score: float


@runtime_checkable
class Reranker(Protocol):
    """Minimal interface implemented by an in-process or service-backed reranker."""

    def rerank(
        self,
        *,
        query: str,
        candidates: Sequence[RerankCandidate],
    ) -> Sequence[RerankScore]: ...


@dataclass(frozen=True, slots=True)
class RerankerRuntime:
    """One immutable reranker configuration snapshot used by a search call."""

    enabled: bool
    candidates: int
    backend: Reranker | None


@runtime_checkable
class RerankerRuntimeProvider(Protocol):
    """Resolve the current optional backend without coupling retrieval to its lifecycle."""

    def __call__(self) -> RerankerRuntime: ...


class InvalidRerankerResponse(ValueError):
    """Raised internally when an optional backend violates its response contract."""


def apply_reranker_scores(
    candidates: Sequence[FusedCandidate],
    scores: Sequence[RerankScore],
    *,
    exact_terms_by_id: Mapping[str, Sequence[str]],
    candidate_limit: int,
) -> tuple[list[FusedCandidate], set[str]]:
    """Fuse baseline and reranker ranks equally for an authorized prefix.

    Model score magnitudes are deliberately ignored after deriving the model rank:
    the final order gives the baseline and reranker ranks equal weight. Exact
    technical matches remain in a protected tier and equal blended ranks keep the
    baseline order.

    The unsubmitted tail is never changed. For a partial response, model ranks are
    projected onto the baseline slots occupied by scored candidates. This prevents
    a singleton or small partial response from receiving an artificial top rank;
    unscored candidates retain their baseline rank and relative order.
    """

    original = list(candidates)
    if not original or candidate_limit <= 0:
        return original, set()
    prefix = original[:candidate_limit]
    tail = original[candidate_limit:]
    known_ids = {candidate.chunk_id for candidate in prefix}
    score_by_id: dict[str, float] = {}
    for result in scores:
        chunk_id = result.chunk_id
        if not isinstance(chunk_id, str) or chunk_id not in known_ids:
            raise InvalidRerankerResponse("reranker returned an unknown chunk identifier")
        if chunk_id in score_by_id:
            raise InvalidRerankerResponse("reranker returned a duplicate chunk identifier")
        if isinstance(result.score, bool):
            raise InvalidRerankerResponse("reranker returned a non-numeric score")
        try:
            score = float(result.score)
        except (TypeError, ValueError) as exc:
            raise InvalidRerankerResponse("reranker returned a non-numeric score") from exc
        if not math.isfinite(score):
            raise InvalidRerankerResponse("reranker returned a non-finite score")
        score_by_id[chunk_id] = score

    if not score_by_id:
        return original, set()

    positions = {candidate.chunk_id: position for position, candidate in enumerate(prefix)}
    scored_slots = sorted(positions[chunk_id] for chunk_id in score_by_id)
    model_order = sorted(
        score_by_id,
        key=lambda chunk_id: (-score_by_id[chunk_id], positions[chunk_id]),
    )
    model_positions = dict(zip(model_order, scored_slots, strict=True))

    def order_key(candidate: FusedCandidate) -> tuple[int, int, int]:
        chunk_id = candidate.chunk_id
        exact_priority = 0 if exact_terms_by_id.get(chunk_id) else 1
        baseline_position = positions[chunk_id]
        model_position = model_positions.get(chunk_id, baseline_position)
        # Dividing by two would not change ordering. Keeping the integer rank sum
        # makes the 50/50 blend exact and deterministic on every supported runtime.
        blended_rank = baseline_position + model_position
        return (exact_priority, blended_rank, baseline_position)

    return sorted(prefix, key=order_key) + tail, set(score_by_id)
