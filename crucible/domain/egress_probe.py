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

The line comes out of the worker, which is untrusted (S4): the harness can print the
marker too. So the first marker line of an attempt is the only one read, it is read only
when a probe is expected at all (the attempt has a network and nothing is recorded yet),
and it is sized before it is parsed: `MAX_PROBE_BYTES` after the marker, `MAX_PROBE_DEPTH`
of nesting and `MAX_PROBE_HOSTS` rows, all well above what the wrapper ever writes (a
row is a hostname, three small numbers and at most 200 characters of curl's message).
A line over any cap is rejected before `json.loads` sees it, and the rejection is what
the attempt records, so no later line is parsed either.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from crucible.domain.time import parse_rfc3339

PROBE_MARKER = "crucible-egress-probe: "

# A host the wrapper could connect to on 443: curl reached the far end and HTTP answered
# (0), or the connection was made and only the TLS handshake (35) or the HTTP exchange
# (52, an empty reply) failed after it. Everything else (6 unresolved, 7 refused, 28
# timed out, 56 a proxy that refused the CONNECT) is the egress path saying no.
REACHABLE_CURL_EXITS: frozenset[int] = frozenset({0, 35, 52})

_DETAIL_CAP = 200

# The caps a line must fit before it is parsed. The wrapper writes one row per
# allowlisted name: the policy's list, the contract's extras and the harness's declared
# endpoints together are a few dozen names at most, and a row is under 600 bytes with
# `detail` already cut to 200 characters inside the worker. The document itself is an
# object holding an array of flat objects: depth three.
MAX_PROBE_BYTES = 64 * 1024
MAX_PROBE_DEPTH = 3
MAX_PROBE_HOSTS = 100

REJECTED_TOO_LONG = f"the probe line exceeds {MAX_PROBE_BYTES} bytes"
REJECTED_TOO_DEEP = f"the probe line nests deeper than {MAX_PROBE_DEPTH} levels"
REJECTED_TOO_MANY_ROWS = f"the probe line carries more than {MAX_PROBE_HOSTS} host rows"
REJECTED_NOT_JSON = "the probe line is not a JSON object"
REJECTED_NOT_A_PROBE = "the probe line has no hosts array"


def probe_expected(policy: Mapping[str, Any] | None, contract: Mapping[str, Any] | None) -> bool:
    """Whether an attempt under this policy and contract runs the probe at all: the
    same rule the providers use for giving the worker a network. No network, no
    wrapper probe, and so no line to believe."""
    network = (policy or {}).get("network") or {}
    if str(network.get("mode", "egress-proxy")) == "none":
        return False
    constraints = (contract or {}).get("constraints") or {}
    return str(constraints.get("network", "policy")) != "none"


def check_shape(payload: str) -> str | None:
    """Why `payload` (the text after the marker) must not be parsed, or None when it
    fits every cap. One pass over the characters, outside JSON strings: nesting depth
    and the number of values directly inside the second level (the host rows) are
    counted without building anything."""
    if len(payload) > MAX_PROBE_BYTES:
        return REJECTED_TOO_LONG
    depth = 0
    rows = 0
    in_string = False
    escaped = False
    for char in payload:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > MAX_PROBE_DEPTH:
                return REJECTED_TOO_DEEP
            if depth == 3:
                rows += 1
                if rows > MAX_PROBE_HOSTS:
                    return REJECTED_TOO_MANY_ROWS
        elif char in "}]":
            depth -= 1
    return None


def read_probe_line(line: str) -> tuple[dict[str, Any] | None, str | None]:
    """`(probe, rejection)` for one log line: `(None, None)` when the line is not a
    marker line at all, `(document, None)` for a wrapper's line, `(None, reason)` for a
    marker line that is over a cap or not a probe document.

    Only a line that starts with the marker counts: a harness that echoes the marker in
    the middle of its own output is not the wrapper speaking."""
    text = line.strip()
    # Kubernetes pods/log prefixes every line with an RFC 3339 timestamp. The normal
    # provider chunker removes it, but accepting the raw shape here makes recording
    # independent of which log boundary supplied the line. Only a parseable timestamp
    # immediately followed by the marker is removed, so a harness mentioning the
    # marker later in ordinary output still does not count.
    prefix, separator, rest = text.partition(" ")
    if separator and rest.startswith(PROBE_MARKER):
        try:
            parse_rfc3339(prefix)
        except ValueError:
            pass
        else:
            text = rest
    if not text.startswith(PROBE_MARKER):
        return None, None
    payload = text[len(PROBE_MARKER) :]
    rejection = check_shape(payload)
    if rejection is not None:
        return None, rejection
    try:
        loaded = json.loads(payload)
    except ValueError:
        return None, REJECTED_NOT_JSON
    if not isinstance(loaded, Mapping):
        return None, REJECTED_NOT_JSON
    raw_hosts = loaded.get("hosts")
    if not isinstance(raw_hosts, list):
        return None, REJECTED_NOT_A_PROBE
    if len(raw_hosts) > MAX_PROBE_HOSTS:
        return None, REJECTED_TOO_MANY_ROWS
    return normalise_probe(loaded), None


def parse_probe_line(line: str) -> dict[str, Any] | None:
    """The probe document in one log line, or None when the line is not one or was
    rejected (`read_probe_line` says which)."""
    probe, _ = read_probe_line(line)
    return probe


def normalise_probe(document: Mapping[str, Any]) -> dict[str, Any] | None:
    """The document with every host row in its one shape, or None when it is not a
    probe document at all. A row the wrapper could not write properly is dropped rather
    than kept half-formed; a document with no usable row is still a probe (of no
    hosts), so an empty allowlist records as such. Rows past `MAX_PROBE_HOSTS` are not
    read."""
    raw_hosts = document.get("hosts")
    if not isinstance(raw_hosts, list):
        return None
    hosts: list[dict[str, Any]] = []
    for row in raw_hosts[:MAX_PROBE_HOSTS]:
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
                "host": host[:_DETAIL_CAP],
                "reachable": reachable,
                "curl_exit": exit_code,
                "ms": ms,
                "detail": detail,
            }
        )
    return {"hosts": hosts}


def find_probe_line(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """The first marker line in a run of log lines, read: `(None, None)` when there is
    none, otherwise what `read_probe_line` made of it. The first marker line decides;
    the wrapper writes its line before the harness can write anything, so a later line
    is never the wrapper's."""
    if PROBE_MARKER not in text:
        return None, None
    for line in text.splitlines():
        probe, rejection = read_probe_line(line)
        if probe is not None or rejection is not None:
            return probe, rejection
    return None, None


def find_probe(text: str) -> dict[str, Any] | None:
    """The first marker line in a run of log lines, parsed, or None when there is none
    or it was rejected."""
    probe, _ = find_probe_line(text)
    return probe


def rejected_record(reason: str) -> dict[str, Any]:
    """What the attempt records when its first marker line was rejected: a probe of no
    hosts that says why, so the page shows it and no later line is read."""
    return {"hosts": [], "rejected": reason}


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
    "MAX_PROBE_BYTES",
    "MAX_PROBE_DEPTH",
    "MAX_PROBE_HOSTS",
    "PROBE_MARKER",
    "REACHABLE_CURL_EXITS",
    "REJECTED_NOT_A_PROBE",
    "REJECTED_NOT_JSON",
    "REJECTED_TOO_DEEP",
    "REJECTED_TOO_LONG",
    "REJECTED_TOO_MANY_ROWS",
    "check_shape",
    "find_probe",
    "find_probe_line",
    "host_words",
    "normalise_probe",
    "parse_probe_line",
    "probe_expected",
    "read_probe_line",
    "rejected_record",
    "unreachable_hosts",
]
