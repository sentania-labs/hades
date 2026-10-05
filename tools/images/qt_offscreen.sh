#!/usr/bin/env bash
# Qt's offscreen platform must start inside the worker image (hades #430), so a GUI
# test suite can run in a worker with no display server. The image carries the shared
# libraries ldd reports missing for PySide6's libQt6Gui.so.6 and libqoffscreen.so
# (images/pins.env); this proves they are enough, in the arrangement a worker Pod has:
# uid 1000, a read-only root, no capabilities, and fsGroup-style storage, the same as
# tools/images/unit_in_image.sh.
#
#   tools/images/qt_offscreen.sh [image]
#
# The image defaults to WORKER in images/manifest.env, which `make images` and `make
# images-check` leave in the daemon. Two containers share one volume that stands in for
# the workspace claim:
#   1. `uv venv` and `uv pip install PySide6-Essentials==$PYSIDE6_VERSION`, the only
#      step with the network (PySide6-Essentials is the wheel that carries QtWidgets
#      and the platform plugins; the full PySide6 only adds the Addons).
#   2. With no network: ldd on the two libraries, which must report nothing missing,
#      then `QApplication([])` with QT_QPA_PLATFORM=offscreen, which must exit 0.
#
# Environment: DOCKER (the Docker CLI or the rootless wrapper), PYSIDE6_VERSION (the
# wheel version to install; the default is the one the pins were taken from).
set -euo pipefail

docker=${DOCKER:-docker}
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
pyside6_version=${PYSIDE6_VERSION:-6.11.2}
image=${1:-}
if [ -z "$image" ]; then
    image=$(sed -n 's/^WORKER=//p' "$repo_root/images/manifest.env")
fi
[ -n "$image" ] || { echo "qt_offscreen.sh: no WORKER in images/manifest.env" >&2; exit 2; }

volume="crucible-qt-offscreen-$$"
cleanup() { $docker volume rm -f "$volume" >/dev/null 2>&1 || true; }
trap cleanup EXIT
$docker volume create "$volume" >/dev/null

# What the Pod's securityContext and emptyDir volumes are (crucible/adapters/execution/
# k8sspec.py, pod_spec and memory_volume): an fsGroup emptyDir is root:1000, mode 2777.
pod=(
    --rm --pull never --user 1000:1000 --read-only --cap-drop ALL
    --security-opt no-new-privileges
    --tmpfs "/tmp:rw,exec,size=2g,uid=0,gid=1000,mode=2777"
    --tmpfs "/home/worker:rw,exec,size=2g,uid=0,gid=1000,mode=2777"
    --mount "type=volume,src=$volume,dst=/work,volume-nocopy"
)

# The claim as the kubelet hands it over: group 1000, setgid. Only this step is root,
# as the kubelet is.
$docker run --rm --pull never --user 0:0 -v "$volume:/work" --entrypoint sh "$image" \
    -c 'chown 0:1000 /work && chmod 2770 /work'

# The test venv, made by the worker uid with the image's own uv and CPython 3.12
# (UV_PYTHON_DOWNLOADS=never is the image's default), from PyPI: the networked step.
if ! $docker run "${pod[@]}" -w /work --entrypoint sh "$image" -c '
    set -eu
    uv venv --quiet --python python3.12 /work/venv
    uv pip install --quiet --python /work/venv/bin/python "PySide6-Essentials==$1"
' sh "$pyside6_version"; then
    echo "qt_offscreen.sh: installing PySide6-Essentials $pyside6_version (the only networked step) failed inside $image" >&2
    exit 1
fi

# No network from here on. ldd first, so a missing library is named rather than left
# to Qt's "could not load the Qt platform plugin" message.
if ! $docker run "${pod[@]}" -w /work --network none -e QT_QPA_PLATFORM=offscreen \
    --entrypoint sh "$image" -c '
    set -eu
    qt=/work/venv/lib/python3.12/site-packages/PySide6/Qt
    for library in "$qt/lib/libQt6Gui.so.6" "$qt/plugins/platforms/libqoffscreen.so"; do
        if ! dependencies=$(ldd "$library" 2>&1); then
            echo "ldd failed for $library:" >&2
            echo "$dependencies" >&2
            exit 1
        fi
        printf "%s\n" "$dependencies"
        case "$dependencies" in
            *"not found"*)
                echo "shared libraries missing from $library" >&2
                exit 1
                ;;
        esac
    done
    exec /work/venv/bin/python -c "from PySide6.QtWidgets import QApplication; QApplication([])"
'; then
    echo "qt_offscreen.sh: QApplication([]) with QT_QPA_PLATFORM=offscreen fails inside $image" >&2
    exit 1
fi
echo "qt_offscreen.sh: Qt's offscreen platform starts inside $image (PySide6-Essentials $pyside6_version)"
