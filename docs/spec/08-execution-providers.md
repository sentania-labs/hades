# 08. Execution-provider contracts

A provider owns the environment an attempt runs in. Task, worker, execution,
event, and API contracts are identical across providers; only isolation
strength and mechanics differ. `GET /providers` reports each provider's
capabilities so Foundry can choose knowingly.

## Interface (`crucible/ports/execution.py`)

```python
class ExecutionProvider(Protocol):
    name: ProviderName  # "fake" | "docker" | "kubernetes" | "hostprocess"

    def capabilities(self) -> ProviderCapabilities: ...
    async def prepare(self, spec: LaunchSpec, checkout_token=None, cancelled=None) -> Workspace: ...
    async def launch(self, ws: Workspace, spec: LaunchSpec, cancelled=None) -> Handle: ...
    async def observe(self, h: Handle) -> Observation: ...  # running, exited(code), lost
    async def logs(self, h: Handle, since: Offset) -> AsyncIterator[LogChunk]: ...
    async def collect(self, h: Handle, ws: Workspace) -> CollectedOutputs: ...
    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None: ...
    async def cleanup(self, ws: Workspace, policy: CleanupPolicy) -> None: ...
    async def reconcile(self) -> list[Handle]: ...  # what is actually running
```

`cancelled` is an async check the supervisor passes (hades #189): `prepare`
asks it before each step and while one runs, `launch` asks it just before it
creates the worker, and a True answer stops the call with
`LaunchCancelledError` having created nothing further (16).

`ProviderCapabilities`: `isolation` (none, process, container, pod),
`network_control` (bool), `resource_limits` (bool), `shared_disk` (bool),
`supports_harnesses` (set), `max_concurrency`.

`Workspace`: the checkout location, the identity mount, the report dir, the
credential mounts, and the checkout lease ID. Two shapes, after Sandcastle:
`shared_disk` (host directory bind-mounted, cheap, Docker) and `isolated`
(provider copies in and out, Kubernetes with a volume, later microVM).

## Fake provider

Deterministic, no model, no containers. Scripted behaviors selected by a
field on the launch spec in tests: succeed with a given report, exit 75,
crash, hang until timeout, vanish (simulate loss), write outside allowed
paths, emit N log lines. Every lifecycle and gate test runs against it.
It is a test fixture: wired only when `test_fixtures` is on (18).

## Docker provider (local, v0.x)

- Talks to Docker through the socket proxy endpoint, never the raw socket
  (13). API version pinned.
- `prepare`: the supervisor takes the checkout lease (a fenced row the
  provider cannot write) before calling `prepare`. With the reference cache
  on (`use_reference_cache`, the default) and a remote repository, `prepare`
  first runs a throwaway **cache refresher** container
  (`crucible-cache-refresher-<attempt>`, label role `cache-refresher`): it
  mounts `<artifact_root>/cache` read-write and nothing else (no workspace,
  no identity bundle, no harness credential), runs on the preparer's network
  through the egress proxy, and fetches the repository's bare mirror
  `<cache>/<sha256(url)[:16]>.git` (or clones it when absent), giving the
  remote 20 seconds to answer a ref listing and otherwise leaving the mirror
  as it is (hades #191). It is the cache's only writer, and it is removed
  before the preparer is created. A refresh that fails is logged and the
  prepare goes on: a stale or absent cache costs time, never correctness.
  This is the same shape as the Kubernetes refresher Job (26, #55), for the
  same reason: the cache is the one volume every attempt shares, so a
  preparer that could write it could poison every later checkout, and
  having one provider refresh in the preparer gave nothing the separate
  container does not (hades #137). `prepare` then runs the throwaway
  preparer container (the Crucible image carries no git, and under rootless
  Docker the checkout must be created by the container uid that will own
  it), which mounts the cache read-only and performs `git clone --reference
  <cache> --dissociate` into `<artifact_root>/workspaces/<attempt>/repo`
  (the checkout owns its objects; nothing shared is mounted into a worker;
  a cache the refresher left absent means a plain clone from the remote).
  A public repository is cloned and refreshed with no credential at all. A
  private one (ADR 0019) uses a read-only installation token scoped to that
  repository, which the supervisor mints just before `prepare` and revokes
  as soon as it returns: the provider writes it to the stdin of each of the
  two containers that talk to the remote, the refresher and the preparer,
  the script puts it on a tmpfs of that container's own
  (`/run/crucible-token`, as the publisher's is, 23), and git reads it
  through a helper that answers only for https on `github.credential_host`;
  the token and the helper are removed right after the fetch or the clone
  and again on every exit, so nothing after the network steps, and no other
  container, can read it. The supervisor's `cancelled` check (hades #189) is
  asked before the refresher is created, on every poll while it runs, before
  the preparer is created, and on every poll while that runs; a cancel
  force-removes whichever container is running and creates nothing further.
  The preparer then, for a `correct` execution, or a retry of a task
  whose branch Crucible already pushed, check out the remote `work_branch`
  head, else create `work_branch` from `base_ref`; record which happened
  as an event; replace the `origin` URL with a placeholder so no push can
  succeed; write shims and `.git/info/exclude`; install the author
  identity from policy (an empty credential helper, whatever the clone used); point
  `core.hooksPath` at the identity bundle's `hooks/` directory, so the worker's commits
  run Crucible's `commit-msg` hook and never a repository hook (06); render the identity bundle to
  `<artifact_root>/workspaces/<attempt>/identity`; create the empty report
  directory at `<artifact_root>/workspaces/<attempt>/report`.
- `launch`: resolve the image tag to a digest and record it on the
  attempt; refuse if the image's harness version is outside the adapter's
  supported range; then create a container from the allowlisted image with: `--init`
  (S5: a harness as PID 1 ignores SIGTERM, so drain would always end in
  SIGKILL after the grace period), `--user
  1000:1000`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--read-only` root with tmpfs for `/tmp` and `/home/worker`, memory and
  CPU limits from policy, `--pids-limit`, network per policy (dedicated
  bridge with egress allowlist via the proxy's network, or `none`), mounts:
  repo (rw), identity (ro), one credential dir (ro or narrow rw), report dir
  (rw), and the workspace's `pkg-cache` leaf at `/crucible/pkg-cache` (rw),
  which `UV_CACHE_DIR`, `PIP_CACHE_DIR` and `npm_config_cache` point into so a
  dependency install never fills the memory-backed home (FDY-0140). The
  verifier mounts a leaf of its own, `pkg-cache-verifier`, at the same path:
  uv and pip reuse cache entries without checking them again, so a cache the
  worker wrote is never what the verifier's checks run from. Labels:
  `crucible.attempt`, `crucible.task`, `crucible.owner`.
- `observe`: container inspect; `lost` if the container ID no longer exists.
- `logs`: docker logs with timestamps, since offset. `--since` is
  inclusive (S8), so resume is strict-after by the stored
  (timestamp, line hash) pair from 10, never by timestamp alone.
- `collect`: never runs git or reads worker-written files as the Crucible
  process. It launches a throwaway collector container (the same hardened
  shape as a worker, `--network none`, uid 1000, the repo mounted rw, the
  report dir mounted ro, an output dir mounted rw). It first commits whatever
  the worker left uncommitted, as the policy's `git` author with the attempt
  trailer, so edits a model forgot to commit are collected and reviewed rather
  than lost (FDY-0140). That commit runs with the worker's `.git/config`
  replaced by a minimal one and an empty hooks directory, never takes caches
  and build output a repository forgot to ignore (`__pycache__`, `*.pyc`,
  `node_modules`, `.venv`, tool caches, `*.egg-info`, `.coverage`), and is
  skipped with a note when `.git` or `.git/config` is not a plain directory or
  file; a quota checkpoint (16) is the same commit with a `wip` subject, refused
  in that case. `attempt_collected` records `uncommitted_work_committed` or the
  `uncommitted_work_note`. It then produces: `git diff
  --stat` and the **full diff** against `base_ref` (the `no_secrets` and
  scope gates consume the diff content, never only the path list), the
  changed-path list, HEAD
  SHA, `git log --format` of `work_branch`, and a copy of the report
  directory. Git in the collector runs with `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_NOSYSTEM=1`, `-c core.fsmonitor= -c diff.external= -c
  core.pager=cat -c core.hooksPath=/dev/null`. The copy step guarantees that
  no symlink is followed and no hard link, device, or file above the
  policy size cap is copied, recording each rejection as an event; the
  mechanism is the collector's choice (C3 enumerates with `find -P` and
  copies only regular files, since worker images carry no `O_NOFOLLOW`
  helper). The
  collector also writes `work_branch.bundle` (`git bundle create` of
  `base_ref..work_branch`) and a second throwaway container with
  `--network none` runs `git bundle verify` on it (never the Crucible
  process, which has no git and must not parse worker-produced files);
  the bundle is the only thing the publisher (23) ever fetches from.
  The collector also checks each commit's author over the range the
  publisher will push (the remote `work_branch` when the checkout has
  one, else `base_ref`), and writes what it found to `commit-policy/`
  for the `commit_policy` gate (11), which shows it to the reviewer.
  Everything read from the tree is data.
- `terminate`: `drain` sends SIGTERM and waits the policy grace; `kill`
  sends SIGKILL.
- `cleanup`: only for attempts whose exit path recorded `logs_drained`;
  remove container; keep or delete the workspace per policy, except the
  per-attempt credential copy, which is removed under every policy (12);
  release the
  lease; remove the per-attempt credential copy.
- `reconcile`: list containers by label; anything with a label but no live
  attempt row is orphaned and removed; any live attempt with no container is
  marked `lost`.

## Kubernetes provider (C8, specified in 26)

Same contract. One Job per role per attempt in a worker namespace (preparer,
worker, collector, bundle verifier, verifier, publisher, and the admin login
flow), a per-attempt PersistentVolumeClaim as the workspace, identity as a
ConfigMap or projected volume, the harness credential as a per-attempt
Secret seeded from the harness's dedicated Secret, the GitHub App key on
the `crucible` pods only, a pod security context mirroring the Docker
flags and enforced again by Pod Security admission, and a per-attempt
NetworkPolicy in place of the egress proxy. `observe` watches the Job and
Pod. No Docker socket anywhere; the supervisor's ServiceAccount is limited
to that one namespace. The operator decided on 2026-09-21 that this
version runs on the node's standard container runtime; a runtime class
(gVisor or Kata) is a later step. Mechanics, cluster prerequisites, and the
kind test tier are in 26.

## Host-process provider (designed, not implemented)

Runs the harness as a subprocess of Crucible's own host, in a worktree,
with credential directories referenced from the host home. Isolation level
`process`: a weaker isolation mode. It stays in the design and is
implemented only if a spike (S1 to S3) proves a required harness cannot
operate correctly in a container. If implemented it requires an explicit
config flag, explicit per-policy authorization recorded as a decision, and
a banner in every attempt record that used it. Not used in the default
loop.
