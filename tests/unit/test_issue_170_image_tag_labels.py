"""One entry per digest on the Images page (crucible#170, FDY-0415).

Four tags on the same digest should yield one choice labelled with the
highest release tag and listing the other three, sorted newest release
first. The Current image cell uses the same label.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from crucible.adapters.clock import SystemClock
from crucible.adapters.harness.registry import default_registry
from crucible.application.admin.context import AdminContext
from crucible.application.admin.images import defaults
from crucible.application.harnesses import HarnessRegistry
from crucible.domain.entities import HarnessImage
from crucible.ports.execution import ImageInfo

_WORKER_HARNESSES: dict[str, str] = {
    "agy": "1.2.8",
    "claude_code": "2.1.280",
    "codex": "0.156.0",
    "hermes": "0.19.0",
}


class _FakeRepo:
    """A repo that returns None/empty for everything."""

    def get(self, key: str) -> Any:
        return None

    def list_all(self) -> list[Any]:
        return []

    def put(self, obj: Any) -> Any:
        return obj


class _FakeUow:
    """A minimal fake UnitOfWork that returns empty harness images."""

    @property
    def principals(self) -> Any:
        return _FakeRepo()

    @property
    def harness_images(self) -> Any:
        return _FakeRepo()


class _FakeProvider:
    """A provider that returns a list of (name, ImageInfo) tuples."""

    def __init__(self, images: list[tuple[str, ImageInfo]]) -> None:
        self.images = images

    async def list_images(self) -> list[ImageInfo]:
        return [img for _, img in self.images]


def _make_ctx(
    images: list[tuple[str, ImageInfo]],
    harnesses: HarnessRegistry | None = None,
) -> AdminContext:
    """Build an AdminContext with the given provider images."""
    ctx = AdminContext(
        uow_factory=_FakeUow,  # type: ignore[arg-type]
        clock=SystemClock(),
        providers={"docker": _FakeProvider(images=images)},  # type: ignore[dict-item]
        harnesses=harnesses or default_registry(),
    )
    return ctx


class _UowWithCurrent(_FakeUow):
    """A fake UoW that returns a current image for a given harness."""

    def __init__(self, current: HarnessImage | None) -> None:
        super().__init__()
        self._current = current

    @property
    def principals(self) -> Any:
        return _FakeRepo()

    @property
    def harness_images(self) -> Any:
        return _FakeRepoWithCurrent(self._current)


class _FakeRepoWithCurrent:
    """A repo that returns a current HarnessImage when queried."""

    def __init__(self, current: HarnessImage | None) -> None:
        self._current = current

    def get(self, key: str) -> Any:
        if key == "hermes":
            return self._current
        return None

    def list_all(self) -> list[Any]:
        return []

    def put(self, obj: Any) -> Any:
        return obj


class _UowFactory:
    """Callable that builds a _UowWithCurrent, capturing ``current`` by closure."""

    def __init__(self, current: HarnessImage | None) -> None:
        self._current = current

    def __call__(self) -> _UowWithCurrent:
        return _UowWithCurrent(self._current)


def _make_ctx_with_current(
    images: list[tuple[str, ImageInfo]],
    current: HarnessImage | None,
) -> AdminContext:
    """Build an AdminContext with provider images and a current HarnessImage."""
    ctx = AdminContext(
        uow_factory=_UowFactory(current),  # type: ignore[arg-type]
        clock=SystemClock(),
        providers={"docker": _FakeProvider(images=images)},  # type: ignore[dict-item]
        harnesses=default_registry(),
    )
    return ctx


def _make_current(harness: str, digest: str, reference: str, version: str) -> HarnessImage:
    """Build a HarnessImage to act as a current image."""
    return HarnessImage(
        harness=harness,
        digest=digest,
        reference=reference,
        version=version,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_by="test",
        previous_digest=None,
        previous_reference=None,
        previous_version=None,
    )


# ---------------------------------------------------------------------------
# AC1: Four tags on one digest yield one choice labelled with the highest
#      release tag and listing the other three.
# ---------------------------------------------------------------------------
def test_ac1_four_tags_one_digest() -> None:
    """Four different tags that resolve to the same digest yield one choice."""
    digest = "sha256:" + "a" * 64

    # Four tags on the same digest
    images: list[tuple[str, ImageInfo]] = [
        ("docker", ImageInfo("ghcr.io/example/worker:0.5.5", digest, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:0.6.0", digest, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:0.6.1", digest, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:latest", digest, _WORKER_HARNESSES)),
    ]

    rows: list[dict[str, Any]] = asyncio.run(defaults(_make_ctx(images), _FakeUow()))  # type: ignore[arg-type]

    hermes_row = next(r for r in rows if r["harness"] == "hermes")
    choices = hermes_row["choices"]

    # Should be exactly one choice
    assert len(choices) == 1
    choice = choices[0]

    # The label (reference) should show highest version first with others
    assert choice["reference"] == "0.6.1 (same image as 0.6.0, 0.5.5, latest)"
    # The submitted value is the digest
    assert choice["digest"] == digest
    # Version should be the highest release
    assert choice["version"] == "0.6.1"


# ---------------------------------------------------------------------------
# AC2: Choices are ordered newest release first.
# ---------------------------------------------------------------------------
def test_ac2_newest_release_first() -> None:
    """Two digests with different images should order newest first."""
    digest_a = "sha256:" + "b" * 64
    digest_b = "sha256:" + "c" * 64

    images: list[tuple[str, ImageInfo]] = [
        ("docker", ImageInfo("ghcr.io/example/worker:0.5.0", digest_a, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:0.6.0", digest_b, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:0.5.5", digest_a, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:0.6.1", digest_b, _WORKER_HARNESSES)),
    ]

    rows: list[dict[str, Any]] = asyncio.run(defaults(_make_ctx(images), _FakeUow()))  # type: ignore[arg-type]

    hermes_row = next(r for r in rows if r["harness"] == "hermes")
    choices = hermes_row["choices"]

    # Two choices (two digests), ordered newest first
    assert len(choices) == 2
    # 0.6.x group should come before 0.5.x group
    assert "0.6" in choices[0]["reference"]
    assert "0.5" in choices[1]["reference"]


# ---------------------------------------------------------------------------
# AC3: The Current image cell uses the same label.
# ---------------------------------------------------------------------------
def test_ac3_current_image_same_label() -> None:
    """The current image for a harness gets the same grouped label."""
    digest = "sha256:" + "d" * 64

    images: list[tuple[str, ImageInfo]] = [
        ("docker", ImageInfo("ghcr.io/example/worker:0.5.5", digest, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:0.6.1", digest, _WORKER_HARNESSES)),
        ("docker", ImageInfo("ghcr.io/example/worker:latest", digest, _WORKER_HARNESSES)),
    ]

    # Set current to 0.5.5 (which shares the digest with 0.6.1 and latest)
    current = _make_current("hermes", digest, "ghcr.io/example/worker:0.5.5", "0.19.0")

    rows: list[dict[str, Any]] = asyncio.run(
        defaults(_make_ctx_with_current(images, current), _UowWithCurrent(current))  # type: ignore[arg-type]
    )

    hermes_row = next(r for r in rows if r["harness"] == "hermes")
    current_label = hermes_row["current"]

    # Current should also use the grouped label (highest version + others)
    assert "0.6.1" in current_label["reference"]
    assert "0.5.5" in current_label["reference"]
    assert "latest" in current_label["reference"]


# ---------------------------------------------------------------------------
# AC4: Verify that the test file itself works (placeholder for CI).
# ---------------------------------------------------------------------------
def test_ac4_assert_passes() -> None:
    """Sanity check that pytest can run this file."""
    assert True
