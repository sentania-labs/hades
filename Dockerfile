# Reproducible build: pinned base digest, pinned uv, locked dependencies.
FROM ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv

# crane (go-containerregistry) is the registry client the Kubernetes provider runs to
# resolve a worker image (108). Pinned by release version and by the SHA-256 of the
# release archive, which the build checks before extracting anything; a mismatch fails
# the build. tar keeps the archive's own mtime on the binary, so the file the final
# stage copies is the same bytes and timestamp on every build. tools/crane/fetch.sh
# reads these two lines, so tests run the same binary.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS crane
ARG CRANE_VERSION=0.22.1
ARG CRANE_SHA256=0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0
ADD https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz /tmp/crane.tar.gz
RUN echo "${CRANE_SHA256}  /tmp/crane.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/crane.tar.gz -C /usr/local/bin crane \
    && chmod 0755 /usr/local/bin/crane \
    && /usr/local/bin/crane version

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS build
COPY --from=uv /uv /usr/local/bin/uv
# .git is not in the build context, so hatch-vcs cannot read the tag itself.
# The caller passes it in: `docker build --build-arg VERSION=$(git describe ...)`.
# Unset, the package falls back to a dev version rather than failing the build.
ARG VERSION=0.0.0.dev0
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never SOURCE_DATE_EPOCH=0 \
    SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
# uv.lock covers [project].dependencies, but not [build-system].requires: without the
# build-backend group (pyproject.toml), uv would resolve hatchling and hatch-vcs from
# the index unhashed at image build time, and a new release of either could change or
# break this image the way uvicorn 0.54.0 broke the worker image (113). Installing the
# group here hash-verifies them from uv.lock, same as every other dependency.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group build-backend --no-install-project
COPY crucible ./crucible
# No cache mount here, and --no-cache: uv keys its built-wheel cache on the
# source tree and not on SETUPTOOLS_SCM_PRETEND_VERSION, so a warm cache would
# reinstall a wheel built for a previous VERSION and the package version would
# silently disagree with the image label. Dependencies are already installed by
# the sync above, so this step only builds the project and costs nothing.
# --no-build-isolation-package crucible builds the project with the hashed
# hatchling/hatch-vcs just installed, instead of letting uv fetch its own copy
# from the index; --no-editable is required alongside it, or hatchling's editable
# build looks for the (undeclared, unhashed) `editables` package. Leaving out
# --group build-backend here uninstalls it again, so the final venv matches
# uv.lock's default groups exactly. --network=none makes that a guarantee rather than
# an expectation: if this step ever tries to fetch anything, the build fails.
RUN --network=none uv sync --frozen --no-dev --no-cache --no-build-isolation-package crucible --no-editable

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG VERSION=0.0.0.dev0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="crucible" \
      org.opencontainers.image.description="A deterministic supervisor for AI coding workers." \
      org.opencontainers.image.source="https://github.com/sentania-labs/crucible" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
COPY images/ca/sentania-lab-root.crt /usr/local/share/ca-certificates/sentania-lab-root.crt
RUN echo "567f110bb80bb2179b44995c78c7a49a4149085a59f6acb73cf37c6503a0481a  /usr/local/share/ca-certificates/sentania-lab-root.crt" \
      | sha256sum -c - \
    && update-ca-certificates
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
RUN groupadd --gid 1000 crucible && useradd --uid 1000 --gid 1000 --create-home crucible \
    && mkdir -p /var/lib/crucible/artifacts && chown -R crucible:crucible /var/lib/crucible
COPY --from=crane /usr/local/bin/crane /usr/local/bin/crane
WORKDIR /app
COPY --from=build --chown=crucible:crucible /app /app
# hades #208: what a room runner is launched with. The service reads both from its
# working directory: the runner script it mounts into the runner's Pod or container, and
# the principal identity it puts at the top of every room's session start.
COPY --chown=crucible:crucible tools/room_runner.py /app/tools/room_runner.py
COPY --chown=crucible:crucible config/principal/IDENTITY.md /app/config/principal/IDENTITY.md
USER crucible
EXPOSE 8080
CMD ["crucible", "serve", "--all"]
