"""The worker's egress probe: one line in the worker log, parsed into an attempt record
(hades #425).

Before the harness starts, the launch wrapper tries every host of the attempt's egress
allowlist the way the harness itself would (curl to port 443, through the proxy variables
when the provider set them, straight through the NetworkPolicy when it did not) and
writes one line to stderr:

    crucible-egress-probe: {"hosts": [{"host": "pypi.org", "reachable": true,
        "curl_exit": 0, "ms": 120, "detail": ""}, ...]}

The supervisor reads that line off the log stream it already pulls and keeps the parsed
document on the attempt, so a dependency install that failed can be read against what
the worker could actually reach rather than blamed on the worker. This module is the
line's shape and nothing else: pure, with no provider and no database in it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

PROBE_MARKER = "crucible-egress-probe: "

# A host the wrapper could connect to on 443: curl reached the far end and HTTP answered
# (0), or the connection was made and only the TLS handshake (35) or the HTTP exchange
# (52, an empty reply) failed after it. Everything else (6 unresolved, 7 refused, 28
# timed out, 56 a proxy that refused the CONNECT) is the egress path saying no.
REACHABLE_CURL_EXITS: frozenset[int] = frozenset({0, 35, 52})

_DETAIL_CAP = 200


def parse_probe_line(line: str) -> dict[str, Any] | None:
    """The probe document in one log line, or None when the line is not one.

    Only a line that starts with the marker counts: a harness that echoes the marker in
    the middle of its own output is not the wrapper speaking."""
    text = line.strip()
    if not text.startswith(PROBE_MARKER):
        return None
    try:
        loaded = json.loads(text[len(PROBE_MARKER) :])
    except ValueError:
        return None
    if not isinstance(loaded, Mapping):
        return None
    return normalise_probe(loaded)


def normalise_probe(document: Mapping[str, Any]) -> dict[str, Any] | None:
    """The document with every host row in its one shape, or None when it is not a
    probe document at all. A row the wrapper could not write properly is dropped rather
    than kept half-formed; a document with no usable row is still a probe (of no
    hosts), so an empty allowlist records as such."""
    raw_hosts = document.get("hosts")
    if not isinstance(raw_hosts, list):
        return None
    hosts: list[dict[str, Any]] = []
    for row in raw_hosts:
        if not isinstance(row, Mapping):
            continue
        host = row.get("host")
        if not isinstance(host, str) or not host:
            continue
        exit_code = row.get("curl_exit")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            exit_code = None
        reachable = row.get("reachable")
        if not isinstance(reachable, bool):
            reachable = exit_code in REACHABLE_CURL_EXITS if exit_code is not None else False
        ms = row.get("ms")
        if isinstance(ms, bool) or not isinstance(ms, int) or ms < 0:
            ms = None
        detail = row.get("detail")
        detail = detail[:_DETAIL_CAP] if isinstance(detail, str) else ""
        hosts.append(
            {
                "host": host,
                "reachable": reachable,
                "curl_exit": exit_code,
                "ms": ms,
                "detail": detail,
            }
        )
    return {"hosts": hosts}


def find_probe(text: str) -> dict[str, Any] | None:
    """The first probe line in a run of log lines, parsed."""
    if PROBE_MARKER not in text:
        return None
    for line in text.splitlines():
        parsed = parse_probe_line(line)
        if parsed is not None:
            return parsed
    return None


def unreachable_hosts(probe: Mapping[str, Any]) -> list[str]:
    return [
        str(row["host"])
        for row in probe.get("hosts") or []
        if isinstance(row, Mapping) and not row.get("reachable")
    ]


def host_words(row: Mapping[str, Any]) -> str:
    """One host's result in words, for a page or a log line: `pypi.org reachable`,
    `github.com unreachable (curl 28: Connection timed out after 5001 ms)`."""
    host = str(row.get("host", ""))
    if row.get("reachable"):
        words = f"{host} reachable"
        if row.get("curl_exit") not in (0, None):
            words += f" (connected; curl {row['curl_exit']}: {row.get('detail') or ''})".replace(
                ": )", ")"
            )
        return words
    exit_code = row.get("curl_exit")
    detail = str(row.get("detail") or "")
    if exit_code is None:
        return f"{host} unreachable" + (f" ({detail})" if detail else "")
    return f"{host} unreachable (curl {exit_code}" + (f": {detail})" if detail else ")")


__all__ = [
    "PROBE_MARKER",
    "REACHABLE_CURL_EXITS",
    "find_probe",
    "host_words",
    "normalise_probe",
    "parse_probe_line",
    "unreachable_hosts",
]
