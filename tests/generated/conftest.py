"""Keep only the newest run folder's bundle per slug during pytest
collection.

Every ``run_generate`` invocation drops a fresh
``tests/generated/generated_<timestamp>/`` folder containing the slug
bundles it emitted. Historical folders stay on disk as an audit trail,
but pytest should execute only the current copy of each slug — running
every historical version would re-test stale code and duplicate output.

This hook inspects the run-folder name of each collected item, groups
by slug, and deselects anything that isn't in the lexicographically
newest folder for that slug (the stamp format ``YYYYMMDD_HHMMSS``
sorts chronologically).
"""

from __future__ import annotations

import re
from pathlib import Path


_RUN_FOLDER_RE = re.compile(r"^generated_\d{8}_\d{6}$")


def _run_folder_and_slug(path: Path) -> tuple[str, str] | None:
    # path looks like .../tests/generated/generated_<stamp>/<slug>/test_<slug>.py
    parts = path.parts
    for i, part in enumerate(parts):
        if _RUN_FOLDER_RE.match(part) and i + 1 < len(parts):
            return part, parts[i + 1]
    return None


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    latest_run_per_slug: dict[str, str] = {}
    item_meta: list[tuple[object, str, str]] = []  # (item, run_folder, slug)

    for item in items:
        info = _run_folder_and_slug(Path(str(item.fspath)))
        if info is None:
            continue
        run_folder, slug = info
        item_meta.append((item, run_folder, slug))
        prev = latest_run_per_slug.get(slug)
        if prev is None or run_folder > prev:
            latest_run_per_slug[slug] = run_folder

    kept: list[object] = []
    deselected: list[object] = []
    for item in items:
        info = _run_folder_and_slug(Path(str(item.fspath)))
        if info is None:
            kept.append(item)
            continue
        run_folder, slug = info
        if latest_run_per_slug.get(slug) == run_folder:
            kept.append(item)
        else:
            deselected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
