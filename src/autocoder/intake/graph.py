"""Dependency graph + topological order over the classified URLs.

Edges captured:

* Anything authenticated depends on the login URL.
* A URL with ``redirects_to`` depends on the redirect target if the
  target is also tracked.
* The user can pin extra ``depends_on`` values manually in the
  registry; we honour those too.

Producing a deterministic topological order means resume/rerun gives
the same processing sequence every time, which makes the manifest
diffable.
"""

from __future__ import annotations

from collections import defaultdict, deque

from autocoder.models import URLKind, URLNode


def build_dependency_graph(
    nodes: list[URLNode],
    login_url: str | None,
) -> dict[str, list[str]]:
    """Return ``{url: [urls it depends on]}``."""
    deps: dict[str, set[str]] = defaultdict(set)
    by_url = {n.url: n for n in nodes}

    if login_url:
        for n in nodes:
            if n.requires_auth and n.url != login_url:
                deps[n.url].add(login_url)

    for n in nodes:
        if n.redirects_to and n.redirects_to in by_url and n.redirects_to != n.url:
            deps[n.url].add(n.redirects_to)
        for manual in n.depends_on:
            if manual in by_url and manual != n.url:
                deps[n.url].add(manual)

    return {u: sorted(v) for u, v in deps.items()}


def topological_order(
    nodes: list[URLNode],
    deps: dict[str, list[str]],
) -> list[URLNode]:
    """Kahn's algorithm. Login-class URLs always sort first within a tier."""
    by_url = {n.url: n for n in nodes}
    indeg: dict[str, int] = {n.url: 0 for n in nodes}
    out: dict[str, list[str]] = defaultdict(list)
    for u, ds in deps.items():
        for d in ds:
            if d in indeg:
                indeg[u] += 1
                out[d].append(u)

    def _priority(url: str) -> tuple[int, str]:
        kind = by_url[url].kind
        # 0 = login first; 1 = post-login landing; 2 = everything else
        if kind == URLKind.LOGIN:
            return (0, url)
        if kind == URLKind.POST_LOGIN_LANDING:
            return (1, url)
        return (2, url)

    ready = deque(sorted([u for u, d in indeg.items() if d == 0], key=_priority))
    ordered: list[URLNode] = []
    while ready:
        u = ready.popleft()
        ordered.append(by_url[u])
        for nxt in out.get(u, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                # re-sort the ready queue after each insertion to keep priority stable
                ready.append(nxt)
        ready = deque(sorted(ready, key=_priority))

    if len(ordered) != len(nodes):
        # cycle: append the rest in arrival order rather than crashing
        seen = {n.url for n in ordered}
        ordered.extend(n for n in nodes if n.url not in seen)
    return ordered
