"""Lazy BGE-M3 dense embedding adapter."""

from typing import Protocol

from ..errors import SemanticBackendError


class Embedder(Protocol):
    dimension: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        batch_size: int,
        device: str | None,
        normalize: bool,
        local_files_only: bool = False,
        max_seq_length: int | None = None,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise SemanticBackendError(
                "semantic dependencies are missing; install the optional semantic component "
                "from the dashboard"
            ) from exc
        self.model = SentenceTransformer(
            model_name,
            device=device,
            local_files_only=local_files_only,
            trust_remote_code=False,
        )
        self.dimension = dimension
        self.batch_size = batch_size
        self.normalize = normalize
        if max_seq_length is not None:
            self.model.max_seq_length = max_seq_length

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize,
            show_progress_bar=False,
        )
        output = vectors.tolist()
        if output and len(output[0]) != self.dimension:
            raise SemanticBackendError(
                f"embedding dimension mismatch: configured {self.dimension}, got {len(output[0])}"
            )
        return output
