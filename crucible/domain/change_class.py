"""Change classification for scoped CI and release (hades #476, the operator's design
of 2026-10-06).

Decided once per run from the paths a change touches, never from what a job tests
(that stays out of scope): core (`crucible/`, `tests/unit`, `tests/integration`,
`pyproject.toml`, `uv.lock`, `docs`) always runs. A path under `images/` or
`tools/images/` adds the images class, which also pulls in the kind tier (the images
and the e2e-kind job share the same worker image). A path under `tools/kind/`,
`deploy/`, or `tests/e2e/test_kind.py` adds the kind class on its own. A path under
`.github/` is the workflow override: everything runs, no exceptions. A path that
matches none of the above is unclassified and also runs everything, the same as the
workflow override, so an unrecognized path never narrows what CI proves.

This module is the single definition: `tools/ci/changes.py` imports it to gate
`.github/workflows/ci.yml` through job outputs, and `crucible.domain.certification`
records its `label` on the certification it computes.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass

CORE_PREFIXES: tuple[str, ...] = ("crucible/", "tests/unit/", "tests/integration/", "docs/")
CORE_FILES: frozenset[str] = frozenset({"pyproject.toml", "uv.lock"})
IMAGES_PREFIXES: tuple[str, ...] = ("images/", "tools/images/")
KIND_PREFIXES: tuple[str, ...] = ("tools/kind/", "deploy/")
KIND_FILES: frozenset[str] = frozenset({"tests/e2e/test_kind.py"})
WORKFLOW_PREFIX = ".github/"


@dataclass(frozen=True, slots=True)
class Classification:
    """Which classes a change's paths touched. `core` is not a field: it always runs,
    whatever else is true, so it carries no information here."""

    images: bool = False
    kind: bool = False
    workflow: bool = False
    unclassified: bool = False

    @property
    def run_everything(self) -> bool:
        """The workflow override and the unclassified-path default (hades #476)."""
        return self.workflow or self.unclassified

    @property
    def run_images(self) -> bool:
        return self.run_everything or self.images

    @property
    def run_kind(self) -> bool:
        """Images adds the kind tier too: the images and e2e-kind jobs share the
        worker image this class is named for."""
        return self.run_everything or self.images or self.kind

    @property
    def label(self) -> str:
        """The single most informative name for this classification, in the priority
        a reader needs: a workflow change explains everything running, an unclassified
        path explains it when there was no workflow change, and otherwise the
        narrowest class that matched names what actually ran."""
        if self.workflow:
            return "workflow"
        if self.unclassified:
            return "unclassified"
        if self.images:
            return "images"
        if self.kind:
            return "kind"
        return "core"


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def classify(paths: Iterable[str]) -> Classification:
    """Classify a change from its touched paths (hades #476). Each path is attributed
    to the first class it matches, in the fixed order: workflow, images, kind, core;
    anything left over is unclassified. An empty `paths` classifies as core only,
    since there is nothing to widen it."""
    images = kind = workflow = unclassified = False
    for raw in paths:
        path = posixpath.normpath(str(raw))
        if path == ".github" or _under(path, (WORKFLOW_PREFIX,)):
            workflow = True
        elif _under(path, IMAGES_PREFIXES):
            images = True
        elif _under(path, KIND_PREFIXES) or path in KIND_FILES:
            kind = True
        elif _under(path, CORE_PREFIXES) or path in CORE_FILES:
            continue
        else:
            unclassified = True
    return Classification(images=images, kind=kind, workflow=workflow, unclassified=unclassified)
