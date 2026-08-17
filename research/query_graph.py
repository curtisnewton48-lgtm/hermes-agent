"""Public facade for Hermes Query Graph v1.

The implementation is split by responsibility so callers retain one stable
import surface while domain types, graph/question lifecycle, claim integration,
closure, and run-wide DAG rules remain independently testable.
"""

from .query_graph_types import *
from .query_graph_claims import QueryGraphClaimMixin
from .query_graph_closure import QueryGraphClosureMixin
from .query_graph_dependencies import QueryGraphDependencyMixin
from .query_graph_service import QueryGraphService as _CoreQueryGraphService


class QueryGraphService(
    QueryGraphClosureMixin,
    QueryGraphClaimMixin,
    QueryGraphDependencyMixin,
    _CoreQueryGraphService,
):
    """Composed Query Graph v1 service exposed to runtime callers."""


__all__ = [name for name in globals() if not name.startswith("_")]
