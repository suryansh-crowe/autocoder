"""Registry layer — persistent state that drives resume + extension.

The single source of truth is the on-disk ``manifest/registry.yaml``
file (path is configured via ``AUTOCODER_MANIFEST_DIR``). This Python
subpackage owns the read/write/diff/resume logic for it. Everything
else under ``manifest/`` (extractions, plans, heals, logs) is derived
data that can be regenerated.
"""

from autocoder.registry.diff import diff_extractions, ChangeReport
from autocoder.registry.resume import next_actionable_nodes
from autocoder.registry.store import RegistryStore

__all__ = ["RegistryStore", "diff_extractions", "ChangeReport", "next_actionable_nodes"]
