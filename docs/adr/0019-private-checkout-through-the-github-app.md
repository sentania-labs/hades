# ADR 0019: A private repository is cloned with a read-only GitHub App token

Status: accepted. The operator's decision of 2026-09-27 on crucible#157 ("3: build it"),
made concrete by FDY-0124 the same day. Amends ADR 0017 decision 5, which refused private
repositories in the picker.

## Context

The preparation step clones with no credential, by design (08): the preparer's git has no
credential helper, and the worker's checkout ends with a placeholder `origin` and an
empty helper, so nothing the worker runs can push. A private GitHub repository therefore
failed at preparation on either provider. That predates the picker (crucible#156), which
refused private repositories with "private: not supported yet" rather than let a first
task fail.

The publisher already mints repository-scoped installation tokens through the App
(`appauth.py`) and hands one to its own container on stdin, onto a tmpfs, where a git
credential helper answers only for https on the one configured host (23, S10). The
question in #157 was whether the preparation step may hold a token too, and on what
terms.

## Decision

1. **A repository is public or private, and only a private one gets a token.** The
   registration records it (`repositories.private`, migration 0025). The picker takes it
   from GitHub at the moment of the pick; the free-text form, `PUT /v1/admin/repositories`,
   `PUT /v1/repositories` and `crucible admin repository register --private` state it. A
   public repository clones with no credential at all, exactly as before.
2. **The token is minted for one preparation step and nothing else.** Immediately before
   `prepare`, the supervisor mints an installation token scoped to that one repository
   with `permissions: {"contents": "read"}` (GitHub adds `metadata: read`). It is never
   cached: the publisher's per-repository cache is not used for it. As soon as `prepare`
   returns or raises, the supervisor revokes it (`DELETE /installation/token`) and empties
   the in-memory object. A failed revocation is logged; the token still expires within
   the hour.
3. **Only the two containers that talk to the remote receive it.** With the Docker
   provider the preparer container, which also refreshes the reference cache, is created
   with stdin open and a 64 KiB tmpfs at `/run/crucible-token` (mode 0700, `noexec`); the
   value is written to stdin, and the script puts it on that tmpfs, exactly as the
   publisher's is delivered. (Since hades #137, 2026-10-06, the Docker provider
   refreshes the cache in a container of its own, before the preparer, and that
   refresher receives the token the same way; the preparer mounts the cache read-only,
   as on Kubernetes. Spec 08 describes the current shape.) On Kubernetes it is a per-attempt Secret
   `checkout-<attempt>`, mounted read-only (mode 0400) at the same path into the cache
   refresher Job and the preparer Job only, and deleted as soon as the preparer's Pod is
   gone, on every path. A Secret that cannot be deleted fails the prepare, so no worker is
   ever launched beside one; it then holds a token that is revoked a moment later, and the
   retention sweep removes it. The token is never in `Env`, `Cmd`, a log, an event
   payload, a database row, or any path inside the workspace.
4. **The helper answers for one host.** The preparer's git is given the same helper as the
   publisher's: it answers `get` only for `https` on `github.credential_host` (default
   `github.com`) and ignores `store` and `erase`. A private repository whose registered
   URL is not https on that host is refused at prepare. `GIT_TRACE*` and
   `GIT_CURL_VERBOSE` are unset first, because they print the Authorization header.
5. **git stops using the token once it has cloned.** Right after the clone (and the cache
   refresh) the script removes the helper and resets its git configuration, and again
   from an `EXIT` trap, so a failing step leaves no helper behind. With the Docker
   provider it removes the token file too. On Kubernetes the file is a read-only Secret
   volume the script cannot remove: it stays mounted, readable only by this Pod, while the
   Pod runs the rest of Crucible's own script (branch, shims, sealing), and goes when the
   Pod does. The checkout's `.git/config` never held it: git keeps the clean URL, which is
   then replaced by the placeholder, and the checkout's own `credential.helper` is empty.
6. **Refusals are plain and early.** Registration of a private repository mints the token
   once and revokes it, so an App that is not connected, a missing installation id, an
   installation id GitHub does not know for the App (HTTP 404), or an installation that is
   not on the repository or cannot read its contents (HTTP 422) is refused when the
   operator registers it, in those words. The same checks
   at prepare end the attempt as an `environment` failure whose detail says why, so an App
   disconnected after registration never leaves a task waiting silently.

## Threat model

**What the preparer now holds.** For a private repository only: one installation token
that can read the contents and metadata of that one repository and do nothing else. It
cannot push, open or comment on a pull request, read another repository, or mint another
token. The App private key never leaves the Crucible process (12, ADR 0017).

**For how long.** From the moment the supervisor mints it to the moment `prepare`
returns: the length of one clone, plus the cache refresh on the same path. The provider
bounds that step (`collector_timeout_seconds` with Docker, `prepare_timeout_seconds` on
Kubernetes). Then the token is revoked at GitHub, so even a copy that escaped stops
working; if revocation fails, GitHub expires it within the hour. The in-memory object is
emptied either way. On Kubernetes the Secret exists only for that step; if its deletion
fails, the prepare fails with it, and `discard`, `cleanup` and the retention sweep delete
it again.

**Blast radius if the preparer is compromised.** The preparer runs Crucible's own script
from the worker image, on a checkout it has just cloned, with hooks off and no command the
repository defines, so the realistic compromise is a hostile worker image or a git
vulnerability triggered by the remote. With the token, such a preparer could read that
one private repository until the step ends and the token is revoked, which is no more
than the worker that follows is given anyway (the checkout itself); it cannot write to
the repository or reach another through the token. On Kubernetes its egress is the git
remote only (26: `github.com` and `api.github.com`, plus the configured credential host
when that is not GitHub). The Docker preparer is not as narrow: it runs on the workers'
network with the attempt's own proxy allowlist (the harness's endpoints, the policy's
registries, the contract's `egress_extra`), so a hostile preparer image there could send
the token to any of those hosts before it is revoked.

The reference cache widens this. It is the one volume shared across attempts, and a
private repository's mirror now lives there, keyed by its URL. Every preparer and
refresher mounts the whole cache (on Docker read-write, as before this change; on
Kubernetes only the refresher writes it, #55), so a hostile preparer image running for
any other repository's attempt can read a private repository's mirror without any
token, and on Docker could rewrite it. (Since hades #137 the Docker preparer mounts the
cache read-only too, and only the refresher container writes it, on both providers.) A worker never mounts the cache. Narrowing each
preparer to its own repository's mirror is a follow-up; until then the protection is
the same as for the harness credentials: the worker images are the operator's promoted
ones (ADR 0018), and the preparer runs only Crucible's script from them.

**Why the worker never sees it.** The worker is a different container or Pod. With
Docker, the token's tmpfs belongs to the preparer container and disappears when that
container is removed, before the worker is created, and the worker's create request has
no such mount and no open stdin. On Kubernetes the Secret is mounted into two Jobs by
name, is deleted before `prepare` returns (a prepare whose deletion failed launches no
worker), and the worker's Pod spec never references it; the
worker's ServiceAccount token is not mounted, so it cannot read Secrets through the API.
The checkout the worker gets holds no token, no helper and no URL that could carry one,
and its `origin` resolves nowhere, as before.

## Alternatives considered

- **Give the worker the token.** Rejected: 23 keeps every GitHub credential away from
  worker code, and ADR 0007 makes Crucible the only party that talks to GitHub.
- **Reuse the publisher's cached token.** Rejected: it is `contents: write` when the
  publisher needs it, and a cached token outlives the step by up to an hour.
- **Exec the token into a running preparer Pod over stdin on Kubernetes**, which would
  keep it out of the API server's storage. Rejected for now: it needs the Job's script to
  wait for a file with a timeout and races the Pod's start, for a token that is read-only,
  scoped to one repository and revoked within minutes. The per-attempt Secret is the
  pattern the harness credential copy already uses (12).
- **Clone private repositories from a deployment-maintained mirror** (23 already allows a
  fetch URL that is not GitHub). It stays possible, but it moves the credential problem to
  the operator instead of solving it.

## Consequences

- The picker registers private repositories, marked private; "private: not supported yet"
  is gone. An archived repository is still listed and refused, now marked
  "archived: cannot take a pull request".
- The App needs Contents read on every repository it should clone privately, which 23's
  permission set already includes.
- On Kubernetes the preparer and refresher of a private repository may also reach the
  credential host when a deployment sets `github.credential_host` to something other than
  `github.com` (for GitHub Enterprise Server), resolved to addresses like every other
  egress name (26).
