#!/usr/bin/env bash
# The worker images, built from a staged copy of images/ (13, C11, FDY-0072).
#
#   tools/images/images.sh build     build every image and write images/manifest.env back
#   tools/images/images.sh check     build every image; fail when any tag, harness list
#                                    or OCI digest differs from images/manifest.env
#
# With DIGEST_WRITEBACK=1, `check` passes when only *_DIGEST lines differ and writes the
# built digests into images/manifest.env for the CI images job to commit (FDY-0310: CI
# owns the digest lines). A tag, harness list or entry that differs still fails. The
# job sets it only on a branch, never on main, a tag or its own digest commit; the
# release never sets it.
#
# Either way the images end up loaded in the daemon under their manifest tags, which is
# where the release pushes them from (tools/release/push_worker_images.sh).
#
# Why a staged copy: on the reference workstation the build daemon is the `crucible`
# service user's rootless daemon, and that user cannot read a checkout under the
# operator's home (mode 750). The Docker CLI the DOCKER wrapper runs as that user
# reads the build context and writes the OCI and docker archives, so both live in a
# scratch directory it can reach. images/build.sh still does all the building; this
# script only moves its inputs and its manifest. CI runs the same script on a runner
# whose daemon can read anything, where the staging is merely harmless.
#
# Environment: DOCKER (the Docker CLI or the rootless wrapper), NO_CACHE and CACHE_DIR
# (passed to build.sh), DIGEST_WRITEBACK (see above), IMAGES_STAGE_ROOT (where the
# scratch directory goes, default TMPDIR or /tmp).
set -euo pipefail

mode=${1:-}
case "$mode" in
    build|check) ;;
    *) echo "images.sh: usage: images.sh build|check" >&2; exit 2 ;;
esac

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_dir="$repo_root/images"

stage=$(mktemp -d "${IMAGES_STAGE_ROOT:-${TMPDIR:-/tmp}}/crucible-images.XXXXXX")
trap 'rm -rf "$stage"' EXIT
# The build inputs are public, so the staged tree is world-readable; only `out`, where
# the daemon user's CLI writes the archives, is writable by it, and it is sticky so no
# other local user can replace an archive once written. build.sh then checks the
# image it loads against the OCI archive. Previous archives in images/out are never
# staged.
tar -C "$source_dir" --exclude=./out -cf - . | tar -C "$stage" -xf -
mkdir "$stage/out"
chmod -R a+rX "$stage"
chmod 1777 "$stage/out"

# Every image is built, so the manifest is written fresh: an entry for an image
# directory that no longer exists cannot survive a build. The header is written here,
# not by build.sh, because build.sh is a build input and editing it would change every
# tag; build.sh keeps an existing header.
cat > "$stage/manifest.env" <<'HEADER'
# Written by tools/images/images.sh: the tag, OCI manifest digest and harness versions
# of each image. The declared pin the tiers read; never chosen by creation time (13, C5).
# The digest is the local build's OCI digest, the record `make images-check` reproduces.
# It is not the registry's: the release pushes with `docker push`, which re-encodes the
# layers, and nothing compares the two (the operator's decision, 2026-09-23). The
# release notes carry the digest the registry reports.
HEADER
OUT="$stage/out" MANIFEST="$stage/manifest.env" "$stage/build.sh"

case "$mode" in
    build)
        cp "$stage/manifest.env" "$source_dir/manifest.env"
        echo "images.sh: wrote images/manifest.env"
        ;;
    check)
        # The whole file, not a subset: a tag, a harness version or a digest that the
        # build did not reproduce is drift, and so is an entry the build did not write.
        if ! diff -u "$source_dir/manifest.env" "$stage/manifest.env"; then
            if [ "${DIGEST_WRITEBACK:-}" = 1 ] &&
                python3 "$repo_root/tools/images/digest_commit.py" apply \
                    "$source_dir/manifest.env" "$stage/manifest.env"; then
                echo "images.sh: every tag and harness version reproduced; only digests differed, and CI commits them"
                exit 0
            fi
            echo "images.sh: the build from the pinned inputs does not reproduce images/manifest.env" >&2
            exit 1
        fi
        echo "images.sh: every image reproduced its declared tag, harness versions and digest"
        ;;
esac

