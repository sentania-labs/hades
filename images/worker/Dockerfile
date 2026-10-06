# Crucible worker image (spec 13, ADR 0011): one image carrying all four harness
# CLIs, Claude Code, Codex, AGY and Hermes, each at its pinned version. The operator
# chose one image over one per harness on 2026-09-22 at 3:09 PM (C11): one digest
# to build, publish, promote and roll back. Debian slim by digest, uid 1000 worker,
# git, curl, jq, make, ripgrep, the lab root CA, the Python runtime Hermes needs, the
# four CLIs, the toolchain a Python project's own checks call (uv, CPython 3.12 and
# gitleaks, hades #184, ADR 0020), and the shared libraries Qt's offscreen platform
# loads (hades #430: what ldd reports missing for PySide6's libQt6Gui.so.6 and
# libqoffscreen.so, so a GUI test suite runs with QT_QPA_PLATFORM=offscreen and no
# display server), nothing else. Shared pins mirror images/pins.env;
# build with images/build.sh. ripgrep is what Hermes's search tool runs; without it,
# its grep fallback missed every match under a relative root (hades #385), and the
# build fails unless `rg --version` runs.
#
# Every harness pin is an `ARG HARNESS_<NAME>_VERSION` here. build.sh reads those
# lines and labels the image `crucible.harness.<name>.version` for each, which is
# what the provider checks against the adapter's tested range before a launch.
ARG BASE_IMAGE=docker.io/library/debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867

# Claude Code: the release binary from Anthropic's release bucket (the version the
# npm registry's `latest` dist-tag named on 2026-09-22). HARNESS_CLAUDE_CODE_SHA256
# was computed over the downloaded file on 2026-09-22 and equals the checksum that
# release's manifest.json publishes.
FROM ${BASE_IMAGE} AS fetch-claude-code
ARG SOURCE_DATE_EPOCH
ARG HARNESS_CLAUDE_CODE_VERSION=2.1.280
ARG HARNESS_CLAUDE_CODE_SHA256=1e08503dbdf3c2cb0d706d32f3408277388d1c76ef108673e8fe42c1b322925b
ADD --checksum=sha256:${HARNESS_CLAUDE_CODE_SHA256} --chmod=0755 \
    https://downloads.claude.ai/claude-code-releases/${HARNESS_CLAUDE_CODE_VERSION}/linux-x64/claude \
    /fetch/claude
# ADD from a URL takes the file's mtime from the server's Last-Modified
# header, which rewrite-timestamp leaves alone when it is older than the
# epoch; a re-upload of the same bytes would change the layer. Pin it.
RUN touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/claude

# Codex: the static musl release binary from the GitHub release rust-v<version>,
# and the code-mode host, a companion binary the CLI spawns for the 5.6 model family
# (Luna, Terra, Sol) and fails closed without (C5, found live 2026-09-17). Both are
# assets of the same release; the CLI looks for the host beside itself. Both sha256
# values were computed over the downloaded tarballs on 2026-09-22 and equal the
# digests GitHub publishes for those release assets. Upstream's .sigstore files
# were not verified (no cosign on the reference workstation).
FROM ${BASE_IMAGE} AS fetch-codex
ARG SOURCE_DATE_EPOCH
ARG HARNESS_CODEX_VERSION=0.156.0
ARG HARNESS_CODEX_SHA256=3d49d9af25a5168cfc51e50e520ab238b23083c259ae7c14f89b007cb2545c7b
ARG CODE_MODE_HOST_SHA256=8383c79b0b6b4f77f01a1ca548fe1d550ad96a6e91b9fc1180bb69a449cae53b
ADD --checksum=sha256:${HARNESS_CODEX_SHA256} --chmod=0644 \
    https://github.com/openai/codex/releases/download/rust-v${HARNESS_CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz \
    /fetch/codex.tar.gz
ADD --checksum=sha256:${CODE_MODE_HOST_SHA256} --chmod=0644 \
    https://github.com/openai/codex/releases/download/rust-v${HARNESS_CODEX_VERSION}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz \
    /fetch/code-mode-host.tar.gz
# The extracted files keep the tarball entry mtime; pin them to the epoch so a
# repacked upstream tarball with the same bytes cannot change the layer.
RUN set -eu; \
    tar -xzf /fetch/codex.tar.gz -C /fetch codex-x86_64-unknown-linux-musl; \
    mv /fetch/codex-x86_64-unknown-linux-musl /fetch/codex; \
    chmod 0755 /fetch/codex; \
    touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/codex; \
    tar -xzf /fetch/code-mode-host.tar.gz -C /fetch codex-code-mode-host-x86_64-unknown-linux-musl; \
    mv /fetch/codex-code-mode-host-x86_64-unknown-linux-musl /fetch/codex-code-mode-host; \
    chmod 0755 /fetch/codex-code-mode-host; \
    touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/codex-code-mode-host

# AGY, the Antigravity CLI: not on any package registry. Its installer
# (https://antigravity.google/cli/install.sh) reads a per-platform manifest and
# downloads a versioned tarball from a public GCS bucket; the URL below is the one
# that manifest named for 1.2.8 on 2026-09-22 and it carries a build number, so it
# stays addressable after a newer release. HARNESS_AGY_SHA256 was computed over the
# downloaded tarball; the manifest publishes a sha512 of the same file, which
# matched:
#   28d0f8661316850d1e1ff541dc5f7727e5df6ea9f2d28a0c9b5567aa638a778b
#   21d9229123c65ee5b5c646d4390b589245b1fdc0004e98f0cd1774aa38147762
FROM ${BASE_IMAGE} AS fetch-agy
ARG SOURCE_DATE_EPOCH
ARG HARNESS_AGY_VERSION=1.2.8
ARG HARNESS_AGY_BUILD=4907747922280448
ARG HARNESS_AGY_SHA256=244752206d1f65c01aff489628f1df51f1a3fddacaa8ed74984661ebb6d09136
ADD --checksum=sha256:${HARNESS_AGY_SHA256} --chmod=0644 \
    https://storage.googleapis.com/antigravity-public/antigravity-cli/${HARNESS_AGY_VERSION}-${HARNESS_AGY_BUILD}/linux-x64/cli_linux_x64.tar.gz \
    /fetch/agy.tar.gz
RUN set -eu; \
    tar -xzf /fetch/agy.tar.gz -C /fetch antigravity; \
    mv /fetch/antigravity /fetch/agy; \
    chmod 0755 /fetch/agy; \
    touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/agy

# Hermes: the wheel from PyPI, by the sha256 PyPI publishes and that was recomputed
# over the download on 2026-09-22. Its dependencies are the hash-locked
# requirements.lock beside this file.
FROM ${BASE_IMAGE} AS fetch-hermes
ARG HARNESS_HERMES_VERSION=0.19.0
ARG HARNESS_HERMES_SHA256=bd0bac012aee38a60894781f4597dc29ee7bedb3448540249921f10d3bef327f
ADD --checksum=sha256:${HARNESS_HERMES_SHA256} --chmod=0644 \
    https://files.pythonhosted.org/packages/e5/30/c85be8290e9565dc3c7a9720e93f3e59e09b1b163487be4946c3aa848f80/hermes_agent-${HARNESS_HERMES_VERSION}-py3-none-any.whl \
    /fetch/hermes_agent-${HARNESS_HERMES_VERSION}-py3-none-any.whl

# The project toolchain (hades #184, ADR 0020): what a repository's `make lint`,
# `make test-unit` and `make scan` call when the repository is a uv project, which
# Crucible itself is. Each is its publisher's release archive, by the sha256 that
# publisher lists for it (uv's .sha256 file, python-build-standalone's SHA256SUMS,
# gitleaks' checksums.txt), recomputed over the download on 2026-09-28. The project's
# own dependencies are not here: uv fetches them hash-locked from PyPI at run time,
# under the project policy's egress allowlist (ADR 0020).
FROM ${BASE_IMAGE} AS fetch-toolchain
ARG SOURCE_DATE_EPOCH
ARG UV_VERSION=0.10.12
ARG UV_SHA256=ec72570c9d1f33021aa80b176d7baba390de2cfeb1abcbefca346d563bf17484
ARG CPYTHON_VERSION=3.12.13
ARG CPYTHON_BUILD=20260310
ARG CPYTHON_SHA256=7e08911171bd1ea2af43ae9d54917d9b2179d2cba4057057e0fd3c8ac1469368
ARG GITLEAKS_VERSION=8.30.1
ARG GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
ADD --checksum=sha256:${UV_SHA256} --chmod=0644 \
    https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz \
    /fetch/uv.tar.gz
ADD --checksum=sha256:${CPYTHON_SHA256} --chmod=0644 \
    https://github.com/astral-sh/python-build-standalone/releases/download/${CPYTHON_BUILD}/cpython-${CPYTHON_VERSION}%2B${CPYTHON_BUILD}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz \
    /fetch/cpython.tar.gz
ADD --checksum=sha256:${GITLEAKS_SHA256} --chmod=0644 \
    https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz \
    /fetch/gitleaks.tar.gz
# The archives' own entry mtimes are pinned by their sha256, so what is extracted from
# them is fixed; the two binaries moved into place are touched to the epoch like every
# other fetched binary, and nothing keeps the archive's owner.
RUN set -eu; \
    mkdir -p /fetch/bin /fetch/opt; \
    tar -xzf /fetch/uv.tar.gz -C /fetch --no-same-owner uv-x86_64-unknown-linux-gnu/uv; \
    mv /fetch/uv-x86_64-unknown-linux-gnu/uv /fetch/bin/uv; \
    tar -xzf /fetch/gitleaks.tar.gz -C /fetch/bin --no-same-owner gitleaks; \
    chmod 0755 /fetch/bin/uv /fetch/bin/gitleaks; \
    touch -h -d "@${SOURCE_DATE_EPOCH}" /fetch/bin/uv /fetch/bin/gitleaks; \
    tar -xzf /fetch/cpython.tar.gz -C /fetch/opt --no-same-owner; \
    test "$(/fetch/opt/python/bin/python3.12 -B -c 'import platform; print(platform.python_version())')" = "${CPYTHON_VERSION}"; \
    test "$(/fetch/bin/uv --version | cut -d' ' -f2)" = "${UV_VERSION}"; \
    test "$(/fetch/bin/gitleaks version)" = "${GITLEAKS_VERSION}"

FROM ${BASE_IMAGE}
# Declared here so it reaches the RUN below: useradd stamps /etc/shadow with
# the build day unless SOURCE_DATE_EPOCH is in its environment (S7).
ARG SOURCE_DATE_EPOCH
ARG HARNESS_HERMES_VERSION=0.19.0
ARG DEBIAN_SNAPSHOT=20260916T000000Z
ARG GIT_VERSION=1:2.39.5-0+deb12u3
ARG CURL_VERSION=7.88.1-10+deb12u15
ARG JQ_VERSION=1.6-2.1+deb12u2
ARG MAKE_VERSION=4.3-4.1
ARG CA_CERTIFICATES_VERSION=20250419~deb12u1
ARG LAB_CA_SHA256=567f110bb80bb2179b44995c78c7a49a4149085a59f6acb73cf37c6503a0481a
ARG PYTHON3_VERSION=3.11.2-1+b1
ARG PYTHON3_VENV_VERSION=3.11.2-1+b1
ARG RIPGREP_VERSION=13.0.0-4+b2
ARG LIBGL1_VERSION=1.6.0-1
ARG LIBEGL1_VERSION=1.6.0-1
ARG LIBXKBCOMMON0_VERSION=1.5.0-1
ARG LIBDBUS_1_3_VERSION=1.14.10-1~deb12u1
ARG LIBFONTCONFIG1_VERSION=2.14.1-4
ARG LIBFREETYPE6_VERSION=2.12.1+dfsg-5+deb12u4
ARG LIBGLIB2_0_0_VERSION=2.74.6-2+deb12u9
ARG LIBX11_6_VERSION=2:1.8.4-2+deb12u2
ARG LIBXCB1_VERSION=1.15-1
RUN set -eu; \
    printf '%s\n' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
        'Suites: bookworm bookworm-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
        'Suites: bookworm-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        > /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "git=${GIT_VERSION}" \
        "curl=${CURL_VERSION}" \
        "jq=${JQ_VERSION}" \
        "make=${MAKE_VERSION}" \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "python3=${PYTHON3_VERSION}" \
        "python3-venv=${PYTHON3_VENV_VERSION}" \
        "ripgrep=${RIPGREP_VERSION}" \
        "libgl1=${LIBGL1_VERSION}" \
        "libegl1=${LIBEGL1_VERSION}" \
        "libxkbcommon0=${LIBXKBCOMMON0_VERSION}" \
        "libdbus-1-3=${LIBDBUS_1_3_VERSION}" \
        "libfontconfig1=${LIBFONTCONFIG1_VERSION}" \
        "libfreetype6=${LIBFREETYPE6_VERSION}" \
        "libglib2.0-0=${LIBGLIB2_0_0_VERSION}" \
        "libx11-6=${LIBX11_6_VERSION}" \
        "libxcb1=${LIBXCB1_VERSION}"; \
    rg --version; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt /var/log/apt /var/log/dpkg.log /var/log/alternatives.log; \
    rm -f /var/cache/ldconfig/aux-cache; \
    groupadd --gid 1000 worker; \
    useradd --uid 1000 --gid 1000 --create-home --home-dir /home/worker --shell /bin/bash worker; \
    python3 -m venv /opt/hermes; \
    find / -xdev -type f -perm /6000 -exec chmod a-s {} +
# The snapshot sources stay in place on purpose: a per-project image built
# FROM this one that runs a plain apt-get update gets the same frozen
# package set. Check-Valid-Until: no lives in the stanzas, not on the
# command line, so that keeps working after the snapshot's Release
# metadata expires. Note that snapshot.debian.org is rate limited.
# Setuid and setgid bits are stripped from every binary (su, mount, passwd,
# ...) so a per-project image inherits none; the launch flags
# (--cap-drop ALL, no-new-privileges) are then belt and braces, not the
# only thing standing.
# --chmod on every COPY from the build context: without it the file keeps the mode of
# the checkout, which follows the cloner's umask (0664 under 002, 0644 under 022). The
# mode is in the layer but not in crucible.build_inputs, so two machines built the same
# tag with different digests (FDY-0082). build.sh refuses a context COPY without it.
COPY --chmod=0644 ca/sentania-lab-root.crt /usr/local/share/ca-certificates/sentania-lab-root.crt
RUN set -eu; \
    echo "${LAB_CA_SHA256}  /usr/local/share/ca-certificates/sentania-lab-root.crt" | sha256sum -c -; \
    update-ca-certificates
# The lock alone decides what goes into the Hermes venv (FDY-0115). --require-hashes
# checks the files the lock names, but without --no-deps pip still resolves each
# package's own requirements: hermes-agent 0.19.0 asks for uvicorn[standard]<1, pip
# treats the extra as a separate requirement, picks the newest uvicorn on the index,
# and refuses it for having no hash. That broke the v0.5.4 release the day uvicorn
# 0.54.0 appeared. --no-deps installs exactly the locked set; --only-binary :all:
# keeps pip from building an sdist, whose build backend would come from the index
# unhashed. pip check then catches a missing or wrong-version requirement, but it
# ignores the packages an extra pulls in (removing uvloop, there only for
# uvicorn[standard], passes it), so the guard that matters is the last one: the
# installed name==version list must equal the lock's. pip and setuptools are left out
# of that comparison: they come with the venv from Debian's pinned python3-venv, not
# from the index or the lock.
COPY --chmod=0644 worker/requirements.lock /tmp/requirements.lock
COPY --from=fetch-hermes /fetch/hermes_agent-${HARNESS_HERMES_VERSION}-py3-none-any.whl /fetch/hermes_agent-${HARNESS_HERMES_VERSION}-py3-none-any.whl
RUN set -eu; \
    /opt/hermes/bin/pip install --no-cache-dir --no-deps --only-binary :all: --require-hashes -r /tmp/requirements.lock; \
    /opt/hermes/bin/pip install --no-cache-dir --no-deps --force-reinstall /fetch/hermes_agent-${HARNESS_HERMES_VERSION}-py3-none-any.whl; \
    test "$(/opt/hermes/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("hermes-agent"))')" = "${HARNESS_HERMES_VERSION}"; \
    /opt/hermes/bin/pip check; \
    normalize() { awk -F'==' '{ n = tolower($1); gsub(/[-_.]+/, "-", n); print n "==" $2 }' | LC_ALL=C sort; }; \
    sed -n -E 's/^([A-Za-z0-9][A-Za-z0-9._-]*==[^ ;\\]+).*/\1/p' /tmp/requirements.lock | normalize > /tmp/locked; \
    /opt/hermes/bin/pip list --format=freeze --exclude pip --exclude setuptools | normalize > /tmp/installed; \
    test -s /tmp/locked; \
    diff -u /tmp/locked /tmp/installed; \
    rm -rf /root/.cache /tmp/requirements.lock /tmp/locked /tmp/installed /fetch
COPY --chmod=0755 worker/crucible-hermes.py /usr/local/bin/crucible-hermes
# FDY-0140: Hermes puts the directory of whichever `hermes` it finds first on the PATH
# of every command the model runs; with none on PATH it falls back to its own venv's
# bin, which would shadow the image's python3. This link makes that /usr/local/bin,
# already on PATH, so the prepend changes nothing.
RUN ln -s /opt/hermes/bin/hermes /usr/local/bin/hermes
# hades #215: the worker checks its own report.yaml before it exits 0. Standard library
# only; it re-executes under the Hermes venv's Python for PyYAML, by path, for its own
# process alone, so PATH is unchanged. No network.
COPY --chmod=0755 worker/crucible-report.py /usr/local/bin/crucible-report
COPY --chmod=0755 worker/crucible-codex-host.py /usr/local/bin/crucible-codex-host
COPY --from=fetch-claude-code /fetch/claude /usr/local/bin/claude
COPY --from=fetch-codex /fetch/codex /usr/local/bin/codex
COPY --from=fetch-codex /fetch/codex-code-mode-host /usr/local/bin/codex-code-mode-host
COPY --from=fetch-agy /fetch/agy /usr/local/bin/agy
# The project toolchain (hades #184). CPython 3.12 is on PATH only as `python3.12`, the
# name uv looks for when a project asks for 3.12; `python3` stays Debian's 3.11, so the
# Hermes wrapper and anything else that runs `python3` is unchanged.
COPY --from=fetch-toolchain /fetch/bin/uv /usr/local/bin/uv
COPY --from=fetch-toolchain /fetch/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=fetch-toolchain /fetch/opt/python /opt/python3.12
RUN ln -s /opt/python3.12/bin/python3.12 /usr/local/bin/python3.12

# Auto-update opt-outs, one per harness, all inert for the other three:
# - Claude Code: DISABLE_AUTOUPDATER stops background updates and DISABLE_UPDATES
#   also blocks a manual "claude update" (both in its environment variable reference).
# - AGY: AGY_CLI_DISABLE_AUTO_UPDATE is the variable the binary checks (found by
#   inspecting the binary; still present in 1.2.8).
# - Codex has no environment variable; the adapter passes
#   `-c check_for_update_on_startup=false` at launch.
# - Hermes 0.19.0 has no automatic update facility.
# The read-only root makes any self-update fail regardless (S7, S11).
# PATH stays Debian's default: the Hermes venv is not on it, so no command any model
# runs, Hermes's included, resolves `python3` into Hermes's dependencies. The Hermes
# adapter launches /usr/local/bin/crucible-hermes, which starts Hermes with the venv's
# own Python by path and adds nothing to PATH (FDY-0140).
# uv (hades #184): never download a Python, since no worker reaches the host it would
# come from and the image carries the one the project asks for; and copy rather than
# hardlink, because its cache (UV_CACHE_DIR, which the provider points at the
# workspace's package-cache mount, FDY-0140) and the project's .venv in the checkout
# are two mounts, where a hardlink always falls back with a warning.
ENV HOME=/home/worker \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    AGY_CLI_DISABLE_AUTO_UPDATE=1 \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
USER 1000:1000
WORKDIR /home/worker
