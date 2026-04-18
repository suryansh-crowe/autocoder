"""Local-LLM verification — prove that inference stays on this machine.

What this script does (in order):

1. Loads the autocoder Settings exactly the way the CLI does.
2. Prints the active model + endpoint + provider class name.
3. Installs a global socket-level monitor that records every outbound
   TCP connection made by the Python process.
4. Sends one real ``/api/chat`` request to the configured Ollama
   endpoint with a 5-token prompt.
5. Inspects the recorded destinations and reports PASS only when:
     * the test prompt got a response,
     * every destination is loopback (127.0.0.1, ::1) or a private
       LAN address (RFC 1918 / fc00::/7) under your control.
6. Exits 0 on PASS, 1 on FAIL.

Run:

    python scripts/verify_local_llm.py

Re-run after any config change. Pass-fail is deterministic given a
running Ollama; nothing in this script needs the network beyond the
prompt itself.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable

# Make the autocoder package importable when running from a checkout.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from autocoder.config import load_settings  # noqa: E402
from autocoder.llm.ollama_client import OllamaClient  # noqa: E402


# ---------------------------------------------------------------------------
# Outbound-connection monitor (works without elevated privileges)
# ---------------------------------------------------------------------------


@dataclass
class _Conn:
    family: str
    host: str
    port: int


_observed: list[_Conn] = []
_orig_getaddrinfo = socket.getaddrinfo
_orig_connect = socket.socket.connect


def _record(host: str, port: int, family: str) -> None:
    _observed.append(_Conn(family=family, host=host, port=port))


def _patched_getaddrinfo(host, port, *args, **kwargs):
    # Capture DNS resolutions too — useful when a hostname is supplied.
    try:
        return _orig_getaddrinfo(host, port, *args, **kwargs)
    finally:
        if isinstance(host, str):
            _record(host, int(port) if port else 0, "dns")


def _patched_connect(self, address):
    if isinstance(address, tuple) and len(address) >= 2:
        host = str(address[0])
        port = int(address[1])
        family = "ipv6" if ":" in host else "ipv4"
        _record(host, port, family)
    return _orig_connect(self, address)


@contextmanager
def network_monitor():
    socket.getaddrinfo = _patched_getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = _patched_connect  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = _orig_getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = _orig_connect  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Locality test — loopback + RFC1918 + ULA + link-local
# ---------------------------------------------------------------------------


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local(host: str) -> tuple[bool, str]:
    """Return (is_local, reason)."""
    if host in _LOOPBACK_HOSTS:
        return True, "loopback hostname"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False, f"hostname {host!r} did not resolve to a literal IP"
    if ip.is_loopback:
        return True, "loopback IP"
    if ip.is_private:
        return True, "RFC1918 / ULA private IP"
    if ip.is_link_local:
        return True, "link-local IP"
    return False, "public IP"


def classify(observed: Iterable[_Conn]) -> tuple[list[_Conn], list[_Conn]]:
    local: list[_Conn] = []
    remote: list[_Conn] = []
    for c in observed:
        if c.family == "dns" and c.host in _LOOPBACK_HOSTS:
            local.append(c)
            continue
        if c.family == "dns":
            # Defer DNS judgment to the actual connect() that follows.
            local.append(c) if _is_local(c.host)[0] else remote.append(c)
            continue
        ok, _ = _is_local(c.host)
        (local if ok else remote).append(c)
    return local, remote


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    settings = load_settings()
    o = settings.ollama

    print("=" * 72)
    print("autocoder local-LLM verification")
    print("=" * 72)
    print(f"  model         : {o.model}")
    print(f"  endpoint      : {o.endpoint}")
    print(f"  provider class: OllamaClient (autocoder.llm.ollama_client)")
    print(f"  num_ctx       : {o.num_ctx}")
    print(f"  num_predict   : {o.num_predict}")
    print()

    prompt = "Reply with the single word 'pong' and nothing else."
    print(f"  test prompt   : {prompt!r}")
    print(f"  sending one real /api/chat request and recording all outbound TCP")
    print()

    # Monitor MUST start before the HTTP client — otherwise httpx's
    # keep-alive socket is created outside the patched window and the
    # chat call reuses it without ever invoking socket.connect().
    started = time.monotonic()
    client: OllamaClient | None = None
    resp = None
    try:
        with network_monitor():
            client = OllamaClient(o)
            if not client.is_available():
                print("FAIL: configured endpoint is not reachable. Is Ollama running?")
                print(f"      curl {o.endpoint}/api/tags")
                return 1
            resp = client.chat(
                system="You answer in one word.",
                user=prompt,
                json_mode=False,
                max_tokens=8,
                purpose="verify_local_llm",
            )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: chat request raised: {exc!s}")
        return 1
    finally:
        if client is not None:
            client.close()
    elapsed = time.monotonic() - started
    if resp is None:
        print("FAIL: no response captured.")
        return 1

    print(f"  response      : {resp.text.strip()[:80]!r}")
    print(f"  in/out tokens : {resp.prompt_eval_count}/{resp.eval_count}")
    print(f"  duration      : {elapsed:.2f}s")
    print()

    local, remote = classify(_observed)

    print("recorded outbound destinations during the request:")
    if not _observed:
        print("  (none)")
    for c in _observed:
        ok, reason = _is_local(c.host)
        tag = "local" if ok else "REMOTE"
        print(f"  [{tag:6}] {c.family:5} {c.host}:{c.port}   ({reason})")
    print()

    verdict = "PASS" if not remote and resp.text.strip() else "FAIL"
    print("=" * 72)
    print(f"VERDICT: {verdict}")
    print("=" * 72)
    if verdict == "PASS":
        print("All outbound connections during the request stayed on loopback / "
              "private network. Inference is local-only.")
        return 0
    print("One or more connections went to a public IP. Investigate above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
