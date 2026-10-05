#!/usr/bin/env bash
# Build the Crucible worker images reproducibly (spec 13, ADR 0011).
#
#   images/build.sh [worker|script-harness ...]      default: both
#
# Environment:
#   OUT=<dir>       where the OCI and docker tarballs go (default images/out)
#   NO_CACHE=1      build from scratch, which is what a reproducibility check wants
#                   (any value other than empty or 0)
#   CACHE_DIR=<dir> import and export the BuildKit layer cache as a local directory,
#                   one subdirectory per image (CI keeps it between runs). The cache
#                   is never a build input and never changes a digest.
#   BUILDER=<name>  buildx docker-container builder to use or create (default crucible-images)
#   DOCKER=<command> Docker CLI command or rootless service-user wrapper (default docker)
#
# Two kinds of image directory:
#   worker/          carries several harnesses. Each is an `ARG HARNESS_<NAME>_VERSION`
#                    in its Dockerfile; the image is tagged
#                    crucible-worker:<YYYYMMDD>-<build>, the date being the UTC day of
#                    SOURCE_DATE_EPOCH (C11: one image, not one per harness).
#   script-harness/  carries one, named after the directory, with `ARG HARNESS_VERSION`;
#                    tagged crucible-worker:script-harness-<version>-<build>.
# <build> is the first 12 hex digits of the crucible.build_inputs hash: the sha256
# over pins.env, every file in the image directory, and this script. Same inputs,
# same tag, and (S7) same digest. Every harness an image carries is labelled
# crucible.harness.<name>.version, and crucible.harnesses lists them.
#
# Never pushes. Prints one line per image: tag, OCI manifest digest, docker
# image ID, size in bytes.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=${OUT:-$here/out}
builder=${BUILDER:-crucible-images}
read -r -a docker_cmd <<< "${DOCKER:-docker}"
images=("$@")
[ ${#images[@]} -gt 0 ] || images=(worker script-harness)
for image in "${images[@]}"; do
    [ -f "$here/$image/Dockerfile" ] || { echo "build.sh: no Dockerfile for image '$image'" >&2; exit 2; }
    # A file copied from the build context keeps the checkout's mode, which follows the
    # cloner's umask and is not a build input, so the same tag would get a different
    # digest on another machine. Every such COPY or ADD states its mode among its own
    # flags. Instructions are case-insensitive and may follow ONBUILD.
    unpinned=$(grep -n -i -E '^[[:space:]]*(onbuild[[:space:]]+)?(copy|add)([[:space:]]|$)' \
        "$here/$image/Dockerfile" | while IFS= read -r line; do
            flags=$(sed -E 's/^[0-9]+:[[:space:]]*([Oo][Nn][Bb][Uu][Ii][Ll][Dd][[:space:]]+)?[A-Za-z]+//' \
                <<<"$line" | grep -o -E '^([[:space:]]+--[^[:space:]]+)*' || true)
            grep -q -E -e '--(from|chmod)=' <<<"$flags" || printf '%s\n' "$line"
        done)
    [ -z "$unpinned" ] || {
        echo "build.sh: $image/Dockerfile copies from the build context without --chmod:" >&2
        echo "$unpinned" >&2
        exit 2
    }
done
no_cache=""
case "${NO_CACHE:-0}" in 0|"") ;; *) no_cache="--no-cache" ;; esac

# shellcheck disable=SC1091
. "$here/pins.env"
: "${BASE_IMAGE:?}" "${DEBIAN_SNAPSHOT:?}" "${GIT_VERSION:?}" "${CURL_VERSION:?}" \
  "${JQ_VERSION:?}" "${MAKE_VERSION:?}" "${CA_CERTIFICATES_VERSION:?}" "${LAB_CA_SHA256:?}" \
  "${SOURCE_DATE_EPOCH:?}" "${BUILDKIT_IMAGE:?}" "${UV_VERSION:?}" "${UV_SHA256:?}" \
  "${CPYTHON_VERSION:?}" "${CPYTHON_BUILD:?}" "${CPYTHON_SHA256:?}" \
  "${GITLEAKS_VERSION:?}" "${GITLEAKS_SHA256:?}" "${RIPGREP_VERSION:?}" \
  "${LIBGL1_VERSION:?}" "${LIBEGL1_VERSION:?}" "${LIBXKBCOMMON0_VERSION:?}" \
  "${LIBDBUS_1_3_VERSION:?}" "${LIBFONTCONFIG1_VERSION:?}" "${LIBFREETYPE6_VERSION:?}" \
  "${LIBGLIB2_0_0_VERSION:?}" "${LIBX11_6_VERSION:?}" "${LIBXCB1_VERSION:?}"

# rewrite-timestamp only clamps a file newer than SOURCE_DATE_EPOCH. A context file at
# or before it (a checkout from before an epoch bump) would keep its own mtime in the
# COPY layer, and a later RUN that sets the mtime to the epoch would no longer change
# it, so the layers would depend on when the machine checked out. Every context file
# must be newer, so every machine clamps it the same way.
stale=$(find "$here" -path "$here/out" -prune -o -type f ! -newermt "@$SOURCE_DATE_EPOCH" -print)
[ -z "$stale" ] || {
    echo "build.sh: these build-context files are not newer than SOURCE_DATE_EPOCH; touch them:" >&2
    echo "$stale" >&2
    exit 2
}

# The docker driver inside dockerd cannot write OCI output, and the OCI
# manifest digest is the one a registry would report, so builds run on a
# docker-container builder with a pinned BuildKit.
# An existing builder must run the pinned BuildKit, since the pin is part of
# the build inputs the tag is named after. A stale one is refused, not reused.
if info=$("${docker_cmd[@]}" buildx inspect "$builder" 2>/dev/null); then
    if ! grep -q -F "$BUILDKIT_IMAGE" <<<"$info"; then
        echo "build.sh: builder '$builder' exists but does not run $BUILDKIT_IMAGE; remove it (docker buildx rm $builder) or set BUILDER" >&2
        exit 2
    fi
else
    "${docker_cmd[@]}" buildx create --name "$builder" --driver docker-container \
        --driver-opt "image=$BUILDKIT_IMAGE" --bootstrap >/dev/null
fi

mkdir -p "$out"
created=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)

# images/manifest.env is the declared pin per image: the tag and manifest digest the
# last build here produced, and the harness versions the image carries. Every
# reproducible image carries the same SOURCE_DATE_EPOCH creation time, so nothing
# downstream may pick an image by "newest"; the e2e and live tiers, the release and
# the operator's contracts read this file instead (13, C5). The manifest is not a
# build input, so recording a build never changes its tag.
manifest="${MANIFEST:-$here/manifest.env}"
record_manifest() {
    local key tag digest carried tmp
    key=$(printf '%s' "$1" | tr 'a-z-' 'A-Z_')
    tag=$2; digest=$3; carried=$4
    tmp=$(mktemp)
    if [ -f "$manifest" ]; then
        grep -v -E "^${key}(_DIGEST|_HARNESSES)?=" "$manifest" > "$tmp" || true
    else
        printf '# Written by images/build.sh: the tag, OCI manifest digest and harness versions of\n# each image. The declared pin the tiers read; never chosen by creation time (13, C5).\n' > "$tmp"
    fi
    printf '%s=%s\n%s_DIGEST=%s\n%s_HARNESSES=%s\n' "$key" "$tag" "$key" "$digest" "$key" "$carried" >> "$tmp"
    { grep '^#' "$tmp"; grep -v '^#' "$tmp" | sort; } > "$manifest"
    rm -f "$tmp"
}

for image in "${images[@]}"; do
    dir="$here/$image"
    dockerfile="$dir/Dockerfile"

    # name:version per harness the image carries, sorted by name. A pin a later
    # stage redeclares must agree with the first, or the label would lie about one.
    carried=$(sed -n 's/^ARG HARNESS_\([A-Z0-9_]*\)_VERSION=\(.*\)$/\1:\2/p' "$dockerfile" \
        | while IFS=: read -r name version; do
            printf '%s:%s\n' "$(printf '%s' "$name" | tr 'A-Z' 'a-z')" "$version"
        done | sort -u)
    duplicate=$(cut -d: -f1 <<<"$carried" | uniq -d)
    [ -z "$duplicate" ] || { echo "build.sh: $dockerfile pins $duplicate to more than one version" >&2; exit 2; }
    single=$(sed -n 's/^ARG HARNESS_VERSION=//p' "$dockerfile" | head -n1)
    if [ -n "$carried" ] && [ -n "$single" ]; then
        echo "build.sh: $dockerfile declares both HARNESS_VERSION and HARNESS_<NAME>_VERSION" >&2
        exit 2
    elif [ -n "$single" ]; then
        carried="$image:$single"
        version="$image-$single"
    elif [ -n "$carried" ]; then
        version=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y%m%d)
    else
        echo "build.sh: $dockerfile declares no harness version" >&2
        exit 2
    fi
    labels=(--label "crucible.harnesses=$(cut -d: -f1 <<<"$carried" | paste -sd, -)")
    while IFS=: read -r name harness_version; do
        [ -n "$harness_version" ] || { echo "build.sh: $dockerfile has an empty version for $name" >&2; exit 2; }
        labels+=(--label "crucible.harness.$name.version=$harness_version")
    done <<<"$carried"
    carried=$(paste -sd, - <<<"$carried")

    inputs=$(
        {
            cat "$here/pins.env" "$here/build.sh"
            find "$dir" -maxdepth 1 -type f -print0 | sort -z | while IFS= read -r -d '' input; do
                printf '%s\0' "${input#"$here/"}"
                cat "$input"
            done
        } | sha256sum | cut -c1-64
    )
    build=${inputs:0:12}
    tag="crucible-worker:$version-$build"
    stem="$out/crucible-worker-$version-$build"
    cache=()
    if [ -n "${CACHE_DIR:-}" ]; then
        mkdir -p "$CACHE_DIR"
        [ ! -d "$CACHE_DIR/$image" ] || cache+=(--cache-from "type=local,src=$CACHE_DIR/$image")
        cache+=(--cache-to "type=local,dest=$CACHE_DIR/$image.new,mode=max")
    fi

    # shellcheck disable=SC2086
    "${docker_cmd[@]}" buildx --builder "$builder" build $no_cache --platform linux/amd64 \
        --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" \
        --build-arg "BASE_IMAGE=$BASE_IMAGE" \
        --build-arg "DEBIAN_SNAPSHOT=$DEBIAN_SNAPSHOT" \
        --build-arg "GIT_VERSION=$GIT_VERSION" \
        --build-arg "CURL_VERSION=$CURL_VERSION" \
        --build-arg "JQ_VERSION=$JQ_VERSION" \
        --build-arg "MAKE_VERSION=$MAKE_VERSION" \
        --build-arg "CA_CERTIFICATES_VERSION=$CA_CERTIFICATES_VERSION" \
        --build-arg "LAB_CA_SHA256=$LAB_CA_SHA256" \
        --build-arg "PYTHON3_VERSION=$PYTHON3_VERSION" \
        --build-arg "PYTHON3_VENV_VERSION=$PYTHON3_VENV_VERSION" \
        --build-arg "RIPGREP_VERSION=$RIPGREP_VERSION" \
        --build-arg "LIBGL1_VERSION=$LIBGL1_VERSION" \
        --build-arg "LIBEGL1_VERSION=$LIBEGL1_VERSION" \
        --build-arg "LIBXKBCOMMON0_VERSION=$LIBXKBCOMMON0_VERSION" \
        --build-arg "LIBDBUS_1_3_VERSION=$LIBDBUS_1_3_VERSION" \
        --build-arg "LIBFONTCONFIG1_VERSION=$LIBFONTCONFIG1_VERSION" \
        --build-arg "LIBFREETYPE6_VERSION=$LIBFREETYPE6_VERSION" \
        --build-arg "LIBGLIB2_0_0_VERSION=$LIBGLIB2_0_0_VERSION" \
        --build-arg "LIBX11_6_VERSION=$LIBX11_6_VERSION" \
        --build-arg "LIBXCB1_VERSION=$LIBXCB1_VERSION" \
        --build-arg "UV_VERSION=$UV_VERSION" \
        --build-arg "UV_SHA256=$UV_SHA256" \
        --build-arg "CPYTHON_VERSION=$CPYTHON_VERSION" \
        --build-arg "CPYTHON_BUILD=$CPYTHON_BUILD" \
        --build-arg "CPYTHON_SHA256=$CPYTHON_SHA256" \
        --build-arg "GITLEAKS_VERSION=$GITLEAKS_VERSION" \
        --build-arg "GITLEAKS_SHA256=$GITLEAKS_SHA256" \
        --label "org.opencontainers.image.version=$version-$build" \
        --label "org.opencontainers.image.created=$created" \
        --label "org.opencontainers.image.source=https://github.com/sentania-labs/crucible" \
        "${labels[@]}" \
        --label "crucible.build_inputs=sha256:$inputs" \
        ${cache[@]+"${cache[@]}"} \
        --provenance=false --sbom=false \
        --output "type=oci,rewrite-timestamp=true,dest=$stem.oci.tar" \
        --output "type=docker,rewrite-timestamp=true,dest=$stem.docker.tar" \
        -f "$dockerfile" -t "$tag" "$here"

    digest=$(tar -xOf "$stem.oci.tar" index.json | jq -r '.manifests[0].digest')
    config=$(tar -xOf "$stem.oci.tar" "blobs/sha256/${digest#sha256:}" | jq -r '.config.digest')
    "${docker_cmd[@]}" load -q -i "$stem.docker.tar" >/dev/null
    id=$("${docker_cmd[@]}" image inspect -f '{{.Id}}' "$tag")
    # The image the daemon now holds under the tag must be the one the OCI archive
    # describes: its ID is the config digest (the manifest digest on a containerd
    # image store), never something else loaded under the same name.
    if [ "$id" != "$config" ] && [ "$id" != "$digest" ]; then
        echo "build.sh: $tag loaded as $id, but the OCI archive describes $digest (config $config)" >&2
        exit 1
    fi
    size=$("${docker_cmd[@]}" image inspect -f '{{.Size}}' "$tag")
    printf '%s digest=%s id=%s size=%s\n' "$tag" "$digest" "$id" "$size"
    if [ -n "${CACHE_DIR:-}" ]; then
        # A local cache export only ever grows; replacing it keeps what the next run
        # imports to this build's layers.
        rm -rf "${CACHE_DIR:?}/$image"
        mv "$CACHE_DIR/$image.new" "$CACHE_DIR/$image"
    fi
    record_manifest "$image" "$tag" "$digest" "$carried"
done
