"""Domain errors exposed by the application and MCP tools."""


class KnowledgeError(Exception):
    """Base exception for expected application failures."""


class ConfigurationError(KnowledgeError):
    """The server or project configuration is invalid."""


class ProjectNotFoundError(KnowledgeError):
    """The requested project does not exist."""


class ProjectDisabledError(KnowledgeError):
    """The requested project is disabled for retrieval."""


class SearchValidationError(KnowledgeError):
    """A search request cannot be executed safely."""


class ChunkNotFoundError(KnowledgeError):
    """The requested chunk does not exist or is inactive."""


class IngestionError(KnowledgeError):
    """A source document could not be parsed or indexed safely."""


class SourceSyncError(KnowledgeError):
    """An official source could not be downloaded or synchronized safely."""


class SourceNotFoundError(SourceSyncError):
    """A linked official source no longer exists."""


class OfficialSyncCancelled(SourceSyncError):
    """An official documentation synchronization was cancelled cooperatively."""


class SemanticBackendError(KnowledgeError):
    """The optional embedding or vector backend is unavailable."""


class RerankerBackendError(KnowledgeError):
    """The optional result-reranking backend is unavailable or returned invalid data."""
