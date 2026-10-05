# Deploying Crucible on Kubernetes

> **These manifests are examples that track `latest`.** A real deployment copies them
> into the deployer's own repository and pins the exact tag and digest there.

The operator-facing runbook for the manifests in `deploy/kubernetes`. It covers what
lab-admin provides, what Crucible provides, how the deployment is applied through Argo,
how the harnesses are logged in once it is up, how to verify that it works, and the one
thing the operator does alone.

Local operation on Docker Compose is a different document: `docs/spec/13-local-operation.md`
and `docs/implementation-notes/deploy-local.md`. The provider's mechanics are
`docs/spec/26-kubernetes-provider.md`; this is only the deployment.

## What is in the repository

```
deploy/kubernetes/
  base/crucible/         the api and supervisor Deployments, PostgreSQL, the settings
                         ConfigMap, the artifact claim, the migration Job, the Service
                         and the Ingress
  base/workers/          the crucible-workers namespace: Pod Security admission at
                         restricted, the default-deny NetworkPolicy, the worker account,
                         the supervisor Role and RoleBinding, the ResourceQuota and the
                         reference-cache claim
  overlays/lab/          every value that is a property of a cluster, as a named
                         placeholder
  overlays/kind/         the disposable proof (`make deploy-kind`), never applied
                         anywhere else
  secret-shapes/         the shape of every Secret, as SealedSecret placeholders and one
                         ExternalSecret example. No value and no ciphertext.
  argocd/application.yaml  the Application template, automated sync off
```

The manifests in this repository are examples, and `base/kustomization.yaml` tracks
`latest` on purpose (spec 13, spec 24). A real deployment is a copy: lab-admin copies
the **whole `deploy/kubernetes` tree** (`base`, `overlays`, `secret-shapes`, `argocd`) into
their own GitOps repository **at the same `deploy/kubernetes` path**, because
`overlays/lab`'s own kustomization resolves `../../base` and `../../secret-shapes/sealed`
as relative paths and `argocd/application.yaml` names `deploy/kubernetes/overlays/lab` as
the Application's `source.path`; copying only the overlay, or copying the tree somewhere
else, leaves one of those pointing at nothing. lab-admin then
pins the image in the copied `overlays/lab`, at `REPLACE_ME_CRUCIBLE_TAG` and
`REPLACE_ME_CRUCIBLE_DIGEST` below; keeping the same path means `argocd/application.yaml`'s
`source.path` needs no edit. Argo tracks that repository, never this
one, and changing the deployed version is that one pin and a sync, in the deployer's own
history.

## What lab-admin provides

Spec 26's checklist, made concrete. Each row is either a placeholder in
`overlays/lab/` or an object lab-admin creates in their own GitOps repository.

| # | What | Where it lands |
|---|---|---|
| 1 | Nothing: the two namespaces, the `restricted` admission labels and the default-deny NetworkPolicy are in `base/workers` and are applied by this Application | - |
| 2 | A CNI that enforces egress NetworkPolicy, on which the worker DNS and local endpoint rules match | verified by the readiness canary, shown on the status page; on Cilium with kube-proxy replacement, see "Cilium and an in-cluster LiteLLM" below |
| 3 | A `ReadWriteOnce` storage class for PostgreSQL, the reference cache and the attempt workspaces | `REPLACE_ME_STORAGE_CLASS_RWO` |
| 3b | A `ReadWriteMany` storage class for the artifact root | `REPLACE_ME_STORAGE_CLASS_RWX` |
| 4 | A pod PID limit configured on every node that can run a `crucible-workers` Pod (the kubelet's `podPidsLimit`; Kubernetes has no per-pod PID field, issue 60) | reported on the status page when the canary can see it; the provider refuses to launch without a confirmed one (95: on a runtime that isolates the pod's cgroup from the container, the canary cannot see it at all, and lab-admin attests to it with `kubernetes.pod_pid_limit_override` instead) |
| 5 | The cluster can pull `ghcr.io/sentania-labs/crucible` and `ghcr.io/sentania-labs/crucible-worker` (the release publishes both); a pull secret if the packages are private. The api and supervisor Pods also read the worker registry themselves, to resolve a tag to a digest and its harness labels before a launch and to list images for promotion: they run `crane`, which the service image ships, with the same pull Secret, so nothing else is configured. They need HTTPS egress to the registry and to the host it redirects blob downloads to (for GHCR, `pkg-containers.githubusercontent.com`). A registry must be named by a host name it serves HTTPS on: one named by a private IP address is refused, because crane would read it over plain HTTP | `REPLACE_ME_IMAGE_PULL_SECRET` |
| 6 | Egress from `crucible-workers` to the model providers, the package registries, GitHub and the Spark is possible at the network edge | the per-attempt NetworkPolicy narrows it; the edge must not block it |
| 7 | The Argo Application | `argocd/application.yaml`, with `REPLACE_ME_ARGOCD_PROJECT`, `REPLACE_ME_MANIFEST_REPO_URL`, `REPLACE_ME_MANIFEST_REVISION` |
| 8 | Nothing: public DNS is the operator's alone (below) | - |

### Every placeholder, and what goes in it

| Placeholder | File | Value |
|---|---|---|
| `REPLACE_ME_LAB_DOMAIN` | `overlays/lab/ingress.yaml` | the domain the API host sits under; the host becomes `crucible.<domain>` |
| `REPLACE_ME_INGRESS_CLASS` | `overlays/lab/ingress.yaml` | the cluster's ingress class name |
| `REPLACE_ME_CERT_ISSUER_ANNOTATION` and `REPLACE_ME_CERT_ISSUER_NAME` | `overlays/lab/ingress.yaml` | the annotation the cluster's certificate issuer watches, and the issuer's name. Delete the annotation entirely if certificates are provisioned some other way; the TLS Secret is `crucible-tls` either way |
| `REPLACE_ME_STORAGE_CLASS_RWO` | `overlays/lab/storage.yaml`, `overlays/lab/settings.yaml` | a `ReadWriteOnce` class. It appears twice on purpose: once for the claims the manifests create and once as the class the provider gives each attempt's workspace claim |
| `REPLACE_ME_STORAGE_CLASS_RWX` | `overlays/lab/storage.yaml` | a `ReadWriteMany` class, for the artifact root the api serves and the supervisor writes |
| `REPLACE_ME_CLUSTER_DNS_IP` | `overlays/lab/settings.yaml` | `kubectl -n kube-system get service kube-dns -o jsonpath='{.spec.clusterIP}'` |
| `REPLACE_ME_RENDER_TIMEZONE` | `overlays/lab/settings.yaml` | the operator's IANA zone. Stored time is UTC; this is only how it is rendered (01) |
| `REPLACE_ME_IMAGE_PULL_SECRET` | `overlays/lab/settings.yaml`, `overlays/lab/pull-secret.yaml` | the pull secret's name, in both namespaces. Delete the `pull-secret.yaml` patch and blank the setting when the packages are public |
| `REPLACE_ME_LOCAL_ENDPOINT_NAMESPACE` | `overlays/lab/settings.yaml` | the namespace of the pods that take the local endpoint's connections (see "Cilium and an in-cluster LiteLLM") |
| `REPLACE_ME_LOCAL_ENDPOINT_LABEL_KEY`, `REPLACE_ME_LOCAL_ENDPOINT_LABEL_VALUE` | `overlays/lab/settings.yaml` | one label those pods carry and nothing else in that namespace needs to: `kubectl -n <namespace> get pods --show-labels` |
| `REPLACE_ME_LOCAL_ENDPOINT_PORT` | `overlays/lab/settings.yaml` | those pods' port, which is the Service's `targetPort`: `kubectl -n <namespace> get service <name> -o jsonpath='{.spec.ports[0].targetPort}'`. `0` means the endpoint URL's own port |
| `REPLACE_ME_PROBE_IMAGE` | `overlays/lab/settings.yaml` comment | the one worker image reference. The example says `ghcr.io/sentania-labs/crucible-worker:latest`; the deployer pins `ghcr.io/sentania-labs/crucible-worker:<version>@<digest>` from the release body, the same sentence and the same source as `REPLACE_ME_CRUCIBLE_TAG`/`REPLACE_ME_CRUCIBLE_DIGEST` below. 26's readiness canary runs it, and a bare repository would mean `:latest` to a kubelet. The committed default is blank, so set it only when that release's worker image is published. |
| `REPLACE_ME_CRUCIBLE_TAG`, `REPLACE_ME_CRUCIBLE_DIGEST` | `overlays/lab/kustomization.yaml` | the exact tag and its digest from the GitHub release being deployed. The base tracks `latest`; this is the deployer's own pin, set here and nowhere in this repository. The release's own body is the source: the release workflow pushes the service image and both worker images with `docker push` (the operator's decision, 2026-09-23), then reads each published digest back from GHCR and records it there. Never take the digest from `images/manifest.env`: that is the local build's OCI digest, and `docker push` re-encodes the layers, so it is not what the registry serves. For an older release whose body predates this, or to double-check the release body itself, read the same digest straight from the registry: `docker buildx imagetools inspect ghcr.io/sentania-labs/crucible:<tag> --format '{{json .Manifest.Digest}}'`. Where both exist, they must agree; a mismatch means one of them is wrong, and the registry read wins |
| `REPLACE_ME_ARGOCD_PROJECT`, `REPLACE_ME_MANIFEST_REPO_URL`, `REPLACE_ME_MANIFEST_REVISION` | `argocd/application.yaml` | the Argo project, the repository holding these manifests, and an exact tag or commit. Never a branch: this field plus the pinned image tag are the deployment's record of what is running |
| `REPLACE_ME_SECRET_STORE_NAME`, `REPLACE_ME_REMOTE_PATH` | `secret-shapes/external/database.yaml` | only on a cluster using external-secrets instead of sealed ones |

### Cilium and an in-cluster LiteLLM

The lab cluster is k3s on Cilium with kube-proxy replacement, and its LiteLLM gateway
runs inside the cluster. Cilium translates a service or LoadBalancer address to the
backend pod addresses before it evaluates policy, so a policy rule on the kube-dns
ClusterIP or on the gateway's service address never matches there: every attempt would
have no DNS and no route to the gateway (crucible#91). The provider therefore also
allows both by where they actually are, as pods selected by namespace and labels, and
the lab overlay sets:

| Setting | Lab value | Why |
|---|---|---|
| `CRUCIBLE_KUBERNETES__CLUSTER_DNS_IP` | the kube-dns ClusterIP | the address rule, kept for CNIs that match it |
| `CRUCIBLE_KUBERNETES__DNS_NAMESPACE`, `__DNS_POD_LABELS` | `kube-system`, `{"k8s-app":"kube-dns"}` (from the base) | the resolver's pods, which Cilium does match; k3s's CoreDNS carries this label |
| `CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_NAMESPACE` | the gateway pods' namespace | turns the local endpoint rule into a selector rule |
| `CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_POD_LABELS` | `{"<key>":"<value>"}` of those pods | which pods |
| `CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_PORT` | the pods' port (the Service's `targetPort`) | Cilium sees the pod port, not the Service port |
| `CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_CIDRS` | `[]` | only an endpoint outside the cluster needs an address range |

The pods to name are the ones the endpoint URL's connection lands on. If the local
endpoint URL (Local gateway page) is LiteLLM's own Service name or LoadBalancer address, that
is the LiteLLM pods. If the URL goes through an ingress, it is the ingress controller's
pods and their port, not LiteLLM's.

These file values only seed the `kubernetes.egress` setting. Once the deployment is up,
change them from the admin UI (Routing, "Edit Kubernetes egress selectors"), from
`crucible admin kubernetes set-egress`, or with `POST /v1/admin/kubernetes/egress`; a
saved value wins over the file, is audited, and reaches the supervisor within 15 seconds
without a restart. A save states both halves: an empty namespace is how a selector is
turned off, and a request that leaves one out is refused. No selector may name
`crucible` or `crucible-workers`, and none may be empty. The readiness canary runs again
under the new values before any launch uses them.

### The Secrets

`deploy/kubernetes/secret-shapes/README.md` is the authority on the shapes. GitOps
delivers one Secret:

| Object | Namespace | Keys |
|---|---|---|
| `crucible-database` | `crucible` | `password`, and `url`, the whole DSN, which must carry the same password |

**The GitHub App Secret is not GitOps's either** (ADR 0017). On the GitHub page press
Create GitHub App (for your account, or name an organization): GitHub opens its own
create page filled in, you confirm, and GitHub sends your browser back to Crucible, which
creates `crucible-github-app` in `crucible` (keys `app-id`, `app.pem`, `webhook.secret`)
and is its only writer (crucible#168). No public DNS record is needed: every redirect is
of your own browser, so the internal hostname you use for the UI is enough, and the App
has no webhook (Crucible polls). If Crucible sees a different address than your browser
uses, set it under Return address (`crucible admin github set-external-url`). Then press
Install on GitHub; GitHub sends you back to the repository picker. Create is the only
way to connect an App. To change Apps, use Replace the App on the same page, install the
new App, and register each repository again on Repositories with its new installation.
A deployment that sealed `crucible-github-app` before this change takes it out of its
GitOps repository without pruning it, or creates the App again afterwards.

**Private repositories need the App and nothing else** (ADR 0019). The picker registers a
private repository as private; the Repositories form and `crucible admin repository
register --private` do the same for one it cannot show, with the installation ID that
covers it. Registration asks GitHub for a read-only token for that repository and revokes
it at once, so a missing App, a wrong installation, or an App without Contents read is
refused there, in those words. Each preparation then gets its own read-only token,
revoked when the step ends; on this cluster it is a short-lived Secret `checkout-<attempt>`
in `crucible-workers`, mounted into the cache refresher and the preparer Jobs only. No
extra RBAC is needed: the service already creates and deletes Secrets there for the
harness credential copies. The token is answered only for https on
`CRUCIBLE_GITHUB__CREDENTIAL_HOST` (default `github.com`); a GitHub Enterprise Server
deployment sets that to its host, and the preparer of a private repository may then reach
it. Public repositories clone with no credential, as before.

**The harness credential Secrets are not GitOps's.** Crucible creates and owns them
(ADR 0015): the login flow below writes each one, the Hermes key entry on the Local
gateway page writes Hermes's, and the supervisor writes a refreshed token back after an attempt.
A Secret written by both Crucible and GitOps drifts, and a sync would restore a token the
harness has already rotated. They are:

| Object | Namespace | Keys |
|---|---|---|
| `crucible-harness-claude-code` | `crucible-workers` | `oauth-token`, `.claude.json` |
| `crucible-harness-codex` | `crucible-workers` | `auth.json` |
| `crucible-harness-agy` | `crucible-workers` | `antigravity-cli_antigravity-oauth-token` |
| `crucible-harness-hermes` | `crucible-workers` | `api-key` |

Each carries `app.kubernetes.io/managed-by: crucible`. The names come from
`CRUCIBLE_KUBERNETES__CREDENTIAL_SECRETS` in the settings ConfigMap, which the base
already sets. Nothing has to exist before the first login, and the supervisor's Role
already carries the verbs Crucible needs for them. A deployment that sealed harness
Secrets before this change takes them out of its GitOps repository first; see the
secret-shapes README for doing that without Argo's prune deleting the credential.

The placeholders in `secret-shapes/sealed/` are deliberately not valid ciphertext: the
sealed-secrets controller refuses them, so nothing starts with a wrong value. Seal the
real ones with `kubeseal` against the cluster's own key, in lab-admin's GitOps
repository. Nothing sealed, encrypted or plain belongs in Crucible's repository.

## What Crucible provides

Everything else: both namespaces with their admission labels and the default deny, the
supervisor's Role scoped to `crucible-workers`, the one Role in `crucible` for the GitHub
App Secret the service owns (ADR 0017), the worker
ServiceAccount with no permission at all, the ResourceQuota that is also the provider's
concurrency bound, the reference cache claim, PostgreSQL for the lab, the migration Job,
the api and supervisor Deployments, the Service, and the Ingress route without a host.

Everything an attempt becomes (Jobs, Pods, ConfigMaps, per-attempt Secrets,
NetworkPolicies and workspace claims), every login Job and probe, and the harness
credential Secrets are created by Crucible at runtime and are not part of the desired
state. Argo tracks only what it applied, so it neither reports those as drift nor
prunes them.


### ResourceQuota sizing

The base quota fits three concurrent attempts at the default policy (2 CPU limit, 0.5
CPU request fraction, 4 GiB memory limit). Its `count/jobs.batch` is 5 x 3 + 2 = 17: five
Jobs per attempt (preparer, worker, collector, bundle-verifier, verifier) plus two
headroom Jobs for the Kubernetes publisher (issue #226). The provider divides
`count/jobs.batch` by five when reporting capacity, so this base quota advertises three
attempts (integer division).

The deployer sets the quota's values as an overlay. For any worker count N with the
default policy the sizing rule is:

```
count/jobs.batch    = 5N + 2      (5 Jobs per attempt + 2 publisher headroom)
persistentvolumeclaims = 4N       (each attempt claims four PVCs: workspace, reference
                                  cache, bundle output, artifact root)
requests.cpu        = N           (2 CPU limit x 0.5 fraction = 1 CPU per attempt)
limits.cpu          = 2N          (2 CPU limit per attempt)
requests.memory     = 4N GiB      (4 GiB per attempt)
limits.memory       = 4N GiB      (4 GiB per attempt, Guaranteed-for-memory)
```

Worked example for N = 10 (the lab's default): 52 Jobs, 40 claims, 10 CPU requested, 20
CPU limit, 40 GiB memory. The quota is the deployer's overlay to set; the provider
advertises capacity from it, so a stale quota shows up as a smaller max_concurrency on
the Providers page, never as an unbounded launch.

## Applying it

1. Copy the whole `deploy/kubernetes` tree into lab-admin's GitOps repository, keeping
   its relative paths intact, then fill every placeholder above in that copy.
2. Seal or source the Secrets.
3. Create the Application from `argocd/application.yaml`.
4. Sync it, by hand. **Automated sync is off by default and the first sync is a person
   looking at the diff**: that look is what catches a placeholder nobody filled in.
5. Watch the migration Job complete. `crucible serve` refuses to start against a schema
   that is not at head, so the api and supervisor wait for it rather than corrupting
   anything.
6. Read the one-time first-run administrator token from the Secret the migration Job
   wrote. It is never in the Job's log (ADR 0016); the log says only where it is:

   ```sh
   kubectl -n crucible get secret crucible-first-run-admin \
     -o jsonpath='{.data.token}' | base64 -d
   ```

   Only the migrate Job's account can create that Secret and only the api's account can
   delete it; reading it takes your own cluster permissions. The api deletes it the
   first time you sign in at `/ui` with it (or when that principal is revoked), so
   create your own administrator before you lose the session. Only the token's salted
   hash is stored (25).

## Logging the harnesses in

Once the api is reachable, sign in at `/ui` with that token and, for each harness
(25, step by step in that document):

1. **Credentials**: start the login (the harness's Login page, `crucible admin
   credentials login`, or `POST /v1/admin/credentials/{harness}/login`). The service runs
   the harness's own CLI as a Job in `crucible-workers` from the promoted worker image,
   with a memory-backed home, no workspace, no credential mounted, and a NetworkPolicy
   for that harness's login endpoints only (never its model API). The page shows the
   device or browser URL and the device code read from the Pod log, the harness's own
   prompt, and the steps for that harness; paste the code back where the harness asks
   for one (Claude Code, AGY). The code has a window (Codex fifteen minutes, AGY sixty
   seconds, and the page shows the local time AGY stops waiting). For AGY, Google's
   redirect after sign-in lands on an address the browser cannot load: copy the `code`
   value from that address bar (or the whole address) and paste it. A login that ended,
   including AGY running out of time, starts again from the same page (hades #173). When the CLI exits the service reads the auth
   files off the Pod, never through its log, checks their shape, writes the harness
   Secret, and deletes the Job. A login that is cancelled, times out, or whose files fail
   the shape check leaves the Secret as it was; files that pass are stored even when the
   CLI exits non-zero (AGY always does here, because its login ends with a prompt to a
   model API the login Job cannot reach), and the page says so. A credential that
   already passes the shape check is only replaced when the login is started with
   replace.
2. **Finish**, then **Validate**: finish records the shape of what is now in the
   Secret, and validate runs the bounded probe in the hardened image against it (a
   worker Job with the per-run copy of the Secret and the worker's own egress rules),
   records the result, and removes everything it created. A login is refused while an
   attempt of that harness holds its credential, and a launch of that harness waits
   while a login runs.
3. The harness stays `session_compatibility: unverified` until the daily-session
   compatibility test passes for it (21, S1b). A harness is not enabled for normal
   workers before that.
4. **Harnesses**: enable the harness. Enable and Disable on this page (or
   `crucible admin harnesses enable|disable NAME`, or the admin API) are the
   administrator's decision: stored by the service, audited, and in force for new tasks
   at once, with no ConfigMap edit and no restart. An unverified harness can be enabled;
   the page keeps its reason as a warning, and **Test** is how you prove it works (hades
   #174).
   Choose credential mount mode on Credentials. It is stored and audited by Hades and
   applies to the next launch. Neither harness enablement and reason nor credential
   mount mode belongs in the deployment ConfigMap.
5. **Images**: choose each harness's worker image. The page has one row per harness,
   with a pulldown of the images that carry that harness at a supported version
   (releases and `latest`); pick one and Promote. Promotion is per harness (the
   operator's decision of 2026-09-25, ADR 0018): each harness can run a different
   image, and promoting one never moves another. A launch of a harness with no image is
   refused. The registry the page lists is `CRUCIBLE_KUBERNETES__IMAGE_REPOSITORIES`,
   which the base settings already set to `["ghcr.io/sentania-labs/crucible-worker"]`;
   compose deployments list the local daemon's `crucible-worker` images instead and need
   no setting.

**Rolling a harness's image back** is Roll back on its row: it returns that harness to
the image its last promotion replaced, and leaves every other harness where it is.
Promotion checks the harness's version in the image against the running release's
tested range, so once a release raises a range's lower bound past an older image's pin,
that older image is no longer offered for it; roll the Crucible release back with it.

For Hermes, use **Local gateway** to set the HTTPS `/v1` gateway URL and the LiteLLM
virtual key together (also `crucible admin gateway set --endpoint-url URL --key` or
`POST /v1/admin/gateway`), then tick the gateway's models to use, their thinking
preference and capability, and the pool concurrency; saving the models writes a new
routing policy version. Crucible writes the key into the `crucible-harness-hermes`
Secret, creating it if it is absent, and tests both: unauthenticated
`/health/readiness` must return 200, then authenticated `/v1/models` decides. The key is
never shown again; the page and `GET /v1/admin/credentials/hermes` report only
`key_set`. The committed lab CA is already installed in the worker image trust store.

The operator's own daily-use harness directories are never read, copied or referenced
(12). These are dedicated Crucible logins.

## Verifying

Done means seen working, so all three:

1. **The status page.** `/ui` Status, or `GET /v1/admin/status`. The `kubernetes`
   provider row must read `health: ok` with

   ```json
   {
     "namespace_ready": true,
     "egress_enforced": true,
     "dns_resolves": true,
     "local_endpoint_reachable": true,
     "local_endpoint_detail": null,
     "pod_pid_limit": <a number, not null>,
     "pod_pid_limit_source": "cgroup-v2-parent",
     "runtime_class": "standard"
   }
   ```

   `namespace_ready` is 26's readiness canary, two short-lived Pods. The first runs
   under the namespace's own rules, must fail to reach the API server, and reads the pod
   PID limit. The second runs under the egress rules a worker gets and must resolve a
   cluster name, connect to the enabled local endpoint (`local_endpoint_reachable` is
   `null` when no local model is enabled), and also fail to reach the API server.
   `egress_enforced: false` means the CNI is not enforcing egress NetworkPolicy and the
   provider will refuse every launch, which is the correct behaviour and not a bug to
   route around. `dns_resolves: false` means the rules do not match on this CNI: set the
   `kubernetes.egress` selectors above; the `detail` field names the check that failed.
   A `null` with `namespace_ready: false` is a canary that could not tell (its image has
   no curl, getent or nslookup), which never passes.

   The operator, 2026-09-23: "a down provider should only block that provider."
   `local_endpoint_reachable: false` does not turn `namespace_ready` false and does not
   refuse every launch: it refuses only a launch whose selected model routes to that
   local endpoint (today, Hermes), while a Claude Code or Codex attempt on a
   subscription endpoint still launches; `local_endpoint_detail` and the refused
   launch's own error name the endpoint check as the reason. The same holds when no
   NetworkPolicy can permit the endpoint at all (it resolves into a denied range):
   the detail says `no rule can permit it`, and only local-route launches are refused.



   `pod_pid_limit` is the kubelet's `podPidsLimit` (the `--pod-pids-limit` flag, or the
   `podPidsLimit` field of the node's `KubeletConfiguration`), not a number the canary's
   own container carries: a container's own cgroup always has some `pids.max`, and it is
   not this setting (95). `pod_pid_limit: null` means the gate could not confirm one is
   in force, which is never treated as a pass; `pod_pid_limit_source` says why:
   - `cgroup-v2-parent`: read directly from the pod's own cgroup. A number here is a
     limit in force; `null` here means checklist item 4 is not done.
   - `cgroupns-private`: the container runtime gave this container its own cgroup
     namespace, so the pod-level cgroup is not visible from inside it at all. This is
     the default on current containerd and runc, so `pod_pid_limit` reads `null` here
     whether or not lab-admin has set `podPidsLimit` on the node; the canary cannot
     confirm it.
   - `cgroup-v1`: the node runs the v1 cgroup hierarchy, where the same visibility
     problem applies and is not implemented; treat as unconfirmed.
   - `operator-declared`: `kubernetes.pod_pid_limit_override` in settings. On a cluster
     whose runtime hides the pod cgroup (the `cgroupns-private` case above, which is
     most current clusters), the canary can never confirm the limit itself; this setting
     is lab-admin's attestation, made once after checking the node's kubelet
     configuration directly, that item 4 is done. It never overrides a limit the canary
     positively read as absent (a `cgroup-v2-parent` result of `null`): only a result
     the canary could not read at all falls back to it.

   On a cluster with more than one node, the canary measures only the node it lands
   on. Item 4 is about every node that can run a worker, so a new node joins the pool
   with the same `podPidsLimit` in its kubelet configuration before it takes work, and
   an override covers every such node, not just the canary's. Each attempt's
   `report/kubernetes-launch.json` names the node it ran on beside the limit, so an
   attempt on a node that was never measured can be found afterwards (issue 60).

2. **The probe, after a change.** The answer is cached while it passes and re-run while
   it fails, so fixing the CNI does not need a Crucible restart. Saving the
   `kubernetes.egress` setting or the local endpoint forgets a passed answer once the
   provider reads it back (within 15 seconds), so the canary runs again before any
   launch uses the new values.

3. **One task.** Submit a trivial task against a registered repository with
   `execution_request.provider: "kubernetes"` and drive it to `accepted`. What that looks
   like end to end is `tools/smoke/kubernetes_smoke.py`, which is what `make deploy-kind`
   runs against a disposable cluster.

## Tasks against this repository

Tasks Foundry submits against this repository (sentania-labs/hades) run under their own
policy, `examples/policies/hades-self-hosting.yaml` (hades #184, ADR 0020). Its required
checks are `make lint`, `make test-unit` and `make scan`, which run in the worker image
with no Docker daemon and no Postgres; `make test-integration` and the e2e tiers need
both, so branch CI on the pushed head stays their full proof of record (the operator's
decision, 2026-09-28). The worker and the verifier fetch the hash-locked dependencies
from PyPI, which the policy's egress allowlist keeps.

The policy names a routing policy version. Upload it naming the one the default-software
in force names, so it routes to the same models (the gateway's Hermes models included):

```sh
# The admin token is read from a file into curl's header file on a pipe (printf is a
# shell builtin), so it is never in any process's argument list.
TOKEN_FILE=/path/to/admin-token
auth() { printf 'Authorization: Bearer %s\n' "$(cat "$TOKEN_FILE")"; }
routing=$(curl -fsS -H @<(auth) \
  "$CRUCIBLE_URL/v1/routing/usage?policy=default-software" | jq -c .routing_policy)
uv run python -c 'import json, sys, yaml
document = yaml.safe_load(open("examples/policies/hades-self-hosting.yaml"))
document["routing"] = {"policy": json.loads(sys.argv[1])}
print(json.dumps(document))' "$routing" > /tmp/hades-self-hosting.json
curl -fsS -X PUT -H @<(auth) \
  -H 'Content-Type: application/json' --data @/tmp/hades-self-hosting.json \
  "$CRUCIBLE_URL/v1/policies/hades-self-hosting/1"
```

Contracts then name `{"name": "hades-self-hosting", "version": 1}`, and their
`required_verification` lists `make lint`, `make test-unit` and `make scan`. A contract
that lacks `make test-unit` is refused at submission; one that also lists `make test` is
accepted and then fails `verification_ran`, because the verifier has no Docker and no
Postgres for the integration tier. A version is
immutable once a task references it, and the admin UI's routing pages only ever write new
default-software versions: after the operator changes routing there, upload the next
version of this policy the same way (bump `version` in the document and the path).

The worker image has to carry the toolchain; a lab running a worker image from before
this change fails every such task at `verification_ran`, so promote a worker image built
from this repository's current `images/` first. `make e2e-kind-self-hosting` proves the
whole path on a disposable kind cluster.

## What the operator does alone

**The public DNS record.** Operator rule 10 and 26 item 8: creating a public DNS record
is the wall between a private lab and the internet, and it is never part of a
provisioning flow, a capability verb or a script. Nothing in this repository creates one,
and nothing in it should ever be changed so that it could.

The deployment provisions the route and reports the record that would be needed:

```
name:   crucible.<the domain filled into REPLACE_ME_LAB_DOMAIN>
type:   A or CNAME
target: the address or name of the cluster's ingress endpoint for
        REPLACE_ME_INGRESS_CLASS
```

Until the operator creates that record, the Ingress is reachable only from inside, and
`kubectl -n crucible port-forward service/crucible-api 8080:8080` is the way in. That is
a perfectly good state to run in: polling is the complete GitHub observation path and the
webhook is only an accelerator (23, Q13). `CRUCIBLE_GITHUB__WEBHOOK_ENABLED` stays
`false` until the record exists and the route terminates TLS.

## Proving a change to these manifests

```sh
make manifests     # kubectl kustomize + kubeconform + the security assertions
make deploy-kind   # the whole thing on a disposable kind cluster, one task through it
```

`make manifests` needs `kubectl` and `kubeconform` on PATH; `make deploy-kind` also needs
`kind`, `openssl` and a Docker daemon, and builds the worker image first
(`make e2e-image`).

`make manifests` also prints a resource budget line for `base`, `overlays/lab` and
`overlays/kind` (issue 93): the CPU and memory each target's Deployments, StatefulSets
and Jobs in the `crucible` namespace request, plus `crucible-workers`'s ResourceQuota
`requests.cpu` / `requests.memory`, which is the manifests' own record of what the
configured concurrency requests at once. `CRUCIBLE_CLUSTER_CPU_BUDGET` refuses a target
whose total exceeds it; it defaults to `12`, the lab's own stated shape (three 4-CPU
nodes, issue 93). `CRUCIBLE_CLUSTER_MEMORY_BUDGET_GI` does the same for memory and has
no default, because no cluster memory fact lives in this repository; set it to your
cluster's real memory to have this check mean anything for memory. Both are `make`
variables:

```sh
make manifests CRUCIBLE_CLUSTER_CPU_BUDGET=12 CRUCIBLE_CLUSTER_MEMORY_BUDGET_GI=48
```

This catches the `crucible-workers` ResourceQuota drifting from what the deployed
policy and `max_concurrency` actually request; it does not catch a policy whose request
fraction alone makes the arithmetic wrong, which is a review-time check on the policy
document, not a rendered one (spec 26, "Requests below limits").

CI runs `make manifests`, from this same definition, on every pull request. It does
**not** run `make deploy-kind`: that builds an image and stands up a kind cluster with
Calico, a TLS registry and a full task lifecycle, which is the `e2e-kind` job's cost
again, and `e2e-kind` is still establishing its own runtime and flake history. So
`make deploy-kind` is a local gate and running it is the author's job, not CI's.
When the author is a Crucible worker it is not run at all: the worker image has no
Docker, kind or kubectl (ADR 0020, CONTRIBUTING.md), so the PR says so and a person
with those tools runs it when the change warrants it.

What kind proves: the manifests are valid, the RBAC grants what the process asks for, the
volumes are there, the images pull, and a real task completes on the Kubernetes provider.
What kind cannot prove: anything cluster-specific. No ReadWriteMany storage, no ingress
controller, no certificate issuer, no lab network, no DNS. Those are seen for the first
time on the lab cluster, which is why the first sync is manual.
