"""Read/write the central registry.

The registry stays human-readable (YAML) so a developer can inspect or
hand-edit it. Concurrent writers are not supported — the orchestrator
is single-process by design.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from autocoder import logger
from autocoder.models import (
    AuthSpec,
    PageExtraction,
    Registry,
    URLNode,
)
from autocoder.utils import fingerprint


class RegistryStore:
    def __init__(self, path: Path):
        self.path = path

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def load(self) -> Registry:
        if not self.path.exists():
            logger.debug("registry_load", path=str(self.path), exists=False)
            return Registry()
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        registry = Registry(**raw)
        logger.debug(
            "registry_load",
            path=str(self.path),
            nodes=len(registry.nodes),
            has_auth=registry.auth is not None,
        )
        return registry

    def save(self, registry: Registry) -> None:
        registry.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self.path.write_text(
            yaml.safe_dump(registry.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        logger.debug(
            "registry_save",
            path=str(self.path),
            nodes=len(registry.nodes),
            action="updated" if existed else "created",
        )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def upsert_node(self, registry: Registry, node: URLNode) -> URLNode:
        existing = registry.nodes.get(node.url)
        if existing is None:
            registry.nodes[node.url] = node
            return node
        # Merge: keep status fields from the existing node when the new
        # node is the result of classification (which only sets kind
        # and a few flags). The orchestrator advances status separately.
        merged = existing.model_copy(
            update={
                "kind": node.kind,
                "requires_auth": node.requires_auth or existing.requires_auth,
                "redirects_to": node.redirects_to or existing.redirects_to,
                "depends_on": sorted(set(existing.depends_on) | set(node.depends_on)),
                "notes": list({*existing.notes, *node.notes}),
                "slug": existing.slug or node.slug,
            }
        )
        registry.nodes[node.url] = merged
        return merged

    def set_auth(self, registry: Registry, auth: AuthSpec) -> None:
        registry.auth = auth

    # ------------------------------------------------------------------
    # Per-node payloads
    # ------------------------------------------------------------------

    def write_extraction(
        self,
        extraction: PageExtraction,
        *,
        extractions_dir: Path,
        slug: str,
    ) -> Path:
        extractions_dir.mkdir(parents=True, exist_ok=True)
        path = extractions_dir / f"{slug}.json"
        path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
        return path

    def read_extraction(self, path: Path) -> PageExtraction | None:
        if not path.exists():
            return None
        return PageExtraction.model_validate_json(path.read_text(encoding="utf-8"))


def fingerprint_extraction(extraction: PageExtraction) -> str:
    payload = {
        "elements": [e.model_dump(mode="json") for e in extraction.elements],
        "headings": extraction.headings,
        "forms": [f.model_dump(mode="json") for f in extraction.forms],
        "title": extraction.title,
    }
    return fingerprint(payload)
