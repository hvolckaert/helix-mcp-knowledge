"""Deterministic Reciprocal Rank Fusion over chunk identifiers."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    chunk_id: str
    lexical: bool
    semantic: bool
    fused_score: float


def reciprocal_rank(rank: int, *, rrf_k: int = 60) -> float:
    return 1.0 / (rrf_k + rank)


def fuse_rankings(
    lexical_ids: list[str],
    semantic_ids: list[str],
    *,
    exact_match_ids: set[str] | None = None,
    rrf_k: int = 60,
) -> list[FusedCandidate]:
    exact_match_ids = exact_match_ids or set()
    scores: dict[str, float] = {}
    for rank, chunk_id in enumerate(lexical_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + reciprocal_rank(rank, rrf_k=rrf_k)
    for rank, chunk_id in enumerate(semantic_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + reciprocal_rank(rank, rrf_k=rrf_k)
    exact_bonus = reciprocal_rank(1, rrf_k=rrf_k)
    for chunk_id in exact_match_ids:
        if chunk_id in scores:
            scores[chunk_id] += exact_bonus
    lexical_set = set(lexical_ids)
    semantic_set = set(semantic_ids)
    return [
        FusedCandidate(
            chunk_id=chunk_id,
            lexical=chunk_id in lexical_set,
            semantic=chunk_id in semantic_set,
            fused_score=score,
        )
        for chunk_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]
