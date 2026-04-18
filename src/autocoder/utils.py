"""Tiny shared helpers — no business logic here."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str, fallback: str = "page") -> str:
    """Return an ASCII slug suitable for a Python identifier or filename."""
    cleaned = _SLUG_RE.sub("_", value.strip().lower()).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}".rstrip("_")
    return cleaned or fallback


def url_slug(url: str) -> str:
    """Slug derived from path + last segment, falling back to host."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_") or parsed.netloc.replace(".", "_")
    return slugify(path, fallback="root")


def page_class_name(slug: str) -> str:
    """Convert a slug into a CamelCase POM class name."""
    parts = [p for p in slug.split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) + "Page"


def fingerprint(payload: Any) -> str:
    """Stable SHA-256 of any JSON-serialisable structure."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def python_identifier(value: str) -> str:
    """Force a string into a valid python identifier."""
    out = re.sub(r"\W+", "_", value).strip("_").lower()
    if not out:
        out = "x"
    if out[0].isdigit():
        out = f"_{out}"
    return out
