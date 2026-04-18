"""Resume logic.

The orchestrator iterates through nodes in dependency order. This
module decides which nodes still need work, given their persisted
``status``. Reruns are cheap because nothing already at
``Status.COMPLETE`` re-executes unless ``force=True`` or its
extraction fingerprint changed.
"""

from __future__ import annotations

from autocoder.models import Registry, Status, URLNode

_FINAL_STATUSES = {Status.COMPLETE, Status.FAILED}


def next_actionable_nodes(
    registry: Registry,
    *,
    force_urls: set[str] | None = None,
) -> list[URLNode]:
    force_urls = force_urls or set()
    actionable: list[URLNode] = []
    for url, node in registry.nodes.items():
        if url in force_urls:
            actionable.append(node)
            continue
        if node.status in _FINAL_STATUSES and node.status == Status.COMPLETE:
            continue
        if node.status == Status.FAILED:
            actionable.append(node)
            continue
        actionable.append(node)
    return actionable


def auth_needs_setup(registry: Registry) -> bool:
    if registry.auth is None:
        return False
    return registry.auth.status != Status.COMPLETE
