"""hades #315: two attempts of the same writable harness sync their refreshed auth
files back at once. Before this fix both wrote through one shared temporary pathname
(the Docker path, `<name>.crucible-sync`) and both replaced the source unconditionally,
so whichever attempt's `os.replace` landed last won even when its token was the older
one. The fix: a temporary pathname unique to each attempt, and a compare-and-swap that
re-reads the source under a lock immediately before the replace and skips the write
when the source has already moved past the candidate the attempt started from.

AC1: two collections of the same harness racing in either order leave the newer token
in place, on the Docker path.
AC2: no two attempts share a temporary pathname, on the Docker path.
AC3: the Kubernetes sync path refuses to replace a source that moved past the
candidate.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.docker import _CredentialCopy, _sync_one
from crucible.adapters.execution.k8sspec import secret as k8s_secret
from crucible.adapters.harness.codex import CodexAdapter
from crucible.ports.harness import CredentialSource, MountMode
from tests.unit.kubernetes_fixtures import build as k8s_build

ATTEMPT_OLD = "01ATTEMPTOLD000000000000A"
ATTEMPT_NEW = "01ATTEMPTNEW000000000000A"


def _token(prefix: str, count: int = 40) -> str:
    return prefix + "x" * count


def _source(tmp_path: Path, *, last_refresh: str) -> Path:
    source = tmp_path / "credentials" / "codex"
    source.mkdir(parents=True)
    (source / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": last_refresh,
                "tokens": {"access_token": _token("eyJ"), "refresh_token": _token("rt-")},
            }
        ),
        encoding="utf-8",
    )
    return source


def _copy_for(source: Path, attempt_id: str) -> _CredentialCopy:
    return _CredentialCopy(
        spec=CodexAdapter().credential_spec(),
        source=CredentialSource(str(source)),
        mode=MountMode.RW_NARROW,
        seeded={},
        attempt_id=attempt_id,
    )


def _rotated(last_refresh: str, marker: str) -> bytes:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "last_refresh": last_refresh,
            "tokens": {"access_token": _token("eyJ", 50), "refresh_token": marker},
        }
    ).encode()


# ----- AC2: no two attempts share a temporary pathname (Docker) -----------------


def test_two_attempts_never_write_through_the_same_temporary_pathname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path, last_refresh="2026-09-17T00:00:00Z")
    opened: list[str] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)

    copy_old = _copy_for(source, ATTEMPT_OLD)
    copy_new = _copy_for(source, ATTEMPT_NEW)
    result_old = _sync_one(
        copy_old, copy_old.spec.auth_files[0], _rotated("2026-09-17T01:00:00Z", "a")
    )
    result_new = _sync_one(
        copy_new, copy_new.spec.auth_files[0], _rotated("2026-09-17T02:00:00Z", "b")
    )
    assert result_old.synced and result_new.synced

    temporaries = [p for p in opened if ".crucible-sync." in p and not p.endswith(".lock")]
    assert len(temporaries) == 2
    assert temporaries[0] != temporaries[1]
    assert temporaries[0].endswith(f".crucible-sync.{ATTEMPT_OLD}")
    assert temporaries[1].endswith(f".crucible-sync.{ATTEMPT_NEW}")
    # The old shared name (hades #315) is never opened at all.
    assert not any(p.endswith("auth.json.crucible-sync") for p in opened)


# ----- AC1: two collections racing in either order leave the newer token (Docker) --


@pytest.mark.parametrize("blocked", ["old", "new"])
def test_two_collections_racing_leave_the_newer_token_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    """Whichever attempt is slower to reach the lock still loses to the token that is
    actually newer: the compare-and-swap re-reads the live source under the lock and
    redoes the newer-than check, rather than trusting the read each attempt started
    from. Both orders (the older attempt blocked at the lock, and the newer one) leave
    the newer token in place."""
    source = _source(tmp_path, last_refresh="2026-09-17T00:00:00Z")
    old_data = _rotated("2026-09-17T01:00:00Z", "old-token")
    new_data = _rotated("2026-09-17T02:00:00Z", "new-token")
    data_by_label = {"old": old_data, "new": new_data}
    attempt_by_label = {"old": ATTEMPT_OLD, "new": ATTEMPT_NEW}
    other = "new" if blocked == "old" else "old"

    real_flock = fcntl.flock
    reached_lock = threading.Event()
    release_lock = threading.Event()
    markers: dict[int, str] = {}

    def patched_flock(fd: int, op: int) -> None:
        if markers.get(threading.get_ident()) == blocked:
            reached_lock.set()
            assert release_lock.wait(timeout=5), "the other attempt never finished"
        real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", patched_flock)

    results: dict[str, Any] = {}

    def run(label: str) -> None:
        markers[threading.get_ident()] = label
        copy = _copy_for(source, attempt_by_label[label])
        results[label] = _sync_one(copy, copy.spec.auth_files[0], data_by_label[label])

    blocked_thread = threading.Thread(target=run, args=(blocked,))
    blocked_thread.start()
    assert reached_lock.wait(timeout=5), "the blocked attempt never reached the lock"

    run(other)
    release_lock.set()
    blocked_thread.join(timeout=5)

    # Whichever order the two attempts actually land in, the newer token is what
    # ends up on disk.
    assert (source / "auth.json").read_bytes() == new_data
    assert results["new"].synced
    if blocked == "old":
        # "new" reached the lock first and won; "old" then found the source had
        # already moved past the candidate it started from, and skipped.
        assert not results["old"].synced
        assert "moved past the candidate" in results["old"].reason
    else:
        # "old" reached the lock first (nothing had raced yet) and legitimately
        # won it; "new" then landed after it, correctly, since "new" really is
        # the newer token.
        assert results["old"].synced
        assert "newer issued-at, written back" in results["old"].reason


# ----- AC3: the Kubernetes path refuses to replace a source that moved past it -----


def test_kubernetes_sync_refuses_a_source_that_moved_past_the_candidate() -> None:
    """A second write's patch carries the resourceVersion this attempt read; a Secret
    that already moved (another attempt's sync-back already landed) answers 409, and
    the sync is recorded as skipped rather than retried blind or raised past collection
    (hades #315)."""
    api, _registry, provider = k8s_build()
    secret_name = "crucible-harness-codex"
    old_document = {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-09-17T00:00:00Z",
        "tokens": {"refresh_token": "original"},
    }
    api.create(
        "secrets",
        k8s_secret(
            name=secret_name,
            namespace=api.namespace,
            object_labels={},
            data={"auth.json": json.dumps(old_document).encode()},
        ),
    )

    real_get = api.get
    winner_document = {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-09-17T01:30:00Z",
        "tokens": {"refresh_token": "already-won"},
    }

    def racing_get(kind: str, name: str) -> dict[str, Any]:
        # The fake client's `get` hands back the stored object itself, not a copy
        # (as the real one, over HTTP, never could); snapshot it before the race
        # patch below mutates that same object in place.
        body: dict[str, Any] = json.loads(json.dumps(real_get(kind, name)))
        if kind == "secrets" and name == secret_name:
            # Another attempt's sync-back lands between this read and this attempt's
            # own patch, moving the Secret's resourceVersion past what was just read.
            api.patch(
                "secrets",
                name,
                {
                    "data": {
                        "auth.json": base64.b64encode(json.dumps(winner_document).encode()).decode(
                            "ascii"
                        )
                    }
                },
            )
        return body

    api.get = racing_get  # type: ignore[method-assign]

    copy = kubernetes_module._CredentialCopy(
        spec=CodexAdapter().credential_spec(),
        source_secret=secret_name,
        mode=MountMode.RW_NARROW,
        seeded={},
    )
    auth = copy.spec.auth_files[0]
    this_attempt_data = json.dumps(
        {
            "auth_mode": "chatgpt",
            "last_refresh": "2026-09-17T01:00:00Z",
            "tokens": {"refresh_token": "stale-candidate"},
        }
    ).encode()

    result = asyncio.run(provider._sync_file(copy, auth, this_attempt_data))

    assert result.changed and not result.synced
    assert "moved past the candidate" in result.reason

    stored = real_get("secrets", secret_name)
    stored_raw = (stored.get("data") or {}).get("auth.json")
    stored_document = json.loads(base64.b64decode(str(stored_raw)).decode("utf-8"))
    assert stored_document["tokens"]["refresh_token"] == "already-won"
