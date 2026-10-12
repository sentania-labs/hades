# ADR 0032: The work bundle travels through the artifact store

Status: accepted. FDY-0627, 2026-10-12, on the operator's decision on hades #632 (with
#630 and #629). Changes ADR 0022 point 5 (where the Kubernetes publisher reads the
bundle) and the bundle sources of 23 "Publication", 26 and 08's correction resume. The
first of two contracts: making the workspace claim ephemeral is FDY-0628.

## Context

The git bundle of an attempt's work branch (`base_ref..work_branch`, sealed by its
sha256 at collection) is the one carrier of the worker's commits (23). Until now it
stayed where the collector wrote it, `output/work_branch.bundle` in the attempt's
workspace, and every later consumer went there for it:

- the Kubernetes publisher mounted that one file off the attempt's claim, named by a
  `k8s://<namespace>/ws-<attempt>/output/work_branch.bundle` path (ADR 0022);
- a correction's preparer on Kubernetes mounted the previous attempt's claim read-only
  to resume from its bundle, so a correction could only run on the provider that held
  that claim (`previous_attempt_bundle_other_provider`);
- the claim therefore had to outlive its attempt for as long as a publication or a
  correction might still need it, which is what keeps it a retained volume.

A Pod that mounts another Pod's claim couples the two Pods' storage, nodes and
lifetimes. The artifact store already holds every other thing collection keeps, scanned
and content-addressed, with an evidence row that names it.

## Decision

1. **Collection stores the bundle as an artifact.** When the collector produced a
   bundle, `record_collection_evidence` stores its bytes through `store_artifact` as the
   attempt's artifact of type `work_bundle`, name `output/work_branch.bundle`, content
   type `application/x-git-bundle`. The store's secret scanner runs over it as over any
   artifact; a match is a scanner finding and nothing is stored. The seal and the stored
   blob come from the same bytes (`collected.read_outputs` reads the file once), and the
   stored blob's sha256 is checked against the seal: an artifact whose digest is not the
   seal is never named as the bundle.
2. **The `bundle_head` evidence row names it.** The row's `artifact_id` is the bundle
   artifact, and its payload carries `bundle_artifact_id` next to the `bundle_sha256` it
   already recorded. `work_bundle_artifact` is the one reader: it returns the artifact
   only when its type is `work_bundle`, it belongs to the row's attempt, and its sha256
   is the row's seal.
3. **The fetch mechanism is the provider's exec channel.** Of the two choices the
   contract offered (an attempt-scoped read through the API, or an authenticated fetch
   the provider gives the Job), Hades takes the second, in the form the Kubernetes
   provider already uses to put a value into a Pod (a login code, k8sapi): the
   supervisor reads the blob from its artifact store, and once the Job's Pod is Running
   the provider writes it into the Pod over the `pods/exec` stdin channel, which the API
   server authenticates and authorizes as the supervisor's ServiceAccount. Nothing in
   the Pod holds a credential for Hades, the Pod needs no egress to the Hades API, and
   no new endpoint or token type exists. The bytes go in frames of at most 1 MiB, and a
   small script (`scripts.DELIVER_SCRIPT`) writes exactly the announced byte count to a
   partial file and renames it into place, so a script waiting for the bundle never
   reads half of one.
4. **The publisher Job mounts no claim.** It gets two memory-backed volumes of its own
   (`emptyDir`, medium Memory): the bundle, sized for it, and the outcome directory.
   Before anything that holds a token exists, the publisher reads the stored blob and
   refuses at `bundle-seal` if there is none or its sha256 is not the seal. The Pod's
   script (`delivered_publisher_script`) waits for the delivered bundle, runs the
   publisher script unchanged (which hashes the bundle against the seal again before any
   remote, exit 7), and then waits for the provider to read its outcome files back over
   exec, which tells it to end. That delivery and read run beside the role's deadline,
   not in place of it: a Pod that hangs before it writes its outcome is a timeout on the
   role's own schedule, and the exchange still in flight is abandoned. The outcome
   never travels through a Pod log (12), and a Job that exited 0 whose outcome could
   not be read is still reported as pushed at the expected head for Crucible's own
   remote-head check, as before. The claim check, the Job that readied the claim's
   `publish/` leaf for a push, `k8spublisher.bundle_leaf` and the `k8s://` bundle path
   form are gone. With both providers wired, the attempt's
   workspace path (not the bundle path) picks the publisher.
5. **A correction's preparer resumes from the stored bundle.** The supervisor still
   selects the last sealed bundle exactly as before, and adds its artifact's store path
   to the launch spec (`resume_bundle_artifact`). On Kubernetes the provider reads the
   blob before it creates the preparer Job, refuses the resume as "previous attempt
   bundle is gone" when there is none, and delivers it over exec into the new attempt's
   own workspace (`resume/work_branch.bundle` on its own claim). The preparer script
   waits for it and then checks it exactly as it always has: a regular file, its sha256
   against `resume_bundle_sha256`, `git bundle verify`, its head against
   `resume_bundle_head`, and descent from the published head. A bundle that does not
   match its seal still refuses with exit 4. The previous attempt's claim is never
   mounted.
6. **The provider no longer pins a correction.** A stored bundle is read from the
   artifact store by whichever provider runs the correction, so
   `previous_attempt_bundle_other_provider` is moot for one, and so is the release of the
   previous attempt's workspace. It is still returned for a bundle collected before this
   change on Docker, which only that host's workspace holds; such a bundle on Kubernetes
   is `previous_attempt_bundle_gone`, since its claim is no longer mounted for it. That is
   the removal of the claim-mount path, not a deprecation of it.
7. **The Docker provider shares the read where it shares the code path.** Its workspaces
   and the artifact store live under the same artifact root on the same host, so its
   publisher stages the stored blob (`bundle_artifact`) into its own directory and its
   preparer mounts the stored blob read-only, with no new carrier. Both still fall back
   to the bundle in the attempt's workspace directory for an attempt collected before
   this change: that directory is the host's, not a claim, nothing makes it ephemeral,
   and the host-side seal check before the container starts is unchanged.

## What remains on the attempt claim

After this change the claim holds what the attempt's own Pods share while it runs:
the checkout (`repo/`), the report, `output/` (the collector's output, including its
own copy of the bundle, which the reader Pod copies out at collection and nothing reads
afterwards), `verify/`, the package caches, the credential copy, a correction's
delivered `resume/` bundle, and `publish/`, which only a merge-main run (hades #411)
still uses for its outcome. Nothing reads the claim after collection to publish or to
resume. The claim is therefore safe to make ephemeral, gone with its attempt; doing so,
and what it means for retention, the cleaner and the merge-main outcome, is FDY-0628.
This change touches no retention rule, cleanup policy, cleaner role or storage class.

## Consequences

- Every new attempt stores its bundle once more, in the artifact store, under the same
  content addressing (the same bundle twice is one blob). Retention of these artifacts
  follows the artifact store's, unchanged here.
- Collection holds the bundle's bytes in memory once, bounded by the collected-output
  limit (256 MiB). A bundle the scanner refuses is not stored, so that attempt has
  nothing to publish or resume from; its `no_secrets` gate has failed already.
- A publication or a correction of a Kubernetes attempt collected before this change
  finds no stored bundle and is refused at `bundle-seal` or as
  `previous_attempt_bundle_gone`. A fresh attempt (a correction from the remote branch,
  or a new run) is the way forward for it.
- The publisher Pod and a resuming preparer Pod hold the bundle in memory or on their
  own claim for their own lifetime, and ask for no new RBAC: `pods/exec` is already
  granted (ADR 0022).
