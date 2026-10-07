# 13. Local operation: Docker Compose, worker images, and the Docker security model

Crucible runs as a persistent local Docker service now and as a Kubernetes
service later. Foundry keeps operating through Claude Code, Codex, and AGY
sessions; nothing here assumes Foundry is a service.

## Services (`compose.yaml`)

| Service | Image | Role | Persistent |
|---|---|---|---|
| `postgres` | `postgres:16` pinned by digest | authoritative state | volume `crucible-pg` |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy` pinned by digest, or an equivalent | reduced Docker API surface | no |
| `egress-proxy` | a small CONNECT proxy image pinned by digest | worker egress allowlist | no |
| `publish-proxy` | the same CONNECT proxy image | publisher egress allowlist, narrower than the workers' | no |
| `crucible` | `ghcr.io/sentania-labs/crucible:<tag>` | api + supervisor (`serve --all`) | volume `crucible-artifacts` |

Workers, collectors, verifiers, and publishers are not Compose services.
They are containers Crucible creates through the proxy, labeled with the
attempt or job. Workers, collectors, and verifiers sit on the
`crucible-workers` network; the publisher sits on `crucible-publish`, its
own internal network with its own proxy, because the one container that
holds a GitHub credential must not reach the model endpoints the workers'
allowlist permits (23). `docker
compose down` does not remove running workers by design; `crucible admin
drain` does.

## The credential root is the operator's to create

Compose binds the four credential directories (`github` and one per harness)
into the `crucible` container from a configurable root. A bind mount whose
source path does not exist is not an error: the Docker daemon creates it, as
root, mode 0755. That is precisely what 12 says a credential directory must
never be, and because the service runs unprivileged and has to write a
refreshed auth file back into that directory after a run, a fresh deployment
that let the daemon create them would look healthy and fail on the first
sync-back.

So the credential root and each per-harness directory under it are created
before the stack starts, by the operator or by the deployment script, owned
by the Crucible service user and mode 0700. `tools/deploy/deploy_local.sh`
does exactly that and creates the layout and nothing else; a credential
itself enters only through `crucible admin credentials login` (25).

Crucible refuses at startup when a configured credential directory is
missing, is not owned by the service user, or is not mode 0700, and names the
directory and what is wrong with it. That refusal is specified here and is
the work item; it is not implemented yet, so today a wrongly created
directory is caught by the deployment script's own layout step or not at all.

**Login needs the harness CLI on the host.** The credential login flow of 25
drives the harness's own CLI in a pty, and by the image rule above each CLI
exists only in its worker image; the Crucible service image carries none of
them. The API form of login therefore cannot work on a normal deployment and
refuses with that reason, and login is a local-mode operation run where the
harness CLI is. Making the API form work anywhere means running the flow
inside the promoted worker image, the way the probe does, with the pty and
the operator's pasted code relayed through the daemon's attach stream; that
is a follow-up phase, not a fix.

Compose owns lifecycle: `restart: unless-stopped` on all four. Closing any
Foundry session touches none of them. Foundry detects Crucible down by
`GET /ready` failing and runs the configured start command (`docker compose
-f <path> up -d`), recorded as an event once the API is reachable. `/ready`
fails when the API cannot serve (database or schema); a supervisor in trouble
shows in the same response's `supervisor` check and on `GET /supervisor`,
and does not fail it (hades #190).

## Two modes

**Normal**: `docker compose up -d`. Everything in containers.

**Developer**: `docker compose --profile dev up -d` starts `postgres`, the
socket proxy, and the egress proxy only; `uv run crucible serve --all
--reload` runs on the host with `CRUCIBLE_DOCKER_HOST` pointing at the
proxy published on loopback only. Same API, same providers, same worker
containers. The artifact root is a host directory in this mode.

A `Makefile` wraps both: `make up`, `make dev`, `make down`, `make reset`
(down with volumes; the only sanctioned way to discard a development
database), `make lint`, `make test`, `make e2e`. CI calls the same
targets. `COMPOSE_PROFILES=full` in `.env` is what lets one compose file
serve both modes; `--profile dev` overrides it.

The egress proxy image must be able to log to the container's stdout;
`ubuntu/squid` cannot (S9), so the pinned choice is made in C3 with that
requirement.

## Worker images and harness version management

**One worker image carries every harness.** The operator decided on
2026-09-22 at 3:09 PM (C11): "let's go with one image, it'll make the tests
cheaper and easier in the long run." Before C11 there was one image per
harness; now `images/worker/Dockerfile` builds a single image with Claude
Code, Codex, AGY, Hermes and Qwen Code, each CLI at its own pinned version and each
downloaded by URL and verified against a sha256 computed when the pin was
taken. It is Debian slim, non-root `worker` (uid 1000), git, curl, jq,
make (the shipped policy's required checks start with it, hades #181),
ripgrep (Hermes's search tool runs it, and its grep fallback missed matches
under a relative root, hades #385), the lab root CA, the Python runtime and
hash-locked virtual environment Hermes needs, the five CLIs, the toolchain a
uv project's own checks call (`uv`,
CPython 3.12 as `python3.12` only, and `gitleaks`, each pinned in
`images/pins.env`; hades #184, ADR 0020), `crucible-report` (the worker's report checker, a
standard-library script that borrows the Hermes environment's PyYAML for its
own process, hades #215), the shared libraries Qt's offscreen platform loads
(hades #430: `libgl1`, `libegl1`, `libxkbcommon0`, `libdbus-1-3`,
`libfontconfig1`, `libfreetype6`, `libglib2.0-0`, `libx11-6` and `libxcb1`,
the Debian packages for what `ldd` reports missing for PySide6's
`libQt6Gui.so.6` and `libqoffscreen.so`, each pinned in `images/pins.env`
at the `DEBIAN_SNAPSHOT` version; no X server or xvfb, so a GUI test
suite runs with `QT_QPA_PLATFORM=offscreen`), and nothing else. No `gh`: workers have no GitHub
credential to use it with. Where a CLI needs a companion binary to work at
all, as Codex does for the 5.6 model family, the companion ships from the
same pinned release, fetched and verified the same way, with its mtime set
to the epoch (07, S11). Each adapter launches its CLI by absolute path
inside the shared image, and Hermes's virtual environment is on PATH for
the Hermes process only. The e2e image, `images/script-harness`, stays
separate: it exists for CI.

Qwen Code 0.25.0 is the bundled `@qwen-code/qwen-code` npm tarball, pinned by
version and SHA256 in its self-contained `fetch-qwen-code` stage. Node 22.16.0
and its bundled npm are pinned by version and SHA256 there and in `pins.env`;
Node's checksum is published in its [release SHASUMS](https://nodejs.org/dist/v22.16.0/SHASUMS256.txt).
The npm archive checksum is recorded by the
[Qwen 0.25.0 package manifest](https://github.com/amrkmn/homebrew-tap/blob/3191e2bd3ba50b2068e1158a0217067db2f26dd0/Formula/qwen-code.rb);
CI verifies the download against it. No optional native npm addons are resolved. The wrapper
selects child_process shell execution and system ripgrep. Node and npm also let
repositories run their JavaScript checks (#288); these are available names in
`repository.required_programs`. See spec 07 for Qwen's loop-cap and context-window
settings and their routing override.

Tag: `crucible-worker:<YYYYMMDD>-<build>` for the worker image, where the
date is the UTC day of `SOURCE_DATE_EPOCH` (the pinned inputs' instant) and
`<build>` is the first twelve hex digits of the build-input hash, so any
changed pin is a new tag; `crucible-worker:script-harness-<version>-<build>`
for the e2e image. Labels: `org.opencontainers.image.version`,
`crucible.harnesses` (the comma-separated list of harnesses the image
carries), `crucible.harness.<name>.version` for each of them (read by
`images/build.sh` from the Dockerfile's `ARG HARNESS_<NAME>_VERSION` lines),
and `crucible.build_inputs` (hash). An image built before C11 carries
`crucible.harness` and `crucible.harness_version` instead and is still read.
Reproducible, byte for byte across machines: pinned base digest, pinned
package versions, a pinned BuildKit, `SOURCE_DATE_EPOCH`, and a stated
`--chmod` on every file copied from the build context (a copied file
otherwise keeps the mode of the checkout, which follows the cloner's umask
and is not a build input). Two builds of the same inputs on different
machines produce the same OCI manifest digest; the CI `images` job checks
the digest in `images/manifest.env` on every pull request, and the release
publishes only a build that reproduces it. Project-specific toolchains come from a per-project
image the task contract names, built `FROM` the worker image; the
provider's image allowlist controls what may run.

Rules:

- Harness CLIs never update themselves inside a running worker. The image
  sets each CLI's auto-update opt-out and the root filesystem is
  read-only, so an update cannot land even if attempted. Found in S7:
  Claude Code honors `DISABLE_AUTOUPDATER=1` and `DISABLE_UPDATES=1`; AGY
  honors `AGY_CLI_DISABLE_AUTO_UPDATE=1`; Codex has no environment
  variable, only the config key `check_for_update_on_startup=false`, which
  the Codex adapter passes at launch. S11 re-confirms on each promotion.
- Every image is a **declared pin**, never "the newest". Every
  reproducible image carries the same `SOURCE_DATE_EPOCH` creation time, so
  two tags of one image tie and a choice by creation time is arbitrary;
  a live run picked a stale tag exactly that way. `images/build.sh` writes
  `images/manifest.env` with, per image, the tag, the OCI manifest digest
  and the harness versions it carries (`WORKER`, `WORKER_DIGEST`,
  `WORKER_HARNESSES`, and the same three for `SCRIPT_HARNESS`); the e2e and
  live tiers and the release read it. Recording is not a build input, so
  writing the manifest never changes a tag. `make lint` and `make e2e`
  execute `images/check-manifest.sh`, which runs the tag and label
  calculation in `build.sh` without building and refuses a stale tag or
  harness list, and the unit tier holds every recorded harness version
  inside its adapter's tested range. A launch is refused when more than one
  tag matches and the manifest pins none.
- An image is launched with a harness's credential only when its
  `crucible.harnesses` label lists the requested harness and its
  `crucible.harness.<name>.version` label is inside the adapter's tested
  range (07). No list, a list without the harness, or no version label for
  it is a refusal before anything is seeded.
- Every attempt records the image digest it ran, resolved at launch.
  Retries and corrections of a task keep that digest unless Foundry
  explicitly authorizes a different image in the correction contract.
- Each adapter declares its tested version range. `GET /harnesses` reports
  installed versions (from image labels of allowlisted images) and the
  supported range; a launch with an unsupported combination is refused
  with a wake, never a warning.
- `make images` builds both images (C11, FDY-0072). The rootless daemon's
  service user cannot read a checkout under the operator's home, so the
  target copies `images/` to a scratch directory it can read, runs
  `build.sh` there, and writes `images/manifest.env` back. `images/manifest.env`
  is the authoritative record of the current tags; this document does not
  copy them.
- CI's `images` job runs the same script as `make images-check`: it builds
  both images from the pinned inputs on a fresh runner and fails if any tag,
  harness version or OCI digest differs from `images/manifest.env`, with one
  exception on a branch, below. It then
  runs `make images-policy-check`, which fails when the program a shipped
  policy's required check starts with (`make` for default-software), or a
  program a shipped policy declares in `repository.required_programs`, does
  not resolve in the worker image (hades #181, #184), and
  `make images-qt-offscreen-check` (`tools/images/qt_offscreen.sh`), which
  installs PySide6 into a venv inside the worker image and fails unless
  `QApplication([])` exits 0 there with `QT_QPA_PLATFORM=offscreen`, as uid
  1000 on a read-only root with no network (hades #430). Before starting Qt, it
  runs `ldd` separately on `libQt6Gui.so.6` and `libqoffscreen.so`, logs each
  dependency list, and fails on either an `ldd` error or an unresolved library.
  PySide6 6.11.2 links no xcb helpers through these two libraries; `libxcb1`
  is needed through `libX11`, while xcb helpers belong to the xcb platform
  plugin. Pull requests import a BuildKit
  layer cache; every push to main builds from scratch before exporting it.
- **CI owns the digest lines** (`WORKER_DIGEST`, `SCRIPT_HARNESS_DIGEST`,
  every `*_DIGEST` line of `images/manifest.env`; the operator's decision,
  2026-10-04, FDY-0310). Humans and workers never edit them: a change under
  `images/` updates the tag and harness lines (`images/check-manifest.sh`
  names the tag `make lint` expects) and leaves the digest lines as they are.
  When the `images` job on a branch builds every tag and harness version the
  manifest declares but a different digest, CI commits the built digests to
  the branch as `github-actions[bot]` (one commit that touches only those
  lines, its message naming the images and carrying the trailer
  `Crucible-Images-Digest: ci`), pushes it, and dispatches CI on the new head
  (a push made with the workflow token starts no run by itself). A tag or
  harness version that differs is a build-input mismatch and still fails the
  job. The job never writes back on main, which fails as before; CI does not
  run on tags, and the release's `make images-check` never writes back. When
  the head is the bot's own digest commit nothing writes back again, so a
  digest that does not reproduce twice fails instead of looping.
- **No branch code holds the write token** (the Codex finding on PR 420).
  The commit is split across two workflows. The `images` job in `ci.yml`
  runs the branch's own Makefile, `images.sh` and
  `tools/images/digest_commit.py`, so it keeps the workflow's read-only
  token: with `make images-check DIGEST_WRITEBACK=1` a digest-only
  difference passes, and the job uploads the built `*_DIGEST` lines as the
  `images-digests` artifact (one `digests.env`, retained a day). The
  privileged half is `.github/workflows/images-digest.yml`, started by
  `workflow_run` when a CI run completes; GitHub always runs that file from
  the default branch, so a branch cannot change it. It holds
  `contents: write` and `actions: write`, and the only code it executes is
  the default branch's `tools/images/digest_commit.py`, checked out by ref:
  `may-commit` repeats the main, tag, event and loop checks on the
  triggering run, and `commit-artifact` treats the artifact as untrusted,
  requires exactly the manifest's digest keys with `sha256:` values, applies
  them to the branch's manifest as read from the commit CI built, and
  refuses unless the result differs in digest values alone. The branch
  checkout is data, never imported or run. The push is not forced and is
  skipped when the branch has moved past the commit CI built.
- The release publishes both to `ghcr.io/sentania-labs/crucible-worker`
  with `docker push`, the way the service image and every ScarGuard service
  are pushed (the operator's decision, 2026-09-23; 24): it builds from
  scratch with `make images-check`, refuses to push unless the build
  reproduces the manifest, and never pushes over an existing version tag
  built from other inputs. `docker push` re-encodes the layers, so the
  registry digest differs from the manifest's local OCI digest; nothing
  compares the two, and the release notes carry the registry's.
  A cluster resolves the worker image there (`kubernetes.image_repositories`,
  26); a compose deployment uses the local daemon's `crucible-worker` images.
- **The worker image follows accepted practice for application images, the
  same as the Crucible service image and ScarGuard's services** (the
  operator's decision, 2026-09-23). Every release tags the worker image
  `crucible-worker:<version>` and the script-harness image
  `crucible-worker:script-harness-<version>`, `<version>` being the Crucible
  release version, never the fingerprint tag (24). `latest` and
  `script-harness-latest` move only when that version is the highest one
  published, the same rule the Crucible service image's release step
  applies; a lower version released after a higher one, or an old release
  job re-run, leaves them where they are. The fingerprint tag stays the
  local build's own name and `images/manifest.env`'s record; it is never
  pushed. Example manifests and the runbook's placeholder comments say
  `latest`; a real deployment pins the exact tag and digest the release
  body gives, the same as it pins the service image.
- `build.sh` is itself a hashed build input, so editing it retags every
  image; the previous tags stay on the daemon and in the registry as the
  rollback. The CI lint job calls the same `make lint` definition as a
  local run, so drift is rejected in both.

Image promotion (a Crucible repository process, C8):

1. Renovate detects a new harness version on a weekly schedule and opens a
   dependency-update PR. No automatic promotion.
2. CI builds a candidate image with pinned inputs and records its digest.
3. Adapter contract tests run against the candidate.
4. A bounded live subscription-authenticated canary task runs against it
   (`make e2e-live`), outside CI, results attached to the PR.
5. A person reviews changed flags, output shape, authentication behavior,
   and report parsing.
6. The supported-version declaration is updated in the same PR.
7. Merge and tag publish the image to GHCR with its digest.
8. Promotion is per harness (ADR 0018, the operator's decision of
   2026-09-25, crucible#116): `POST /admin/images/{digest}/promote` with a
   `harness` makes the image that harness's default, an explicit admin act
   recorded as an event. The image must carry that harness at a version
   inside its adapter's range; the other harnesses it carries are neither
   checked nor moved, so Hermes, AGY and Claude Code can each run a
   different image.
9. The image a harness's last promotion replaced is kept as its previous
   image. **Rollback is per harness**: `POST /admin/images/rollback` with a
   `harness` swaps its default and its previous image, and no other harness
   moves. (Until 2026-09-25 one promotion switched all four harnesses and a
   rollback moved all four together, C11.)

## Docker authority, stated honestly

Anything that can talk to the Docker socket is root-equivalent on that
daemon's host user. Crucible must create containers, so it needs some of
that authority. The model, in order of preference:

1. **Dedicated rootless Docker daemon for Crucible (preferred).** A
   service user runs its own `dockerd` in rootless mode; only that daemon's
   socket is proxied to Crucible. A full escape yields that unprivileged
   user, not root. Costs: one daemon to install, some feature loss (no
   privileged ports, slower overlay, cgroup limits depend on the host's
   cgroup v2 delegation). Spike S9 passed on the development workstation
   on 2026-09-16 (`docs/history/spikes/S9.md`): limits enforced, internal network
   egress only through the proxy, performance on par with rootful. This is
   the default local arrangement from C3; socket `/run/user/<uid>/docker.sock`
   of the `crucible` service user, proxied.
2. **Default socket through the proxy (temporary fallback).** If S9 fails,
   the host daemon's socket is proxied. A compromise of Crucible is then a
   compromise of the host. That risk is recorded in the readiness report
   and in ADR 0004, and revisited before any multi-user deployment.

In both arrangements:

- **Crucible never sees the raw socket.** Only `docker-socket-proxy` mounts
  it. Crucible talks HTTP to the proxy.
- **The proxy narrows the API surface, not the request bodies.** It allows
  `containers` (create, start, inspect, logs, stop, kill, remove, list),
  `images` (inspect, list), `networks` (inspect, connect), `volumes`
  (create, remove, inspect), and disables `exec`, `build`, `swarm`,
  `system`, `plugins`, `secrets`, `configs`. Note from S9: with the
  reference proxy image, `EXEC=0` refuses `exec` start and inspect but
  still lets `POST /containers/{id}/exec` create an exec instance; no
  command runs, but Crucible's client must not rely on the create call
  failing. It cannot reject a create request that asks for `Privileged`,
  a host namespace, or a bind of `/`.
  It is a tripwire against accidents and a reduction of surface, not a
  boundary against a compromised Crucible.
- **Create-request policy in Crucible** refuses to emit `Privileged`, host
  PID or network namespaces, any bind mount outside the artifact root and
  credential root, any capability add, or an image outside the allowlist.
  Unit-tested. Protects against Crucible bugs, not a hostile Crucible.
- **Workers, collectors, verifiers, and publishers never receive the socket
  or the proxy endpoint.** Verified by the isolation integration test (18).
- **Not carried to Kubernetes.** The Kubernetes provider (26) uses the API
  server with a namespaced ServiceAccount. Only the create-request policy
  survives, as a Pod spec policy enforced by Pod Security admission on the
  workers namespace; egress control becomes a per-attempt NetworkPolicy.

## Networking

Workers attach only to `crucible-workers`, a Docker network created with
`internal: true`: no default route, no reach to the Compose internal
network, `postgres`, `crucible`, or the socket proxy. Egress is provided by
`egress-proxy` (an HTTP CONNECT proxy with a hostname allowlist plus a
resolver) that sits on both `crucible-workers` and the outside; the
provider sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` in the container
environment. One egress proxy serves the deployment, so its allowlist is
deployment-wide: the union over every enabled harness of the adapter's
declared endpoints, the hostnames of every enabled local model
`endpoint_url` in the routing policy (05b), and the policy
`egress_allowlist`; Crucible refuses
to launch an attempt whose effective allowlist exceeds what the proxy
was configured with, and a per-attempt proxy is a later hardening. On this
provider a worker reaches an allowlisted host through those proxy variables
and only through them (the network has no default route), so a program that
ignores `HTTPS_PROXY` reaches nothing; the launch wrapper's egress probe
(26, hades #425) uses `curl`, which honours them, and so records the same
path the harness and its package installs take. Worker
`/tmp` is mounted without `noexec` in v0.x because no evidence exists yet
that the real harnesses never execute from it. C5a did not test it; the
test against each real harness is carried forward and this section is
updated with the result.

`make proxy-config` accepts routing policy JSON through
`ROUTING_POLICY_FILES`. Generation reads only entries whose `enabled` value is
true and whose `endpoint` is `local`; a configured but disabled local URL is not
authorized. Each enabled HTTP or HTTPS URL becomes an exact destination ACL and an
exact port ACL. The port is added to `Safe_ports`; an HTTPS port is also added to
`SSL_ports`. The allow line follows both unsafe-port denies and precedes the final deny.
After an operator uploads or selects a different routing policy, the proxy configuration
is atomically regenerated from that policy and the proxy is reloaded before an attempt
can use the route.

Each local routing entry is the pair `(harness, model)`. `model` is the exact name the
gateway returned for the stored key and the exact value the launch and credential probe
send. The supervisor repeats that authenticated listing on its normal schedule. A name
that disappears is pruned by this same reference for every harness, with its first
missing time retained for the Local gateway page and an orchestrator wake.

From S6, as each list stood after an authenticated task completed through
the filter (so none of them is provisional any more): Claude Code
`api.anthropic.com` (plus `mcp-proxy.anthropic.com` only if account MCP
connectors are wanted; telemetry to Datadog denied); Codex
`api.openai.com`, `auth.openai.com`, and `chatgpt.com`, which a
ChatGPT-plan login requires rather than merely prefers; AGY
`daily-cloudcode-pa.googleapis.com`, `oauth2.googleapis.com`,
`www.googleapis.com`, and `lh3.googleusercontent.com`, the last two for the
CLI's eligibility check, which fails closed without them. Everything
else each CLI tried (experiment flags, update checks, browser downloads,
`ab.chatgpt.com`, `antigravity-unleash.goog`, `play.googleapis.com`) is
denied, and the runs complete without them. The
publisher's allowlist is `github.com` and `api.github.com` only, and it is
a separate proxy on a separate network rather than an entry on the
workers' list. `network:
none` gives `--network none` and no proxy. Crucible never programs host
firewall rules.

## What the cluster deployment provides instead

Everything above is local operation. On Kubernetes the same service is the api
and supervisor Deployments of 26, and four of this document's arrangements have
a different shape there. `deploy/kubernetes` is the manifests and
`docs/deployment.md` is the runbook (C9).

| Compose here | Kubernetes there |
|---|---|
| the socket proxy, and the create-request policy behind it | no Docker socket at all; Pod Security admission at `restricted` on `crucible-workers` is the enforcement, and the namespaced ServiceAccount is the authority (26) |
| the egress proxy with a hostname allowlist | one NetworkPolicy per attempt per role that needs egress, over a namespace default deny (26) |
| the credential root, a directory per harness, created 0700 before start | one Secret per harness in `crucible-workers`, readable only by the supervisor's account; mount mode is chosen on Credentials |
| `crucible serve --all` in one container | `--api` and `--supervisor` in two Deployments, the supervisor at one replica with the same database lease |

The artifact root is the one thing that gets harder rather than simpler: it is
one named volume locally and has to be a `ReadWriteMany` claim there, because
the api serves what the supervisor wrote.

`deploy/kubernetes` is examples, not a deployable record: the base tracks `latest`
and a real deployment copies `overlays/lab` into the deployer's own GitOps
repository and pins there (24, docs/deployment.md). Nothing in this repository's
manifests is what a cluster ends up running.

## GitHub webhook ingress

Polling is the complete observation path and the local default; no
public route to the workstation is required or created (22, Q13).
`github.webhook_enabled` stays false locally. In Kubernetes the endpoint
sits behind the cluster's ingress with the webhook secret validating every
delivery in memory before anything is stored (23).

## Filesystem

Read-only root, tmpfs `/tmp` and `/home/worker` (size-limited), repo mount
rw, identity ro, the credential mount, report dir rw. Nothing else. The
credential mount is one writable mount plus the Crucible-owned template
files, each bind-mounted read-only at its own path **inside** the credential
directory, on top of the copy (12). A template is Crucible's own file from
the identity bundle, not a second credential: it is written into the bundle
before the bundle is hashed, so the identity hash covers it, and it is what
stops a worker planting a hook, an MCP server definition, or a plugin that a
later worker would inherit.

## Resource limits

From the policy (05b): CPU, memory, pids, tmpfs total, concurrency per
provider (3) and per harness (1).
