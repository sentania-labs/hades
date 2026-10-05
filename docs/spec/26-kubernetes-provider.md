# 26. Kubernetes execution provider

Status: draft for review, 2026-09-21. Decided by the operator the same day:
"let's move forward with the weaker container only sandboxing on k8s ...
and we will target the microvm once it's working." So this version of the
provider runs worker pods on the node's standard container runtime with the
full pod security context, and a stronger runtime class (gVisor or Kata) is a
later, separate step that changes one field of the pod spec and nothing
else.

Everything above the provider is unchanged: task, worker, execution, event,
artifact, gate, evidence, review, publication, administration, and the API
(03, 04, 09, 10, 11, 16, 23, 25). Spec 08 defines the provider interface
every provider implements; this document is the Kubernetes provider's
mechanics, in the same order as 08's Docker section, plus what the cluster
must guarantee before a worker runs there.

## Why a second provider, and why now

The operator cannot use Crucible for real work until workers are sandboxed
on the lab Kubernetes cluster; the Docker provider (08, 13) is for
development, testing, and the readiness evidence gathered so far. The
bootstrap contract names deployment on that cluster through Argo as a
design requirement. The readiness rows that prove isolation, termination,
log capture, and failure detection (19) are re-proven on this provider
before the gate counts for real-project use.

## Topology (from 01, made concrete)

One namespace for Crucible itself (`crucible`), one for workers
(`crucible-workers`). In `crucible`: the `api` Deployment, the `supervisor`
Deployment (one replica, lease-guarded exactly as today, fenced tokens in
PostgreSQL), and PostgreSQL (operator-managed or external; the manifests
carry a single-instance StatefulSet for the lab and a connection-string
option for an external server). No Docker socket anywhere. The GitHub App
key and webhook secret are the Secret `crucible-github-app` in `crucible`,
which the service owns and reads through the API server (12, ADR 0017); it is
mounted, optional, on the `crucible` pods only. The control plane holds two
grants in `crucible`: that Secret (`get` and `patch` by name, and `create`)
and deleting the first-run administrator Secret (ADR 0016). Everything a worker needs lives in `crucible-workers` and is created
per attempt by the supervisor through the Kubernetes API.

The supervisor's ServiceAccount is bound to a Role in `crucible-workers`
that permits create, get, list, watch, and delete on Jobs, Pods,
ConfigMaps, Secrets, and PersistentVolumeClaims, plus `pods/log` and
`pods/exec` for log tail and the login flow, and nothing in any other
namespace. It has no cluster-scoped permissions.

The exact verb set, which is what `deploy/kubernetes/base/workers/role.yaml`
carries and what a unit test holds it to (C9):

| Resource | Verbs |
|---|---|
| `pods`, `configmaps` | create, get, list, watch, delete |
| `secrets`, `persistentvolumeclaims` | create, get, list, watch, **patch**, delete |
| `pods/log` | get |
| `pods/exec` | create, get |
| `resourcequotas`, `events` | get, list |
| `batch/jobs` | create, get, list, watch, delete |
| `networking.k8s.io/networkpolicies` | create, get, list, watch, delete |

The admin login Job and the service-owned harness Secrets (ADR 0015) need no
verb beyond this table: the Job and its NetworkPolicy are `create`, `list` and
`delete`, the URL is `pods/log`, the pasted code and the auth files cross on
`pods/exec`, and the Secret is `create` when absent and one merge `patch` after
that. There is no `update` on anything. The three additions to the sentence
above are the ones the implemented provider needs: `patch` on PersistentVolumeClaims writes the retention label
`cleanup` leaves behind, `patch` on Secrets is the `rw-narrow` credential
sync-back (12), and reading the ResourceQuota is where `max_concurrency` on
`GET /providers` comes from. Reading events is how a Job whose Pod the namespace
quota refused (`FailedCreate`, "exceeded quota") is seen at once rather than at the
end of its timeout. `pods/exec` needs `create` as well as `get`
because the API server authorizes an exec against `create` even when the
client opens it as a GET WebSocket upgrade, which is how the reader Pod's tar
stream is opened; a Role with only `get` is refused at the upgrade with a 403
(found on a real cluster in C9).

The account itself lives in the `crucible` namespace, not in
`crucible-workers`: a Pod can only use a ServiceAccount from its own
namespace, and the supervisor Pod runs beside the api. A RoleBinding may name
a subject in another namespace, so the permission stays namespaced to
`crucible-workers` exactly as above. The api Deployment uses the same account,
because the admin status page (25) reports provider health and the Kubernetes
provider answers that by running the namespace readiness canary: the api
process makes real API calls to `crucible-workers` on every status read.
(Made concrete 2026-09-22 during C9.) Admission (the cluster's
Pod Security admission at the `restricted` level on `crucible-workers`)
enforces the pod shape below independently of Crucible's own code, which
is the surviving half of 13's create-request policy.

## The attempt as Kubernetes objects

Per attempt the provider creates, in `crucible-workers`, all labelled
`crucible.attempt`, `crucible.task`, `crucible.owner`, `crucible.role`:

| Object | Role | Lifetime |
|---|---|---|
| PersistentVolumeClaim `ws-<attempt>` | the workspace: `repo/`, `report/`, `output/`, and `publish/` (the publisher's outcome, made before a push) | attempt, then per cleanup policy; a kept claim is deleted by the retention step once nothing needs it (16) |
| ConfigMap `identity-<attempt>` (or a projected volume from an object store above the ConfigMap size cap, 08) | the identity bundle, read-only | attempt |
| Secret `cred-<attempt>` | the per-attempt copy of one harness credential directory, seeded from the harness's dedicated Secret in `crucible-workers`, `rw-narrow` where the adapter declares it (12) | attempt, deleted under every cleanup policy |
| Secret `checkout-<attempt>` | a private repository's read-only installation token (ADR 0019), key `token`, mounted mode 0400 at `/run/crucible-token` into the refresher and the preparer Jobs and nothing else | created just before the refresher, deleted once the preparer's Pod is gone, on every path; a deletion that fails fails the prepare, and `discard`, `cleanup` and the retention sweep retry it |
| Job `refresh-cache-<attempt>` | the reference cache's only writer: fetches the repository's bare mirror on the cache PVC (or clones it when absent), with git egress only and no workspace or identity bundle; for a private repository it also mounts `checkout-<attempt>` and fetches with it. It gives the remote 20 seconds to answer a ref listing and otherwise leaves the mirror as it is, so a refresh that cannot connect costs seconds, not the kernel's two-minute connect timeout (hades #191) | until complete, then deleted, before the preparer starts |
| Job `prepare-<attempt>` | the preparer: clone into the PVC from the reference cache, mounted read-only, then branch, shims, author identity, `origin` placeholder (08) | until complete, then deleted |
| Job `worker-<attempt>` | the worker, one Pod, `backoffLimit: 0`, `restartPolicy: Never` | until terminal, then deleted after `logs_drained` |
| Job `collect-<attempt>` | the collector, no network, repo and report read-only, output read-write (08) | until complete |
| Job `verify-bundle-<attempt>` | `git bundle verify`, no network | until complete |
| Job `verifier-<attempt>` | re-runs `required_verification` on an independent clone from the bundle (10, 11) | until complete |
| Job `prepare-<attempt>` (before a push) | runs `publish_leaf_script` as the worker uid with the whole claim mounted and no network: refuses (exit 7) unless `output/work_branch.bundle` is a regular file, creates `publish/` with `output/`'s owner and mode, and refuses (exit 8) if it cannot. The publisher's subPaths therefore always exist before its Pod starts, and are never made by the kubelet | until complete, then deleted with its Pod |
| Job `publish-<attempt>` | the publisher (23, ADR 0022): mounts `output/work_branch.bundle` off `ws-<attempt>` as one read-only file and the claim's `publish/` leaf for its outcome, checks the bundle against its seal, builds from it and pushes without force. Its egress is its own NetworkPolicy `np-publisher-<attempt>` | until complete, then deleted with its Pod and policy |
| Secret `publish-token-<attempt>` | the installation token for one push, key `token`, mounted mode 0400 at `/run/crucible-token` into the publisher Job and nothing else | created just before the Job, deleted once its Pod is gone, on every path; one a crashed supervisor left is replaced by the next push of that attempt and removed by the retention sweep |
| Job `login-<harness>-<id>` (admin flow, 25) | the harness's own login in that harness's default worker image (ADR 0018), no workspace, no credential mounted, a memory-backed home; the service reads the auth files back over exec and writes the harness Secret (ADR 0015). Labelled `crucible.role=login`, `crucible.harness`, `crucible.login` and never `crucible.attempt` | until the service has read it back, cancelled, or timed out; its own deadline and a TTL remove it if the api died |
| NetworkPolicy `np-login-<id>` | the login Job's egress: the adapter's `login_endpoints` only | with its Job; the retention sweep removes one whose Job is gone |
| ConfigMap `login-lock-<harness>` (admin flow, 25) | the harness's login lock across every api replica: created before the login Job, so exactly one replica gets it and the others are refused naming its holder. Carries the holder and an expiry (the login deadline, the read-back window and five minutes). Labelled `crucible.role=login-lock` and `crucible.harness`, never `crucible.attempt` | deleted by the api that took it, by uid, however the login ends; a lock past its expiry (its api died) is deleted by uid and replaced by the next login |
| the probe's claim, ConfigMap, per-run Secret and Jobs (admin flow, 25) | the bounded credential probe: an attempt's objects for one prompt, labelled `crucible.admin=probe` | removed by the probe; neither swept nor adopted by the supervisor for two hours |

A Job per role keeps the same separation the Docker provider has (worker,
collector, verifier, publisher are distinct processes with distinct
mounts), and lets Kubernetes own restarts, deadlines, and garbage
collection. `activeDeadlineSeconds` on each Job is the policy's timeout for
that role; Crucible still drains before the deadline and classifies the
exit itself (16). A single-purpose role's own time (the collector, the
verifier, the bundle verifier, the cleaner, the Job that readies a claim for
the publisher) is counted from when its Pod is Running: pulling the image and
waiting for a node are bounded by the launch timeout instead, and the Job's
`activeDeadlineSeconds` is the role's time plus the launch timeout, since
Kubernetes counts it from the Job's start. The short roles' time is the
`kubernetes.timeouts` setting (`role_timeout_seconds`, 120 by default, 25);
before the lab findings of 2026-09-29 it was a fixed 120 seconds that
included the pull. A role Job whose Pod the namespace quota refuses ends at
once with the quota's own message rather than at its timeout, except the
publisher's two Jobs, which wait for room until their deadline (a failed
publication needs an operator's retry, and a full namespace is not a failed
push), and whose timeout then names the quota.

## Pod shape (every role)

Mirrors 13's Docker flags, enforced twice: by the provider's spec and by
Pod Security admission on the namespace.

```yaml
securityContext:            # pod
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  fsGroupChangePolicy: OnRootMismatch
  seccompProfile: {type: RuntimeDefault}
containers:
- securityContext:          # container
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities: {drop: ["ALL"]}
  resources:                # from policy limits
    limits: {cpu: ..., memory: ..., ephemeral-storage: ...}
    requests: {cpu: ..., memory: ...}   # a fraction of the limit, not the limit (issue 93)
  volumeMounts:
  - {name: tmp, mountPath: /tmp}           # emptyDir, medium Memory, sizeLimit
  - {name: home, mountPath: /home/worker}  # emptyDir, medium Memory, sizeLimit
  - {name: ws, mountPath: /crucible/repo, subPath: repo}    # PVC, rw for the worker
  - {name: ws, mountPath: /crucible/report, subPath: report}
  - {name: identity, mountPath: /crucible/identity, readOnly: true}
  - {name: cred, mountPath: <adapter mount target>, readOnly: <not rw-narrow>}
automountServiceAccountToken: false
serviceAccountName: crucible-worker      # a no-permission account
enableServiceLinks: false
hostNetwork: false
hostPID: false
hostIPC: false
terminationGracePeriodSeconds: <policy grace>
```

The mount paths are the ones `crucible/ports/execution.py` defines and the
identity bundle names (06): a worker is told its checkout is at
`/crucible/repo` and its report directory at `/crucible/report`, so those are
where they are mounted. Only the preparer gets the whole claim, at
`/crucible/work`, the way the Docker preparer gets the whole workspace (08).
(Corrected 2026-09-21 during C8a; the draft named one `/crucible/workspace`
mount, which would have contradicted the bundle every worker reads.)

A Secret volume is read-only in Kubernetes whatever the mount asks for, so the
`readOnly: <not rw-narrow>` line above is the read-only case only. The
`rw-narrow` case is below, under credentials.

`--init` has no Kubernetes equivalent; the worker image's entrypoint
already reaps and forwards signals (S5), and `terminationGracePeriodSeconds`
plus a SIGTERM from Crucible's `drain` gives the harness the same window.
`--pids-limit` becomes the node's pod PID limit (a kubelet setting lab-admin
sets; the provider records the effective limit in the launch evidence and
refuses to launch if none is configured). A runtime class is not set in this
version; the field is reserved and documented as the microVM step.

The Docker provider sets `PidsLimit` per container from the policy; the Pod API
has no equivalent. A container's `resources` take only `cpu`, `memory`,
`ephemeral-storage` and huge pages, pod-level `resources` take the same, and
the API server refuses a `pids` entry in either (proved on kind, issue 60). The
only per-pod PID limit on Kubernetes is the kubelet's `podPidsLimit`, a node
setting applied to every Pod on that node. So the limit is the
**operator-declared path** (the operator accepted it on 2026-09-23, on
condition it is documented): lab-admin sets `podPidsLimit` on every node that
can run a Pod of `crucible-workers`, not only the one the canary happens to
land on, and keeps it there through the node configuration pipeline. The
canary confirms it on its own node where the runtime lets it see the pod-level
cgroup; where it cannot, `kubernetes.pod_pid_limit_override` is lab-admin's
attestation, and that attestation is about every such node. A node added to
the pool later is covered only once its kubelet carries the same setting. The
launch evidence records the node each attempt ran on beside the limit the
probe established, so an attempt on a node the canary never measured is
visible after the fact. (Made concrete 2026-09-25, issue 60.)

### Requests below limits (issue 93)

A role pod that requests exactly its limit (2 CPU / 4Gi by default) cannot
schedule on a small cluster that is otherwise busy: no node has 2 CPU
unreserved even at 10% utilization on three 4-CPU nodes, and every launch
times out. Requests are therefore a fraction of the limit, not the limit
itself, carried on the policy's `resources` block:

- `resources.cpu_request_fraction` (default 0.5): the container's CPU
  request as a fraction of `resources.cpus`. A fraction rather than an
  absolute value tracks the limit automatically when a task's policy raises
  or lowers it, and can never itself exceed the limit. At the default, one
  attempt requests 1 CPU while still being allowed to burst to 2, and three
  concurrent attempts (`max_concurrency`) request 3 CPU rather than 6,
  which is what the 3 x 4-CPU lab needs to actually schedule anything.
- `resources.memory_request_fraction` (default 1, operator decision
  2026-09-23): memory stays request-equals-limit by default. Requesting the
  full memory limit gives Guaranteed QoS for memory pressure, so a worker
  promised the policy's memory is not the first thing evicted, which would
  otherwise show up as a `lost` attempt nobody caused (16). CPU carries no
  equivalent eviction risk, so it alone gets the lower default. A deployment
  whose cluster is memory-constrained as well as CPU-constrained may lower
  this fraction too; both fractions are ordinary policy fields, so they are
  editable and versioned exactly where `resources.cpus` and
  `resources.memory` already are (04, 05b), with no separate settings
  surface of their own.

The pod's effective request is the larger of its app containers' sum and any
one init container's request, so the writable-credential init container
(`rw-narrow` harnesses, below) carries the same fraction as the main
container; left at the full limit it would silently cancel the role pod's
lower request.

The readiness canary (`ROLE_CANARY`) is a shell script with curl, never a
role pod, so it does not use the policy's resources at all: both of its pods
request and limit a fixed small size, `kubernetes.canary_cpu_millicores` (default
100m) and `kubernetes.canary_memory` (default 64Mi), configured the same
way as `kubernetes.probe_image`.

## Networking: NetworkPolicy replaces the egress proxy

There is no Squid on the cluster. The workers namespace carries a default
deny for ingress and egress, and the provider creates one NetworkPolicy per
attempt **per role that needs egress**, selecting that attempt's pods of that
role by label. A NetworkPolicy has one `podSelector`, so a single object per
attempt would have to carry the union of every role's destinations, which
would hand the worker GitHub and the collector the model endpoints; a role
with no egress gets no object at all, because the namespace's default deny is
already the answer for it. (Clarified 2026-09-21 during C8a.) The allowed destinations come from the same policy document that
generates the proxy allowlist locally (05b routing pools, the adapter's
declared endpoints, 13), resolved to CIDRs or FQDN rules where the CNI
supports them:

- worker: the policy's `egress_allowlist` as written (05b: "hostnames the
  egress proxy permits for workers"), the model provider endpoints of the
  routed harness, and, for a local route, the configured `endpoint_url`
  hostname and port over HTTP or HTTPS. GitHub is on that list exactly when
  the policy puts it there: a worker holds no GitHub credential, so what it
  gets is read-only in effect, and the git traffic Crucible itself does is the
  preparer's and the publisher's. Until hades #425 (2026-10-05) the provider
  subtracted `github.com` and `api.github.com` from the worker and the
  verifier on the strength of an older sentence here, so a policy that
  allowlisted github.com produced a worker whose curl to it timed out against
  the default deny while the task page said it was permitted; the Docker
  provider's Squid had permitted it all along.
- preparer and publisher: `github.com` and `api.github.com` only. For a
  private repository whose `github.credential_host` is not `github.com`
  (GitHub Enterprise Server, ADR 0019), the preparer and the refresher may
  also reach that host and port, resolved like any other allowlisted name,
  and the publisher always may, because that is where it pushes.
- collector, bundle verifier, verifier: no egress at all (the verifier
  gets the policy's `egress_allowlist` as written, the same list the Docker
  verifier reaches through the proxy, and never the harness endpoints).
- login Job: the harness's login endpoints only, the adapter's
  `login_endpoints` (Claude Code `platform.claude.com` and `api.anthropic.com`,
  Codex `auth.openai.com`, AGY `oauth2.googleapis.com` and `www.googleapis.com`),
  never its model API (crucible#58). Claude Code is the exception: its
  `setup-token` asks the model host for the account's roles before printing the
  token, so its login reaches `api.anthropic.com` (2026-09-29). A harness without login endpoints gets no
  policy at all, and so no egress. (Made concrete 2026-09-24, FDY-0112.)

A `networking.k8s.io/v1` policy has no deny verb and no FQDN rule, so the
allowlist's names are resolved to addresses when the policy is written and the
names themselves are recorded in the object's `crucible.io/egress-hosts`
annotation. A name that does not resolve refuses the launch rather than being
dropped or widened. For a configured local endpoint, the hostname is the trust
anchor: the provider resolves it and permits only the resulting addresses on the
configured port. A private result must also be inside the operator's explicit
`kubernetes.local_endpoint_cidrs` declaration. A URL that directly names a denied
address, or a name resolving to the Kubernetes API ClusterIP, another namespace, or
any denied range outside that declaration, is refused. General allowlist hostnames that
resolve into a denied range remain refused. IPv6 never
appears in a rule and is therefore denied entirely.
A resolved address stays in a policy for `kubernetes.resolve_ttl_seconds`
(default 300) before its name is looked up again. Every Pod whose policy names
resolved addresses carries the same addresses as `hostAliases`, so it connects
to exactly what its policy permits: a name like github.com answers one address
with a 60 second TTL, and a different one to a different resolver, so a Pod that
looked the name up again could get an address its policy never named and time
out against the default deny (hades #191, 2026-09-28). The broad rule pins
nothing. `kubernetes.broad_egress`
(default false) replaces the resolved addresses with the broad rule, the
public internet on 443 minus every denied range, for a CNI that enforces names
some other way; that rule lets a worker reach any public address whether or not
the policy named it, so a deployment turns it on deliberately or not at all.
Both are restart-bound settings, set like `kubernetes.probe_image` and shown on
the admin UI's settings page. (Made concrete 2026-09-25, issue 61.)

**How a worker reaches an allowlisted host (hades #425).** On this provider
the path is direct. There is no proxy and no proxy variable in the worker's
environment: `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` are the Docker
provider's (13) and are never set here, so a plain `curl https://pypi.org/`
or a `uv sync` connects straight to the host. What lets the connection through
is the attempt's worker NetworkPolicy, an `ipBlock` per address the name
resolved to when the policy was written, on TCP 443, and what makes the Pod
connect to one of those addresses rather than a fresh answer from the resolver
is the `hostAliases` entry that pins the name to them. A host the policy names
but the worker cannot reach is therefore one of three things: the name resolved
to an address the namespace denies (the launch is refused and says so), the
name's rule was not written at all (the cause of #425, now gone), or the far
end did not answer on 443. An address that two allowlisted names share is
permitted for both; an unlisted name that happens to resolve to a permitted
address (raw.githubusercontent.com beside objects.githubusercontent.com, both
on Fastly) answers too, which is a property of address-based enforcement and
not a wider grant. IPv6 is denied entirely, so a name's AAAA answer never
matters.

**The egress probe.** Before the harness starts, the launch wrapper the
worker command is wrapped in (the same wrapper on both providers, 07) tries
every name in `CRUCIBLE_EGRESS_ALLOWLIST`, which the provider sets to the
worker's resolved plan: all names at once, `curl` to `https://<host>/` with a
5 second connect timeout and 10 seconds in all, certificate not checked, the
proxy variables honoured where they exist. A `host:port` entry (a local model
endpoint) is left alone. It writes one line to stderr,
`crucible-egress-probe: {"hosts": [...]}`, with per host `reachable` (true
when a connection was made: curl 0, or 35 and 52 when only the TLS handshake
or the HTTP exchange failed after it), `curl_exit`, `ms` and curl's own
message as `detail`. A host it cannot reach is reported, never a reason not
to start the harness, and a missing `curl` is reported the same way. The
supervisor reads the line off the log stream it already pulls and keeps the
parsed document, with `recorded_at`, as the attempt's `egress_probe` (the
first such line only; the harness echoing one later never replaces it), which
`GET /tasks/{id}` and `GET /attempts/{id}` return and the task page shows as
its Egress section, one row per attempt and host. So a dependency install
that failed reads against what the worker could reach before it started,
and is attributed to the egress path or to the worker accordingly. An attempt
with no allowlisted host is not wrapped for the probe and records nothing.

Two destinations are denied explicitly, because a naive policy lets them
through: cluster DNS is allowed on port 53 UDP and TCP to the cluster's DNS
service and nothing else on that address, and the Kubernetes API service,
the node network, the pod network of other namespaces, link-local
`169.254.0.0/16`, and the lab's private ranges are denied. The e2e tier
proves each denial from inside a worker pod (18).

**Selectors for a CNI that translates service addresses first.** Some CNIs
translate a service or LoadBalancer address to its backend pod addresses before
they evaluate policy; Cilium with kube-proxy replacement is the one the lab runs.
There an `ipBlock` on the kube-dns ClusterIP or on a gateway's service address
never matches, and a worker has no DNS and no model (crucible#91). So the policy
also names those destinations by where they actually are, with a standard
`networking.k8s.io/v1` peer of one `namespaceSelector` (on
`kubernetes.io/metadata.name`) and one non-empty `podSelector`:

- cluster DNS: the resolver's pods, `kube-system` and `k8s-app: kube-dns` by
  default, in the same rule as the address and therefore on port 53 UDP and
  TCP and nothing else. An empty DNS namespace leaves the address rule alone.
- an in-cluster local endpoint: when a namespace is set for it, the worker's
  local route is allowed as the pods that take its connections on their own
  port (the Service's `targetPort`, or the URL's port when that is 0), and the
  URL's host is not resolved into an address rule at all. With no namespace set
  the endpoint is outside the cluster and keeps the resolved-address rule above.
  The pods to name are the ones the URL's connection lands on: the gateway's own
  when the URL is its Service, the ingress controller's when it goes through an
  ingress.

A selector never widens a denial: it adds no address, it carries only port 53 or
the endpoint's one port, it may not be empty, and it may not name the workers
namespace or Crucible's own (a worker reaching another attempt or Crucible's
database). Those rules are checked when the setting is saved, when the service
starts, and again when a policy is rendered. No Cilium-specific policy is used;
every CNI that enforces NetworkPolicy matches both forms.

The selectors are the `kubernetes.egress` setting. The settings file seeds it
(`kubernetes.dns_namespace`, `dns_pod_labels`, `local_endpoint_namespace`,
`local_endpoint_pod_labels`, `local_endpoint_port`); an administrator edits it
from the admin API (`GET` and `POST /v1/admin/kubernetes/egress`), the CLI
(`crucible admin kubernetes egress` and `set-egress`) or the Routing page of the
admin UI. A saved value is a `provider_settings` row that wins over the file,
every edit is an audited `kubernetes_egress_updated` event, and each process's
provider reads the row back within 15 seconds (usually sooner in the process that
took the edit), so the supervisor follows an edit made through the API without a
restart. Until a process has read it back, that process keeps launching under the
values it last proved. (Added 2026-09-23 for crucible#91.)

**Readiness.** If the cluster's CNI does not enforce egress NetworkPolicy, the
provider refuses to launch: readiness of the namespace is probed by two canary
pods, one after the other, and the result is recorded and shown on the admin
status page (25). The gate is consulted in `prepare`, before the claim, the
per-attempt credential Secret or the preparer Job (which has GitHub egress)
exist, and again in `launch`; a credential probe consults it before it creates
anything too. (Made concrete 2026-09-25, issue 59.)

- The first runs under the namespace's own rules and no policy of its own, which
  is what a role with no egress gets. It must fail to reach the API server
  (`egress_enforced`), and it reads the pod PID limit. A canary with a policy of
  its own would be isolated by that policy, so it could not tell a namespace that
  lost its default deny from one that has it.
- The second runs under its own NetworkPolicy, rendered exactly as a worker's is
  (cluster DNS, and the enabled local endpoint of the routing policy in force
  when there is one). It must resolve a cluster name, `kubernetes.default.svc`
  (`dns_resolves`), connect to the enabled local endpoint's URL when one is
  enabled (`local_endpoint_reachable`), and still fail to reach the API server.

A failure of the API-server, default-deny or DNS check is `namespace_ready: false`
with a detail naming the check that failed, and every launch is refused until it
passes. The operator, 2026-09-23: "a down provider should only block that
provider." An unreachable `local_endpoint_reachable` does not turn
`namespace_ready` false: it refuses only the launch whose own route (the routing
policy's `endpoint` field on its selected model, carried onto `LaunchSpec.endpoint`)
is `local` (crucible#91, crucible#110), and admits every launch routed elsewhere. A
local endpoint no NetworkPolicy can permit (it resolves into a denied range, or its
selector is refused) is the same endpoint failure: the second canary runs without
it, so DNS and the API server are still proved, and only local-route launches are
refused; worker rules that cannot be written for any other reason fail the probe. A
missing tool in the canary image (no curl, no getent or nslookup) is inconclusive
and never a pass. A probe whose API server, default-deny, DNS and local endpoint
checks all settled (passed, or the endpoint reachable or none configured) is kept
until the provider reads back a changed `kubernetes.egress` setting or enabled
local endpoint; the canary then runs again under the new values before any launch
uses them, and an answer proved under values that changed while the canary ran is
discarded rather than kept. A probe whose only problem is the local endpoint
(unreachable, unresolved or inconclusive) is not kept: the next `launch` or status
read runs the canary again on its own, so a gateway that comes back is picked up
without a settings change or a restart.

The canary runs the first worker image reference the provider knows of, which
before any attempt has resolved one is the first entry of
`kubernetes.image_repositories`. Those are bare repositories, and a bare
repository means `:latest` to a kubelet, so a registry with no `latest` leaves
the canary in `ImagePullBackOff` until the launch timeout and the status page
reporting the namespace as not ready for a reason that has nothing to do with
the namespace. A deployment therefore names one exact, pullable reference in
`kubernetes.probe_image`, and the wiring puts it first.
(Added 2026-09-22 during C9, after exactly that happened on kind.)

## Provider mechanics (08's interface)

- `prepare`: create the PVC, ConfigMap, and per-attempt Secret; when a
  cluster-side reference cache volume is configured, run the refresher Job,
  the only Pod that mounts that volume writable, to fetch the repository's
  mirror on it; then run the preparer Job with the cache mounted read-only
  (on the claim and on the mount). The cache is the one volume every attempt
  shares, so a preparer that could write it could poison every later
  checkout (crucible#55). In the supervisor a refresh waits for the
  preparers cloning from that mirror to finish and holds new ones back until
  it is done. A refresh that fails is logged and the preparer clones from the
  remote without a reference. For a private repository (ADR 0019) the
  supervisor passes a read-only installation token to `prepare`, which
  writes it to the per-attempt Secret `checkout-<attempt>`, mounts that
  Secret into the refresher and the preparer and no other Pod, and deletes
  it before returning, on every path. The preparer Job otherwise performs exactly
  what 08's Docker `prepare` performs. `prepare` returns
  when the Job completes; a failed Job is a prepare failure with the Job's
  log excerpt as detail.
- `launch`: resolve the worker image to a digest through the image registry
  (11, 25) and record it; refuse an unsupported harness version; create the
  NetworkPolicy and the worker Job; return the Job name as the handle. The
  registry is read with `crane` (go-containerregistry), which the service image
  ships at a pinned version: `crane digest` for the reference's own digest (an
  index's, when it is one) and `crane config --platform linux/amd64` by that
  digest for the harness labels. The credential is the image pull Secret the
  kubelet already uses, written for each call into a private `DOCKER_CONFIG`
  directory that is removed when the call returns; each call is bounded by a
  timeout, and crane trusts what the service trusts (`SSL_CERT_FILE`, the
  system store with the lab CA). A registry named by a private (RFC 1918) IP
  address or a `.localhost` name is refused, because crane would fall back to
  plain HTTP for it; name the registry by a host name it serves HTTPS on. The
  operator's decision of 2026-09-24 (108). Registry reads run on a thread pool of
  their own (six threads), so a slow registry cannot hold the threads Kubernetes
  API calls run on. The image listing (25) runs one at a time and every caller
  shares it, and it is bounded at 12 seconds, below the 15 seconds the harness and
  image endpoints wait: past the bound no crane process is started and any still
  running is killed, and tags not resolved in time are left out of that listing.
  Tags starting `ci-` are CI proof pushes, never promotable, and are skipped before
  anything is resolved, so their number does not add to the listing's cost (111);
  the Images page says so. Nothing on the registry is pruned.
- `observe`: read the Job and its Pod.

  | Job | Pod | Result |
  |---|---|---|
  | gone | n/a | `lost` ("the namespace has no such Job") |
  | exists | Pending or Running, within the launch timeout | `running` |
  | exists | Pending past the launch timeout | launch failure, the Pod's conditions as detail (image pull, no schedulable node, PVC unbound), not a stall |
  | exists | terminated container | `exited(code)` |
  | exists | evicted, or its node is gone | `lost` |
  | exists | none yet, and a `FailedCreate` naming the namespace quota | launch failure at once, with the quota's message (the lab findings of 2026-09-29; the Job controller retries such a Pod forever and never fails the Job) |
  | exists | none yet, within the launch timeout | `running` (a Job controller can take a few seconds to create a Pod on a busy node; this is not a loss, 103) |
  | exists | none yet, past the launch timeout | launch failure ("the Job controller never created a Pod") |
  | exists | had one, now gone | `lost` (the Pod existed and disappeared, unlike the row above) |

  Distinguishing the last two rows needs the observer's own memory of whether
  it ever saw this attempt's Pod (103): a Job it has watched since launch
  keeps that memory in the process; one adopted by `reconcile` after a
  restart never had the chance, so it starts in the "none yet" row with the
  Job's own creation time standing in for the launch time.
- `logs`: `pods/log` with timestamps, `sinceTime` from the stored offset,
  resumed strict-after by the (timestamp, line hash) pair (10). A restarted
  supervisor re-attaches by Job name. Each poll also sends `limitBytes`
  (4 MiB), keeps only the whole lines of a capped read, and lets the next poll
  resume from the last of them, so no poll holds a whole long-running log; the
  supervisor's final drain repeats the pull until it brings nothing (10).
  `sinceTime` is one-second granular, so a read that cannot get past its first
  second is retried larger up to 64 MiB; past that, the rest of the second is
  skipped with a `[crucible] log lines skipped` line in the log rather than
  read unbounded. (Made concrete 2026-09-25, issue 63.)
- `collect`: the collector Job with the workspace mounted read-only and an
  output subpath read-write; then the bundle verifier Job; outputs are read
  by the supervisor from the PVC through a short-lived reader Pod, never by
  mounting the PVC into the Crucible pods. The reader's tar is streamed to a
  scratch file and extracted from there, so the supervisor never holds the
  collected archive (up to 256 MiB) in memory. A step the cluster could not
  take or answer (an API server that refused, reset or timed out a
  connection or answered 429 or 5xx, a quota-refused role Pod, a reader Pod
  that did not start, an exec stream that ended before its status) raises
  `ProviderUnavailableError`, and the supervisor collects again later from the
  claim, which still holds the work, rather than failing the attempt (10); a
  credential copy already synced is not synced twice. A failed status look
  while a Pod starts is asked again until the deadline, never taken as the
  answer.
- `terminate`: `drain` deletes the Pod with the policy grace period, read
  off the Pod itself (SIGTERM, then SIGKILL by the kubelet); `kill` deletes
  with grace zero.
- `cleanup`: only after `logs_drained`; delete Jobs and NetworkPolicy;
  delete the per-attempt Secret under every policy; keep or delete the PVC
  per policy (retained PVCs carry a retention label the orphan sweep
  honours); release the lease. `release_workspace` deletes a kept claim and
  anything else still labelled for its attempt once the retention step
  decided nothing needs it (16), and reports a claim still there so the step
  tries again.
- `reconcile`: list Jobs by label; a Job with no live attempt row is
  orphaned and deleted; a live attempt with no Job is `lost`. A Job still
  waiting on its Pod is adopted like a running one (103), its launch time
  taken from the Job's own creation timestamp; one whose Pod already
  finished and was reaped is left alone for cleanup, not adopted.

Heartbeats and stall detection (10, C6c): log progress comes from the Pod log,
and `container_running` is the Pod phase. The filesystem fingerprint comes from
the running worker itself (FDY-0140): the provider's `activity` execs a
read-only `find` in the worker container over the checkout, the report
directory and the home (where a harness keeps its session state), and the
supervisor compares one answer with the next. The workspace path is a `k8s://`
name, so the supervisor's own walk would see nothing and a silent worker, Hermes
under `-z` above all, was stalled out while it worked. The probe is asked no
more often than a `command_running` renewal; the walk stops itself after 10 s
and the exec after 15 s, and a failed or unfinished ask is neither activity nor
its absence. The worker mounts the claim's `pkg-cache` leaf and the verifier its
`pkg-cache-verifier` leaf at `/crucible/pkg-cache` for the package caches (08).

## Credentials on the cluster (12, made concrete)

Each harness's dedicated credential directory becomes one Secret in
`crucible-workers` that only the supervisor's ServiceAccount can read. The
service owns it (ADR 0015, the operator's decisions of 2026-09-23): it creates
it when absent, labelled `app.kubernetes.io/managed-by: crucible` and
`crucible.credential: <harness>`, and it is the only writer, from the login Job,
the Hermes key entry and the sync-back. GitOps does not deliver it; the
database and TLS Secrets stay with GitOps, and the GitHub App Secret is the
service's too (ADR 0017). Its name is
`kubernetes.credential_secrets[<harness>]`, else `crucible-harness-<harness>`
with `_` as `-`.
Per attempt, the provider copies it into `cred-<attempt>`, taking only the
auth files the adapter declares. A Secret key cannot hold a path separator, so
a harness whose auth file sits in a subdirectory (AGY's token) is keyed with
the separator replaced and the volume projects it back to its declared path.

A read-only harness mounts `cred-<attempt>` directly, which is a tmpfs nothing
writes and nothing stores. A harness whose adapter declares `rw-narrow` cannot:
a Kubernetes Secret volume is read-only however it is mounted, and the three
subscription harnesses refresh their own token in place (S1). So an init
container copies the named files off the Secret's read-only projection into the
`credential` leaf of the attempt's own claim, mode 0700 on the directory and
0600 on each file, which is the same shape, the same properties and the same
place the Docker provider puts it (12). The provider reads the rotated file
back from there through the reader Pod and writes it into the harness Secret
whenever the attempt reaches collection, whether the worker exited successfully
or not. This matches the Docker provider: a valid newer refresh is durable state
even when the task itself fails.
(Made concrete 2026-09-21 during C8a.)
The per-attempt Secret is deleted under every cleanup policy. The admin
login flow (25) runs the harness's login in a login Job and captures the device
URL from the Pod log; a Secret volume cannot be written, so the CLI writes into
the Pod's memory-backed home and the service reads the files back over exec and
writes the harness Secret itself, only after they pass the shape check. Hermes
declares optional read-only credential file `api-key`. When the Hermes Secret
exists and holds it, the provider copies that file into the per-attempt
credential Secret and never syncs it back; when it does not, the launch uses the
adapter's explicit unauthenticated placeholder. The Local gateway page's key
entry writes that Secret (crucible#119). (Made concrete 2026-09-24, FDY-0112.)

The supervisor defers a launch while a login Job for its harness exists,
because the login is about to replace the credential (12). A launch that raced
past that check seeds the credential as it stands, and the login then declines
to write over it; a credential probe refuses outright while a login Job exists.

Each api process keeps its logins in memory, so two replicas (a rollout, or an
overlay with more than one) would each start a login for the same harness and
the last to finish would silently replace the Secret. The `login-lock-<harness>`
ConfigMap stops that: `create` is atomic, so only one replica starts a Job, and
a login stores its files only if the lock is still the one it created. Once its
outcome is decided (the CLI exited, the login failed, or it was cancelled) a
login reads `finishing`: the API and the UI refuse a cancel or a code for it,
since the CLI is gone, and neither is audited as accepted. It reads `finished`
or `failed` only after the service has deleted its Job, waited for its Pods and
released its lock, so an operator who retries the moment a login ends, a
cancelled one included, is not refused by that login's own lock. Each step is
best effort: a lock whose delete failed expires on its own. The Docker provider
runs in one api process by design and keeps the in-memory check alone; it sets
`finished` or `failed` before it removes the login container, so it never reads
`finishing`.

Every failure of the Kubernetes API client is a `KubernetesApiError`, which
is a `ProviderError`, so none escapes the supervisor's handlers; a refused,
reset or timed-out connection and a 429 or 5xx answer are its
`KubernetesUnavailableError` form, which is also `ProviderUnavailableError`
(nothing is decided from it). A readiness canary that could not run because
the API server could not answer is not a refusal: the launch fails as an
environment failure the retry rule covers, and the next launch runs the
canary again. A credential Secret that could not be read for the same reason
does not refuse the launch either.

A credential probe whose harness hangs is ended by the worker Job's
`activeDeadlineSeconds` before the provider's own wait runs out. The provider
reads the Job's `DeadlineExceeded` condition and records that probe as a
timeout, not a crash.

## Observability and administration

Attempt evidence that has no field of its own on the provider port (the Job and
Pod names, the node, the effective limits and requests, the pod PID limit and
the NetworkPolicy applied) is stored as one `report/kubernetes-launch.json`
artifact of the attempt, which carries an `artifact_present` evidence row like
any other per-attempt fact Crucible observed (11). The image digest stays on
the attempt row, and so does the launch wrapper's egress probe (`egress_probe`,
hades #425, above). (Made concrete 2026-09-21 during C8a.) `limits.as_dict()`
(issue 93) carries `cpu_request` and `memory_request` beside `cpu` and
`memory`, so the evidence records what was actually asked of the scheduler
next to what was allowed to run. The limits are read back from the live Pod
once it has been seen, and `limits_source` says so (`pod`; `template` for an
attempt adopted before its Pod existed; `policy` before any Pod was read):
admission may rewrite what was asked for, and an attempt adopted after a
supervisor restart has no policy in memory at all, so what the Pod carries is
the observed fact. The same reading gives an adopted attempt's drain the grace
period its Pod was created with, which is the task policy's. (Made concrete
2026-09-25, issues 66 and 76.)

`GET /providers` reports the Kubernetes provider with `isolation: pod`,
`network_control: true`, `resource_limits: true`, `shared_disk: false`, the
harnesses that have a default image (each harness its own, ADR 0018), and `max_concurrency` from the
namespace's ResourceQuotas: the fewest attempts any one limit admits, with
`count/jobs.batch` divided by five Jobs an attempt and the CPU and memory
requests and limits by one Pod's worth at the limits of the last launch (the
policy defaults before one). Until the lab findings of 2026-09-29 only the Job
count was read. The admin status page (25) shows the namespace
readiness probe, the CNI egress enforcement result, the pod PID limit, and
the runtime class in use ("standard" in this version). Attempt evidence
records the image digest, the Job and Pod names, the node, the effective
limits and requests, and the NetworkPolicy applied.

## What the cluster must guarantee first (lab-admin)

Recorded here so the prerequisite is a checklist, not folklore; each item
is verified by the namespace readiness probe or the e2e tier and shown on
the status page.

1. The two namespaces exist; `crucible-workers` has Pod Security admission
   at `restricted` and a default-deny NetworkPolicy.
2. The CNI enforces egress NetworkPolicy (the canary must fail to reach the
   API server), and the worker rules match on it: the canary must resolve a
   cluster name and reach the enabled local endpoint. On a CNI that translates
   service addresses first, the `kubernetes.egress` selectors are what make the
   second half true.
3. A storage class for the workspace PVCs with `ReadWriteOnce` and a size
   the policy's workspace cap fits, and one with `ReadWriteMany` for the
   artifact root: the api serves what the supervisor wrote and they are
   separate Deployments on this topology. Object storage for artifacts would
   remove the second requirement and is a later phase.
   (Added 2026-09-22 during C9.)
4. Every node that can run a Pod of `crucible-workers` has a pod PID limit
   configured (the kubelet's `podPidsLimit`, the pod-level cgroup, not any one
   container's own `pids.max`); there is no per-pod field to set instead (see
   the pod shape above, issue 60). The canary
   reads it from the parent of its own cgroup under cgroup v2, which the
   container runtime must make visible for the probe to confirm it; on a
   runtime that isolates the pod's cgroup from the container (the default on
   current containerd and runc), the probe reports the limit as unconfirmed
   rather than guess, and lab-admin attests to it with
   `kubernetes.pod_pid_limit_override` instead, once, after checking the
   node's kubelet configuration directly (95).
5. The cluster can pull the Crucible and worker images from the registry
   the release publishes to (24); a pull secret if the packages are private.
6. Egress from `crucible-workers` to the model providers, the package
   registries, GitHub, and the Spark is possible at the network edge (the
   NetworkPolicy narrows it; the lab's edge must not block it).
7. An Argo Application pointing at the deployment manifests; the manifests
   are the deployment repository's record, and pin an exact image tag (24).
8. Public DNS is the operator's alone (operator rule 10): the API's ingress
   route is provisioned and reported; no record is created by Crucible or by
   any manifest.

A runtime class for worker pods (gVisor or Kata) is not a prerequisite for
this version.

9. The namespace's `requests.cpu` and `requests.memory` (`resourcequota.yaml`)
   stay consistent with the running policy's request fractions and
   `max_concurrency` by hand: `requests.cpu` is `max_concurrency` times
   `resources.cpus * cpu_request_fraction`, and `requests.memory` the same
   with `memory_request_fraction` (issue 93). `limits.cpu` and
   `limits.memory` stay `max_concurrency` times the plain limits, unchanged
   by this. `make manifests` prints the rendered total and refuses a target
   whose total exceeds a stated cluster CPU or memory budget
   (`docs/deployment.md`), which catches the quota drifting from the policy
   but not a policy whose fraction alone makes the math wrong; that is a
   review-time check, not a rendered one. A quota left stale after a policy
   raises its fraction fails closed: fewer concurrent attempts admit, never
   more than the quota allows.

The manifests that satisfy the Crucible half of this list are
`deploy/kubernetes`, and the operator-facing runbook, including every
placeholder lab-admin fills in and the public DNS record the operator creates
alone, is `docs/deployment.md` (C9).

## Testing (18, extended)

A fourth tier, `make e2e-kind`: the end-to-end suite run against the
Kubernetes provider on a throwaway kind cluster with a unique name, deleted
on exit even on failure (the `sdlc` skill's kind pattern). It loads the
script-harness image with `kind load docker-image`, installs a CNI that
enforces NetworkPolicy (kind's default does not), applies the namespace
manifests, and runs the same cases as the Docker tier plus:

- every NetworkPolicy denial from inside a worker pod: API server, cluster
  DNS on any port but 53, another namespace, link-local, the lab ranges;
- a worker under a policy that allowlists a host reaches it (hades #425): the
  tier's stand-in github.com on `198.51.100.10`, under resolved rules and
  `hostAliases`, fetched from inside the worker, with the launch wrapper's
  probe reporting it reachable and the allowlisted-but-silent
  `198.51.100.30` unreachable; then the same through the supervisor, with
  the probe on the attempt record and the task page;
- a Pod evicted or deleted out of band is `lost`;
- a worker that ignores SIGTERM is killed at the grace period;
- the per-attempt Secret is gone after cleanup under every policy;
- supervisor restart re-attaches to a running Job and logs resume;
- the readiness probe refuses launches when egress enforcement is absent
  (run once with the enforcing CNI removed).

`tools/kind/cilium-egress.sh` is the same kind of disposable cluster running
Cilium with kube-proxy replacement instead of Calico. It puts a stand-in model
gateway behind a Service and shows, from a pod under each policy, that the
address-only rules of before crucible#91 leave a worker with no DNS and no
gateway while the selector rules give it both, with the API server, the rest
of the resolver's ports and the internet still unreachable; then it runs the
provider's own readiness canary in both forms. It is a local proof, not a CI
job. (Added 2026-09-23.)

It runs in CI on the same runner class as the Docker tier. The readiness
rows 5, 7, 11, 12, and 23 are re-proven on this tier and cited in 19 with
their kind run before the gate counts for real-project use.

## Out of scope for this version

Runtime class (microVM or gVisor), multi-cluster, object storage for
artifacts (the PVC and the reader Pod are enough for the lab), a Kubernetes
operator, and autoscaling. Each is a later phase in 20.
