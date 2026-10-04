"""The shell and Git tools available in the script-harness image, without Python."""

import os
import shutil
from pathlib import Path


def collector_env(root: Path) -> dict[str, str]:
    bindir = root / "collector-bin"
    bindir.mkdir(exist_ok=True)
    for name in (
        "sh",
        "git",
        "awk",
        "sort",
        "grep",
        "sed",
        "cat",
        "head",
        "tail",
        "wc",
        "find",
        "dirname",
        "basename",
        "mkdir",
        "rm",
        "cp",
        "mv",
        "chmod",
        "tr",
        "cut",
        "env",
        "test",
        "printf",
        "tee",
    ):
        target = shutil.which(name)
        assert target is not None, name
        link = bindir / name
        if not link.exists():
            link.symlink_to(target)
    assert shutil.which("python3", path=str(bindir)) is None
    return {**os.environ, "PATH": str(bindir)}
