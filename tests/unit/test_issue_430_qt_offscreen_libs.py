"""The worker image carries the shared libraries Qt's offscreen platform needs (hades #430).

A GUI test suite constructs a QApplication with QT_QPA_PLATFORM=offscreen; on Debian 12
that loads libGL, libEGL, libxkbcommon, libdbus-1, libfontconfig, libfreetype, libglib
(with libgthread) and libX11 (which brings libxcb), none of which bookworm-slim has. Each
Debian package is pinned in images/pins.env at the DEBIAN_SNAPSHOT version, installed by
the worker Dockerfile at that pin, passed through by images/build.sh, and listed in spec
13. The proof against the built image is `make images-qt-offscreen-check`
(tools/images/qt_offscreen.sh), which CI's images job runs after the build; this file
checks the pins, the wiring, and the script's arrangement under a stand-in `docker`.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PINS = REPOSITORY / "images" / "pins.env"
DOCKERFILE = REPOSITORY / "images" / "worker" / "Dockerfile"
BUILD_SH = REPOSITORY / "images" / "build.sh"
SPEC = REPOSITORY / "docs" / "spec" / "13-local-operation.md"
CI = REPOSITORY / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPOSITORY / "Makefile"
SCRIPT = REPOSITORY / "tools" / "images" / "qt_offscreen.sh"

# Pin key in images/pins.env -> the Debian package it pins, one per shared library ldd
# reports missing for PySide6's libQt6Gui.so.6 and libqoffscreen.so (libxcb1 is libX11's
# own dependency, pinned with the rest).
PACKAGES = {
    "LIBGL1_VERSION": "libgl1",
    "LIBEGL1_VERSION": "libegl1",
    "LIBXKBCOMMON0_VERSION": "libxkbcommon0",
    "LIBDBUS_1_3_VERSION": "libdbus-1-3",
    "LIBFONTCONFIG1_VERSION": "libfontconfig1",
    "LIBFREETYPE6_VERSION": "libfreetype6",
    "LIBGLIB2_0_0_VERSION": "libglib2.0-0",
    "LIBX11_6_VERSION": "libx11-6",
    "LIBXCB1_VERSION": "libxcb1",
}

# A Debian version: optional epoch, upstream version, Debian revision.
DEBIAN_VERSION = re.compile(r"^(\d+:)?[0-9][A-Za-z0-9.+~-]*-[A-Za-z0-9.+~]+$")


def pins() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in PINS.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_every_qt_library_package_is_pinned_at_a_debian_version() -> None:
    values = pins()
    for key in PACKAGES:
        assert key in values, f"{key} is not pinned in images/pins.env"
        assert DEBIAN_VERSION.match(values[key]), f"{key}={values[key]} is not a Debian version"


def test_the_pins_comment_names_the_ldd_source_and_rules_out_a_display_server() -> None:
    text = PINS.read_text(encoding="utf-8")
    assert "libQt6Gui.so.6" in text
    assert "libqoffscreen.so" in text
    assert "ldd" in text
    assert "xvfb" in text.lower()


def test_the_dockerfile_installs_each_package_at_its_pin() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    values = pins()
    for key, package in PACKAGES.items():
        assert f"ARG {key}={values[key]}\n" in dockerfile, f"{key} ARG default differs from pins"
        assert f'"{package}=${{{key}}}"' in dockerfile, f"{package} is not installed at ${key}"


def test_the_dockerfile_adds_no_display_server_or_fonts() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8").lower()
    for forbidden in ("xvfb", "xserver", "xorg", "fonts-"):
        assert forbidden not in dockerfile, f"the worker image must not carry {forbidden}"


def test_build_sh_requires_and_passes_each_pin() -> None:
    build = BUILD_SH.read_text(encoding="utf-8")
    for key in PACKAGES:
        assert f'"${{{key}:?}}"' in build, f"build.sh does not require {key} from pins.env"
        # hades #475: one `arg_names` list feeds both builders' --build-arg flags.
        passed = build.split("arg_names=(", 1)[1].split(")", 1)[0].split()
        assert key in passed, f"build.sh does not pass {key}"


def test_spec_13_lists_every_package_and_the_check() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    for package in PACKAGES.values():
        assert f"`{package}`" in spec, f"spec 13 does not list {package}"
    assert "QT_QPA_PLATFORM=offscreen" in spec
    assert "images-qt-offscreen-check" in spec


def test_the_images_job_runs_the_offscreen_check_after_the_build() -> None:
    ci = CI.read_text(encoding="utf-8")
    images_job = ci[ci.index("\n  images:\n") : ci.index("\n  e2e-kind:\n")]
    assert "run: make images-qt-offscreen-check" in images_job
    assert images_job.index("make images-check") < images_job.index(
        "make images-qt-offscreen-check"
    )
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(
        r"^images-qt-offscreen-check:.*\n\tDOCKER=.*tools/images/qt_offscreen\.sh", makefile, re.M
    )
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def _run(
    tmp_path: Path, *, fail_on: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run the script under a stand-in `docker` that records every call it gets."""
    log = tmp_path / "calls"
    log.mkdir()
    docker = tmp_path / "docker"
    # One file per call, its arguments NUL-separated: a `-c` script spans lines.
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'%s\\0\' "$@" > "$LOG/$(printf \'%03d\' "$(ls "$LOG" | wc -l)")"\n'
        'case "$*" in *"$FAIL_ON"*) [ -z "$FAIL_ON" ] || exit 1 ;; esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [str(SCRIPT), "crucible-worker:test"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DOCKER": str(docker), "LOG": str(log), "FAIL_ON": fail_on},
        cwd=REPOSITORY,
    )
    calls = [
        path.read_text(encoding="utf-8").rstrip("\0").split("\0") for path in sorted(log.iterdir())
    ]
    return result, calls


def test_the_check_installs_pyside6_then_runs_qapplication_offscreen_with_no_network(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Qt's offscreen platform starts inside crucible-worker:test" in result.stdout

    runs = calls
    install = next(c for c in runs if any("PySide6-Essentials==" in word for word in c))
    start = next(c for c in runs if any("QApplication([])" in word for word in c))
    assert runs.index(install) < runs.index(start)

    # Both run as the worker uid on a read-only root with the Pod's capabilities.
    for container in (install, start):
        assert container[:2] == ["run", "--rm"]
        assert "crucible-worker:test" in container
        for flag in ("--user", "1000:1000", "--read-only", "--cap-drop", "ALL"):
            assert flag in container, f"{flag} missing from {container}"

    # The install is the only networked step; the start has none and runs offscreen.
    assert "--network" not in install
    assert container_option(start, "--network") == "none"
    assert container_option(start, "-e") == "QT_QPA_PLATFORM=offscreen"
    script = start[-1]
    assert "ldd" in script and "libQt6Gui.so.6" in script and "libqoffscreen.so" in script
    assert "from PySide6.QtWidgets import QApplication; QApplication([])" in script

    # The volume the two share is removed afterwards.
    assert runs[-1][:3] == ["volume", "rm", "-f"]


def container_option(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def test_the_check_fails_when_qapplication_does_not_start(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, fail_on="QApplication([])")
    assert result.returncode == 1
    assert "QApplication([]) with QT_QPA_PLATFORM=offscreen fails inside" in result.stderr


def test_the_check_fails_when_the_install_does_not_complete(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, fail_on="PySide6-Essentials==")
    assert result.returncode == 1
    assert "installing PySide6-Essentials" in result.stderr
    assert "the only networked step" in result.stderr


@pytest.mark.parametrize("library", ["libQt6Gui.so.6", "libqoffscreen.so"])
@pytest.mark.parametrize("failure", ["ldd_error", "missing_library"])
def test_the_check_rejects_failed_dependency_inspection(
    tmp_path: Path, library: str, failure: str
) -> None:
    # Execute the actual container shell body, so a successful grep or a second
    # ldd invocation cannot hide the first library inspection failing.
    result, calls = _run(tmp_path)
    assert result.returncode == 0
    start = next(c for c in calls if any("QApplication([])" in word for word in c))
    ldd = tmp_path / "ldd"
    ldd.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  */{library})\n"
        + (
            '    echo "cannot inspect library" >&2; exit 1 ;;\n'
            if failure == "ldd_error"
            else '    echo "libGL.so.1 => not found"; exit 0 ;;\n'
        )
        + '  *) echo "libGL.so.1 => /lib/libGL.so.1" ;;\nesac\n',
        encoding="utf-8",
    )
    ldd.chmod(0o755)
    inspected = subprocess.run(
        ["sh", "-c", start[-1]],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert inspected.returncode == 1
    expected = "ldd failed for" if failure == "ldd_error" else "shared libraries missing from"
    assert expected in inspected.stderr
    assert library in inspected.stderr
