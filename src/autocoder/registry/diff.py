"""Detect what changed between two extractions of the same URL.

Used by the rerun path to decide whether to regenerate the POM /
feature / steps for a given URL. If only headings shifted and the
selectors are unchanged, we skip regeneration to save tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autocoder.models import PageExtraction


@dataclass
class ChangeReport:
    added_elements: list[str] = field(default_factory=list)
    removed_elements: list[str] = field(default_factory=list)
    changed_selectors: list[str] = field(default_factory=list)
    headings_changed: bool = False
    title_changed: bool = False

    @property
    def needs_regeneration(self) -> bool:
        return bool(
            self.added_elements
            or self.removed_elements
            or self.changed_selectors
            or self.title_changed
        )


def diff_extractions(prev: PageExtraction | None, curr: PageExtraction) -> ChangeReport:
    if prev is None:
        return ChangeReport(added_elements=[e.id for e in curr.elements])
    prev_by_id = {e.id: e for e in prev.elements}
    curr_by_id = {e.id: e for e in curr.elements}
    added = [eid for eid in curr_by_id if eid not in prev_by_id]
    removed = [eid for eid in prev_by_id if eid not in curr_by_id]
    changed: list[str] = []
    for eid, current in curr_by_id.items():
        previous = prev_by_id.get(eid)
        if previous is None:
            continue
        if previous.selector != current.selector:
            changed.append(eid)
    return ChangeReport(
        added_elements=added,
        removed_elements=removed,
        changed_selectors=changed,
        headings_changed=prev.headings != curr.headings,
        title_changed=prev.title != curr.title,
    )
