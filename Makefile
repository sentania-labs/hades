# Same targets locally and in CI (18). One definition, two callers.
#
# Crucible runs on the dedicated rootless daemon of the `crucible` service user
# (13, ADR 0004, S9). Point DOCKER at that daemon before running anything that
# creates containers. On the reference workstation the service user has no login
# shell, so the operator's wrapper is:
#
#   make e2e DOCKER='sudo -u crucible -H env HOME=/var/lib/crucible-docker \
#     XDG_RUNTIME_DIR=/run/user/999 DOCKER_HOST=unix:///run/user/999/docker.sock docker'
#
# CI has an ordinary rootful daemon and needs none of that: DOCKER stays `docker`.
#
# That service user cannot read a checkout under the operator's home (mode 750), so a
# build on its daemon cannot use images/ in place. `make images` (FDY-0072) copies
# images/ to a scratch directory it can read, builds the worker image and the
# script-harness image there with images/build.sh, and writes images/manifest.env
# back. CI and the release run the same script through `images-check`; on their
# rootful daemon the staging is merely harmless:
#
#   make images DOCKER='<the rootless daemon wrapper above>'
DOCKER ?= docker
COMPOSE ?= $(DOCKER) compose
# Normal local and CI boots build from the working tree. Release passes
# `--pull never` here after it has built and classified the candidate image.
COMPOSE_UP_FLAGS ?= --build
UV ?= uv

# The cluster budget `make manifests` checks the rendered requests against (issue 93).
# Left blank by default: tools/manifests/validate.sh falls back to the lab's own stated
# CPU shape for CRUCIBLE_CLUSTER_CPU_BUDGET, and enforces no memory budget at all until
# a deployment names its cluster's real memory.
CRUCIBLE_CLUSTER_CPU_BUDGET ?=
CRUCIBLE_CLUSTER_MEMORY_BUDGET_GI ?=

# The rootless daemon's socket, derived, never hardcoded: the uid differs per host (S9).
CRUCIBLE_UID := $(shell id -u crucible 2>/dev/null)
CRUCIBLE_DOCKER_SOCKET ?= $(if $(CRUCIBLE_UID),/run/user/$(CRUCIBLE_UID)/docker.sock,/var/run/docker.sock)
export CRUCIBLE_DOCKER_SOCKET

# The hostnames the egress proxy permits: the union of the policy's egress_allowlist
# and the adapters' declared model endpoints (13, S6). `proxy-config` writes the squid
# configuration from this list and Crucible refuses to launch an attempt that needs a
# host the running proxy does not permit.
EGRESS_ALLOWLIST ?= github.com objects.githubusercontent.com pypi.org files.pythonhosted.org registry.npmjs.org api.anthropic.com api.openai.com auth.openai.com chatgpt.com daily-cloudcode-pa.googleapis.com oauth2.googleapis.com www.googleapis.com lh3.googleusercontent.com
ROUTING_POLICY_FILES ?=
WORKERS_SUBNET ?= 10.88.0.0/24

# The publisher's own allowlist (23 step 3). It is deliberately not the workers' list:
# the one container that holds a GitHub credential reaches GitHub and nothing else.
PUBLISH_ALLOWLIST ?= github.com api.githubusercontent.com api.github.com
PUBLISH_SUBNET ?= 10.88.1.0/24

# Where `make deploy-local` runs a published release: a directory the `crucible`
# service user owns, because that user cannot read the operator's home (750) and so
# cannot run the normal-mode stack from a working tree under it. DEPLOY_TAG is an exact
# version, never `latest`: a deployment pins, and that is what makes a rollback one word.
# It is a deployment pin, not the package version, which still comes from the git tag
# (release.md). It does not follow a new release on its own: bump it, or pass DEPLOY_TAG.
CRUCIBLE_SERVICE_USER ?= crucible
CRUCIBLE_DEPLOY_DIR ?= /var/lib/crucible/deploy
CRUCIBLE_CREDENTIAL_ROOT ?= /var/lib/crucible/credentials
DEPLOY_TAG ?= 0.2.1
CRUCIBLE_DEPLOY_IMAGE ?= ghcr.io/sentania-labs/crucible:$(DEPLOY_TAG)
CRUCIBLE_DEPLOY_PORT ?= 8080

.PHONY: up dev down reset lint check-image-manifest scan scan-tree scan-history smoke test test-unit test-unit-in-image \
	test-integration e2e e2e-github e2e-live e2e-admin e2e-image build proxy-config proxies preflight \
	e2e-kind e2e-kind-self-hosting e2e-command-timeout registry-check manifests deploy-kind first-run-kind release-images-classify release-images-pull release-images-verify \
	deploy-local deploy-local-down images images-check images-policy-check release-notes

up: preflight proxy-config ## normal mode: postgres, proxies, migrate, crucible
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d $(COMPOSE_UP_FLAGS) --wait

dev: preflight proxy-config ## developer mode: postgres and the two proxies; run `uv run crucible serve --all` on the host
	@test -f .env || cp .env.example .env
	$(COMPOSE) -f compose.yaml -f compose.dev.yaml --profile dev up -d --wait

preflight: ## S9 follow-up 1: say what is wrong rather than letting compose be cryptic
	@test -n "$(CRUCIBLE_UID)" \
	  || echo "preflight: no 'crucible' service user, so this is ADR 0004's fallback: the host daemon behind the proxy. A compromise of Crucible is then a compromise of the host (13)."
	@test -S "$(CRUCIBLE_DOCKER_SOCKET)" \
	  || { command -v sudo >/dev/null && sudo -n test -S "$(CRUCIBLE_DOCKER_SOCKET)"; } \
	  || { echo "preflight: no docker socket at $(CRUCIBLE_DOCKER_SOCKET)"; exit 2; }
	@test -f /etc/apparmor.d/rootlesskit || echo "preflight: warning, /etc/apparmor.d/rootlesskit is missing; the rootless daemon will not start after a reboot on Ubuntu 24.04 (S9)"
	@$(DOCKER) info --format '{{range .SecurityOptions}}{{.}} {{end}}' 2>/dev/null | grep -q rootless \
	  || echo "preflight: warning, DOCKER is not pointed at a rootless daemon"
	@$(DOCKER) info --format '{{.CgroupVersion}}' 2>/dev/null | grep -q '^2$$' \
	  || echo "preflight: warning, cgroup v2 is what the memory and pids limits need (05b, S9)"

proxy-config: ## write the egress proxy's allowlist from EGRESS_ALLOWLIST (13, S6)
	@mkdir -p var/egress
	@python3 -m crucible.application.proxy_config \
	  --output var/egress/squid.conf --subnet "$(WORKERS_SUBNET)" \
	  $(foreach host,$(EGRESS_ALLOWLIST),--host $(host)) \
	  $(foreach policy,$(ROUTING_POLICY_FILES),--routing-policy "$(policy)")
	@{ \
	  echo "# Generated by 'make proxy-config'. The publisher's allowlist (23 step 3):"; \
	  echo "# the one container that holds a GitHub credential reaches GitHub and nothing else."; \
	  echo "acl publishers src $(PUBLISH_SUBNET)"; \
	  echo "acl SSL_ports port 443"; \
	  echo "acl Safe_ports port 443"; \
	  echo "acl CONNECT method CONNECT"; \
	  for host in $(PUBLISH_ALLOWLIST); do echo "acl allowed dstdomain $$host"; done; \
	  echo "http_access deny !Safe_ports"; \
	  echo "http_access deny CONNECT !SSL_ports"; \
	  echo "http_access allow publishers CONNECT allowed"; \
	  echo "http_access deny all"; \
	  echo "http_port 3128"; \
	  echo "cache deny all"; \
	  echo "access_log /var/log/squid/access.log"; \
	  echo "pid_filename none"; \
	  echo "shutdown_lifetime 1 second"; \
	} > var/egress/squid-publish.conf
	@echo "wrote var/egress/squid.conf from remote hosts and enabled local routing entries"
	@echo "wrote var/egress/squid-publish.conf with $(words $(PUBLISH_ALLOWLIST)) allowed hostname(s)"

proxies: preflight proxy-config ## bring up only the socket proxy and the egress proxy
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d docker-socket-proxy egress-proxy

down:
	$(COMPOSE) --profile "*" down

reset: ## DESTRUCTIVE: down plus postgres, artifact, and credential volumes
	$(COMPOSE) --profile "*" down --volumes

lint: check-image-manifest
	$(UV) sync --frozen --quiet
	$(UV) run ruff format --check crucible tests tools/release tools/smoke tools/registry tools/images
	$(UV) run ruff check crucible tests tools/release tools/smoke tools/registry tools/images
	$(UV) run mypy crucible tests tools/release tools/smoke tools/registry tools/images
	$(UV) run lint-imports

check-image-manifest: ## fail when a declared worker-image tag is stale
	images/check-manifest.sh

# The worker images (13, C11). One worker image carries Claude Code, Codex, AGY and
# Hermes; the script-harness image is the e2e tier's. Both targets build both from a
# staged copy of images/ (see the header) with images/build.sh. Neither pushes: the
# release pushes what images-check loaded with `docker push`
# (tools/release/push_worker_images.sh, 24).
#
# CACHE_DIR is a BuildKit layer cache directory that CI keeps between runs; it never
# changes a digest. NO_CACHE=1 builds from scratch. WORKER_REGISTRY is where the
# release publishes the worker images.
WORKER_REGISTRY ?= ghcr.io/sentania-labs/crucible-worker
CACHE_DIR ?=
NO_CACHE ?=
images: ## FDY-0072: build both images from a staged copy the daemon's user can read; writes images/manifest.env
	DOCKER="$(DOCKER)" CACHE_DIR="$(CACHE_DIR)" NO_CACHE="$(NO_CACHE)" tools/images/images.sh build

# DIGEST_WRITEBACK=1 (the CI images job on a branch only, FDY-0310) passes a build whose
# only difference is the *_DIGEST lines and writes those into images/manifest.env for the
# job to commit; a tag or harness version that differs still fails. Unset, as on main and
# in the release, any difference fails.
DIGEST_WRITEBACK ?=
images-check: ## build both images and fail if any tag, harness version or OCI digest differs from images/manifest.env
	DOCKER="$(DOCKER)" CACHE_DIR="$(CACHE_DIR)" NO_CACHE="$(NO_CACHE)" DIGEST_WRITEBACK="$(DIGEST_WRITEBACK)" tools/images/images.sh check

# hades #181: the program each shipped policy's required check starts with (`make`, for
# default-software's `make lint`, `make test`, `make scan`) must resolve in the worker
# image, so a policy and the image cannot disagree about it again; so must every program
# a shipped policy declares in `repository.required_programs` (hades #184, `uv` and
# `gitleaks` for hades-self-hosting). Probes the WORKER tag images/manifest.env
# declares, which `make images` or `make images-check` leaves in the daemon.
images-policy-check: ## fail when the worker image lacks a program a shipped policy's checks start with or declare
	$(UV) sync --frozen --quiet
	DOCKER="$(DOCKER)" $(UV) run python tools/images/policy_commands.py

# hades #184, FDY-0134: the unit tier as a Kubernetes worker Pod runs it, in the WORKER
# image images/manifest.env declares: uid 1000, read-only root, no network for the tests,
# and fsGroup-style setgid /tmp, home and workspace. A test that only passes on a plain
# host (a mode comparison on tmp_path, say) fails here. Needs the image in the daemon.
test-unit-in-image: ## the unit tier inside the worker image, as a worker Pod runs it; needs `make images` first
	DOCKER="$(DOCKER)" tools/images/unit_in_image.sh

# Reads the registry through DOCKER, so log it in first: reading a manifest back is
# still an authenticated registry call.
release-notes: ## release only: print the published service and worker image digests, read back from the registry, as release notes markdown
	@test -n "$(CRUCIBLE_IMAGE)" || { echo "set CRUCIBLE_IMAGE"; exit 2; }
	@DOCKER="$(DOCKER)" python3 tools/release/release_notes.py --service-image "$(CRUCIBLE_IMAGE)" \
	  --worker-repository "$(WORKER_REGISTRY)"

scan: scan-tree scan-history ## secret scan; needs gitleaks on PATH

scan-tree: ## every tracked file as it is in the working tree (caches and .venv excluded)
	@T=$$(mktemp -d) && git ls-files -z | tar --null -T - -cf - | tar -xf - -C "$$T" \
	  && gitleaks detect --no-git --redact --no-banner --source "$$T"; S=$$?; rm -rf "$$T"; exit $$S

# In a Crucible checkout (hades #184) origin/main is still there, with the origin URL a
# placeholder: the worker's clone has the remote-tracking ref from its preparer, and the
# verifier's tree is cloned from that checkout, whose local main becomes its origin/main.
# Nothing is fetched. A clone with no origin/main at all is refused by name rather than
# left to fail inside gitleaks' git log.
scan-history: ## commits in SCAN_RANGE (default origin/main..HEAD)
	@test -n "$${SCAN_RANGE:-}" || git rev-parse --verify --quiet origin/main >/dev/null \
	  || { echo "scan-history: no origin/main in this clone; set SCAN_RANGE" >&2; exit 2; }
	gitleaks detect --redact --no-banner --source . --log-opts="$${SCAN_RANGE:-origin/main..HEAD}"

smoke: ## drive one task end to end through a running stack; `make up` first
	@test -f .env || cp .env.example .env
	COMPOSE="$(COMPOSE)" $(if $(CRUCIBLE_IMAGE),CRUCIBLE_IMAGE="$(CRUCIBLE_IMAGE)") \
	  python3 tools/smoke/compose_smoke.py

release-images-classify: ## classify release candidate and supporting images without pulling
	@CRUCIBLE_IMAGE="$${CRUCIBLE_IMAGE:-ghcr.io/sentania-labs/crucible:classification-test}" \
	  POSTGRES_PASSWORD="$${POSTGRES_PASSWORD:-classification-only}" COMPOSE="$(COMPOSE)" \
	  DOCKER="$(DOCKER)" python3 tools/release/compose_images.py classify

release-images-pull: ## pull only images that do not resolve to CRUCIBLE_IMAGE
	@test -n "$(CRUCIBLE_IMAGE)" || { echo "set CRUCIBLE_IMAGE"; exit 2; }
	@COMPOSE="$(COMPOSE)" DOCKER="$(DOCKER)" python3 tools/release/compose_images.py pull-supporting

release-images-verify: ## prove every CRUCIBLE_IMAGE service container uses the local candidate
	@test -n "$(CRUCIBLE_IMAGE)" || { echo "set CRUCIBLE_IMAGE"; exit 2; }
	@COMPOSE="$(COMPOSE)" DOCKER="$(DOCKER)" python3 tools/release/compose_images.py verify-candidate

# The unit and integration tiers run in parallel, one pytest-xdist worker per CPU, and each
# integration worker owns its own database (issue 195). PYTEST_WORKERS=0 runs them serially
# in one process, for debugging: `make test PYTEST_WORKERS=0`. Every test has a time limit
# either way (issue 192, pyproject.toml).
PYTEST_WORKERS ?= auto
# The all-thread dump (faulthandler_timeout, pyproject.toml) fires after 180 s, a little
# past the default 120 s limit. The e2e, kind and live tiers set longer limits on their
# own cases, so their targets move the dump past those limits too, rather than dumping
# every thread of a slow but healthy case (issue 192).
E2E_DUMP_SECONDS ?= 660
KIND_DUMP_SECONDS ?= 960
LIVE_DUMP_SECONDS ?= 3660

test: test-unit test-integration

test-unit:
	$(UV) sync --frozen --quiet
	$(UV) run pytest tests/unit -q -n $(PYTEST_WORKERS)

test-integration: ## needs Docker for postgres:16 (testcontainers) or CRUCIBLE_TEST_DATABASE_URL
	$(UV) sync --frozen --quiet
	$(UV) run pytest tests/integration -q -m integration -n $(PYTEST_WORKERS)

e2e-image: ## build the e2e worker image (18) on whichever daemon DOCKER names
	DOCKER_HOST=$${DOCKER_HOST:-} images/build.sh script-harness

e2e: check-image-manifest ## the Docker-provider end-to-end tier (18): real containers, no model
	$(UV) sync --frozen --quiet
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	$(UV) run pytest tests/e2e -q -m e2e -o faulthandler_timeout=$(E2E_DUMP_SECONDS)

e2e-kind: check-image-manifest ## Kubernetes-provider e2e on a disposable kind cluster (18, 26)
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_E2E_KIND_PYTEST_ARGS="-o faulthandler_timeout=$(KIND_DUMP_SECONDS) $${CRUCIBLE_E2E_KIND_PYTEST_ARGS:-}" \
	tools/kind/e2e-kind.sh

# hades #184: the self-hosting proof on the same disposable cluster. A Hermes task under
# examples/policies/hades-self-hosting.yaml, against a bare copy of this repository at
# HEAD, in the combined worker image images/manifest.env pins (`make images` first),
# with the real per-worker NetworkPolicy and PyPI reached through it. The model is a
# stub Pod; the verifier runs `make lint`, `make test-unit` and `make scan`. Needs the
# network (PyPI), about 8 GiB free, and a committed HEAD. Local only, not in CI.
e2e-kind-self-hosting: check-image-manifest ## the repository's own checks in the worker image, on kind (hades #184)
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_E2E_KIND_WORKER_IMAGE="$$(awk -F= '$$1 == "WORKER" {print $$2}' images/manifest.env)" \
	CRUCIBLE_E2E_KIND_TESTS=tests/e2e/test_kind_self_hosting.py \
	CRUCIBLE_E2E_KIND_PYTEST_ARGS="-s -o faulthandler_timeout=$(KIND_DUMP_SECONDS) $${CRUCIBLE_E2E_KIND_PYTEST_ARGS:-}" \
	tools/kind/e2e-kind.sh

# The command-timeout tier (issue 128): Claude Code, Codex and Hermes from the pinned
# worker image, each driven by a stub model server on loopback under `--network none`,
# with a scripted command longer than a small configured timeout. No login, no
# credential, no network; it needs the worker image (`make images`). The trap is
# reproduced without the launch setting, then the setting shown to end or wait out the
# command, and each report directory classified by the adapter.
e2e-command-timeout: check-image-manifest ## real harnesses against a stub model: the launch-set command timeout (128)
	$(UV) sync --frozen --quiet
	CRUCIBLE_E2E_COMMAND_TIMEOUT=1 CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	$(UV) run pytest tests/e2e/test_command_timeout.py -q -m e2e_command_timeout -o faulthandler_timeout=$(E2E_DUMP_SECONDS)

# The registry adapter with the real crane the service image ships (108): a stub registry
# that redirects its blobs to another host and demands a password, then an anonymous
# resolve of the published worker image on GHCR. Needs the network, no Docker, no secret.
REGISTRY_CHECK_REFERENCE ?= ghcr.io/sentania-labs/crucible-worker:latest
registry-check: ## resolve through the real crane: a redirecting stub registry, then the published worker image
	$(UV) sync --frozen --quiet
	@crane=$$(tools/crane/fetch.sh) && export PATH="$$(dirname "$$crane"):$$PATH" \
	  && CRUCIBLE_E2E_REGISTRY=1 $(UV) run pytest tests/e2e/test_registry.py -q -m e2e_registry \
	  && $(UV) run python tools/registry/check_published.py --reference "$(REGISTRY_CHECK_REFERENCE)"

# CRUCIBLE_CLUSTER_CPU_BUDGET and CRUCIBLE_CLUSTER_MEMORY_BUDGET_GI (issue 93): the
# cluster's CPU and memory ceiling the render-time resource check refuses to exceed.
# CPU defaults inside validate.sh to the lab's own stated shape; memory has no such
# default because no cluster memory fact lives in this repository.
manifests: ## render deploy/kubernetes and validate every object; needs kubectl and kubeconform
	CRUCIBLE_CLUSTER_CPU_BUDGET="$(CRUCIBLE_CLUSTER_CPU_BUDGET)" \
	CRUCIBLE_CLUSTER_MEMORY_BUDGET_GI="$(CRUCIBLE_CLUSTER_MEMORY_BUDGET_GI)" \
	UV="$(UV)" tools/manifests/validate.sh

# Bring the deployment manifests up on a disposable kind cluster and run one task through
# the deployed API on the Kubernetes provider (C9, sdlc skill step 3). This is the
# author's half of "done means seen working": it proves the manifests and the image, and
# it deliberately proves nothing cluster-specific (github-ci skill).
#
deploy-kind: check-image-manifest ## deploy the manifests on a disposable kind cluster and run one task
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	  UV="$(UV)" tools/kind/deploy-kind.sh

# The first-run path on a disposable kind cluster (crucible#119, #120, #121, #123, #79):
# the same deployment as deploy-kind, then gateway URL and key, a model picked from the
# gateway, a stand-in GitHub App connected and a repository picked, and Status reading
# ready for Hermes; then a private repository picked and prepared from a git stand-in
# that demands the read-only checkout token, with the worker shown unable to read it
# (crucible#157, ADR 0019). Stand-ins only: no real model and no real GitHub API. Needs
# the combined worker image images/manifest.env pins on the host daemon (`make images`).
first-run-kind: check-image-manifest ## deploy on a disposable kind cluster and walk the first-run setup
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" CRUCIBLE_DEPLOY_KIND_FIRST_RUN=1 \
	  UV="$(UV)" tools/kind/deploy-kind.sh

# The live GitHub tier (23). Local only, never in CI: it mints a real installation token
# from the mounted App key and opens a real pull request on one throwaway repository,
# then deletes every branch and closes every pull request it created. It never touches
# the default branch. The three variables below name files and a repository; no key,
# token, or secret is ever a value here.
#
#   make e2e-github \
#     CRUCIBLE_GITHUB_APP_JSON=~/path/to/app.json \
#     CRUCIBLE_GITHUB_APP_KEY=~/path/to/app.pem \
#     CRUCIBLE_GITHUB_TARGET_REPO=owner/throwaway
e2e-github: ## the live GitHub tier: a real App against a throwaway repository
	$(UV) sync --frozen --quiet
	@test -n "$(CRUCIBLE_GITHUB_APP_JSON)" || { echo "set CRUCIBLE_GITHUB_APP_JSON"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_APP_KEY)" || { echo "set CRUCIBLE_GITHUB_APP_KEY"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_TARGET_REPO)" || { echo "set CRUCIBLE_GITHUB_TARGET_REPO"; exit 2; }
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_GITHUB_APP_JSON="$(CRUCIBLE_GITHUB_APP_JSON)" \
	CRUCIBLE_GITHUB_APP_KEY="$(CRUCIBLE_GITHUB_APP_KEY)" \
	CRUCIBLE_GITHUB_TARGET_REPO="$(CRUCIBLE_GITHUB_TARGET_REPO)" \
	$(UV) run pytest tests/e2e -q -m e2e_github -o faulthandler_timeout=$(LIVE_DUMP_SECONDS)

# The live harness tier (07, 12, 18). Local only, never in CI: it runs the real Claude
# Code, Codex and AGY images with the dedicated Crucible credentials (never the
# operator's daily-use directories) on the rootless daemon, one harness at a time, and
# publishes a trivial change to the throwaway repository, then closes the pull request
# and deletes the branch. Every variable names a directory, a file, a repository or a
# harness; no key, token or secret is ever a value here.
#
#   make e2e-live HARNESS=claude_code \
#     CRUCIBLE_LIVE_CREDENTIAL_ROOT=/path/to/dedicated/credentials \
#     CRUCIBLE_GITHUB_APP_JSON=~/path/to/app.json \
#     CRUCIBLE_GITHUB_APP_KEY=~/path/to/app.pem \
#     CRUCIBLE_GITHUB_TARGET_REPO=owner/throwaway \
#     DOCKER='<the rootless daemon wrapper above>'
HARNESS ?= all
e2e-live: ## the live harness tier: real harness images, the dedicated credentials, a throwaway repository
	$(UV) sync --frozen --quiet
	@test -n "$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" || { echo "set CRUCIBLE_LIVE_CREDENTIAL_ROOT"; exit 2; }
	@if [ "$(HARNESS)" = "hermes" ] || [ "$(HARNESS)" = "all" ]; then test -n "$(or $(CRUCIBLE_LOCAL_ENDPOINT_URL),$(CRUCIBLE_SPARK_ENDPOINT_URL))" || { echo "set CRUCIBLE_LOCAL_ENDPOINT_URL"; exit 2; }; fi
	@test -n "$(CRUCIBLE_GITHUB_APP_JSON)" || { echo "set CRUCIBLE_GITHUB_APP_JSON"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_APP_KEY)" || { echo "set CRUCIBLE_GITHUB_APP_KEY"; exit 2; }
	@test -n "$(CRUCIBLE_GITHUB_TARGET_REPO)" || { echo "set CRUCIBLE_GITHUB_TARGET_REPO"; exit 2; }
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_LIVE_CREDENTIAL_ROOT="$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" \
	CRUCIBLE_LIVE_HARNESSES="$(HARNESS)" \
	CRUCIBLE_LIVE_MODELS='$(CRUCIBLE_LIVE_MODELS)' \
	CRUCIBLE_LIVE_IMAGES='$(CRUCIBLE_LIVE_IMAGES)' \
	CRUCIBLE_LIVE_AGY_MOUNT_MODE="$(CRUCIBLE_LIVE_AGY_MOUNT_MODE)" \
	CRUCIBLE_LIVE_REPORT="$(CRUCIBLE_LIVE_REPORT)" \
	CRUCIBLE_LOCAL_ENDPOINT_URL="$(or $(CRUCIBLE_LOCAL_ENDPOINT_URL),$(CRUCIBLE_SPARK_ENDPOINT_URL))" \
	CRUCIBLE_GITHUB_APP_JSON="$(CRUCIBLE_GITHUB_APP_JSON)" \
	CRUCIBLE_GITHUB_APP_KEY="$(CRUCIBLE_GITHUB_APP_KEY)" \
	CRUCIBLE_GITHUB_TARGET_REPO="$(CRUCIBLE_GITHUB_TARGET_REPO)" \
	$(UV) run pytest tests/e2e -q -m e2e_live -s -o faulthandler_timeout=$(LIVE_DUMP_SECONDS)

# The live administration tier (25, C5b): every row of the operations table through
# `/v1/admin` and through `crucible-admin`, against a live stack on the daemon DOCKER
# names, and `credentials probe` with the dedicated credentials for each harness. Rotate
# and remove act on scratch copies inside the artifact root, never on the dedicated
# root. The GitHub variables are optional; without them `github check` is recorded as
# not configured. Again no key, token or secret is ever a value here.
#
#   make e2e-admin \
#     CRUCIBLE_LIVE_CREDENTIAL_ROOT=/path/to/dedicated/credentials \
#     DOCKER='<the rootless daemon wrapper above>'
e2e-admin: ## the live administration tier: API and CLI parity on a live stack, probes with the dedicated credentials
	$(UV) sync --frozen --quiet
	@test -n "$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" || { echo "set CRUCIBLE_LIVE_CREDENTIAL_ROOT"; exit 2; }
	CRUCIBLE_E2E_DOCKER="$(DOCKER)" \
	CRUCIBLE_E2E_DOCKER_SOCKET="$(CRUCIBLE_DOCKER_SOCKET)" \
	CRUCIBLE_LIVE_CREDENTIAL_ROOT="$(CRUCIBLE_LIVE_CREDENTIAL_ROOT)" \
	CRUCIBLE_LIVE_REPORT="$(CRUCIBLE_LIVE_REPORT)" \
	CRUCIBLE_GITHUB_APP_JSON="$(CRUCIBLE_GITHUB_APP_JSON)" \
	CRUCIBLE_GITHUB_APP_KEY="$(CRUCIBLE_GITHUB_APP_KEY)" \
	CRUCIBLE_GITHUB_TARGET_REPO="$(CRUCIBLE_GITHUB_TARGET_REPO)" \
	$(UV) run pytest tests/e2e -q -m e2e_admin -s -o faulthandler_timeout=$(LIVE_DUMP_SECONDS)

build:
	docker build -t crucible:dev .

deploy-local: proxy-config ## run a pinned published release on the rootless daemon from /var/lib/crucible/deploy
	CRUCIBLE_SERVICE_USER="$(CRUCIBLE_SERVICE_USER)" \
	CRUCIBLE_DEPLOY_DIR="$(CRUCIBLE_DEPLOY_DIR)" \
	CRUCIBLE_CREDENTIAL_ROOT="$(CRUCIBLE_CREDENTIAL_ROOT)" \
	CRUCIBLE_DEPLOY_IMAGE="$(CRUCIBLE_DEPLOY_IMAGE)" \
	CRUCIBLE_DEPLOY_PORT="$(CRUCIBLE_DEPLOY_PORT)" \
	CRUCIBLE_LOCAL_ENDPOINT_URL="$(or $(CRUCIBLE_LOCAL_ENDPOINT_URL),$(CRUCIBLE_SPARK_ENDPOINT_URL))" \
	CRUCIBLE_EGRESS_ALLOWLIST_HOSTS="$(EGRESS_ALLOWLIST)" \
	CRUCIBLE_WORKERS_SUBNET_CIDR="$(WORKERS_SUBNET)" \
	tools/deploy/deploy_local.sh up

deploy-local-down: ## stop the deployed stack; the postgres and artifact volumes are kept
	CRUCIBLE_SERVICE_USER="$(CRUCIBLE_SERVICE_USER)" \
	CRUCIBLE_DEPLOY_DIR="$(CRUCIBLE_DEPLOY_DIR)" \
	tools/deploy/deploy_local.sh down
