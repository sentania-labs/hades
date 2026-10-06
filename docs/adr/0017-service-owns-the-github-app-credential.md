# ADR 0017: The service owns the GitHub App credential, and the UI connects the App

Status: accepted. The operator's feedback of 2026-09-25 (quoted below), made concrete by
FDY-0117 the same day. Extends ADR 0015's ownership model from the harness credentials to
the GitHub App credential. Amended 2026-09-27 (crucible#168): the App manifest flow needs
no public DNS, and it is now the only way the App is connected; see "Amendment: the
one-click App" below.

## Context

Until this change the GitHub App's private key and webhook secret arrived as the
`crucible-github-app` Secret, delivered by GitOps as a SealedSecret, and the App id was a
setting (`CRUCIBLE_GITHUB__APP__APP_ID`). A deployment had to seal a key, set an id,
turn `github.enabled` on and restart, and then type every repository by hand with its
installation id (crucible#120). The mount was `optional: true`, so a name mismatch in the
sealed Secret produced pods that started clean with no key and failed only at the first
delivery (crucible#79).

The operator, 2026-09-25, on "No repository is registered. Open Repositories before
submitting work.": "It'd be nice to give it a GH cred/install it as an app and then select
repos from an org or account." And on the setup as a whole: "some of these things need to
ensure that options and choices reference other values set in the system, so that things
become populated based on available options" (crucible#121). ADR 0015 already records the
earlier decision behind it: "You should not be concerned with deployment. You are
building an app/seevice."

## Decision

1. **The service is the single writer of the GitHub App credential.** On Kubernetes it is
   the Secret `crucible-github-app` in the service's own namespace (`crucible`, named by
   `github.app.secret_name`), keys `app-id`, `app.pem` and `webhook.secret`. The service
   creates it the first time Create GitHub App writes it, labels it
   `app.kubernetes.io/managed-by: crucible` and `crucible.credential: github-app`, and
   replaces its data with one merge patch on each later write; a webhook secret is
   replaced only when one is given. With the Docker provider the same three files sit
   beside `github.app.private_key_path`, written mode 0600 by the same flow: each write
   stages the id and the key, under a lock, in a new `.versions/<v>` directory and renames one
   `.current` symlink onto it, the id file and the key path are links through `.current`,
   and a reader resolves `.current` once and reads both from that version, so a failed
   write leaves the previous pair in force and no reader pairs one App's id with
   another's key (Codex review of PR 156, 2026-09-25). GitOps no
   longer delivers the Secret; the sealed placeholder is gone from `secret-shapes/sealed`.
2. **Connect GitHub checks before it stores.** The operator enters an existing App's id
   and one of its private keys (the UI's GitHub page, `POST /v1/admin/github/app`, or
   `crucible admin github connect`). The service signs an App JWT with that key in memory
   and calls `GET /app`; a refusal, or an answer naming another App, stores nothing. The
   answer carries the App's install link, built from the `html_url` GitHub returned. The
   key is never returned, logged or audited; its public-key fingerprint is.
   (Superseded by the amendment below, 2026-09-27: the operator removed this path, and
   Create GitHub App is the only way to connect an App. The route, the CLI verb and the
   form are gone.)
3. **The key is read through the API server, on each signature.** Both the api and the
   supervisor read the Secret when they sign a JWT, so a connect is in force at once
   rather than after the kubelet's next projection of the mount. The mount stays, still
   `optional: true`, because a fresh deployment has no Secret until the operator connects
   and must start anyway (#79's "make the mount required" would stop it). It carries
   `webhook.secret` to the webhook route, which reads a file.
4. **A credential counts when the service wrote it, or when the settings say one was
   placed on purpose.** It is configured when it has a key and either an `app-id` the
   service wrote beside it, or `github.enabled` with a non-zero `github.app.app_id`. A
   deployment that sealed its own Secret before this change keeps working with its
   settings unchanged. When `github.enabled` is on and the key is missing, the process
   says so at startup and names the store (#79); the GitHub page names it too.
5. **The picker reads what the App can see.** `GET /app/installations`, then per
   installation one token scoped to `metadata: read`, used for
   `GET /installation/repositories` and discarded before the call returns. A pick
   registers the repository with the installation id, the clone URL and the default
   branch GitHub reported at that moment, and the default policy unless another is named.
   The free-text registration stays for anything the picker cannot show. A private
   repository is listed and marked "private: not supported yet", and a pick of one is
   refused with those words: the preparation step clones without a credential, so its
   first task could only fail. Private checkout is a separate operator decision.
   (Amended by ADR 0019, 2026-09-27: the operator decided to support private
   repositories, and the picker now registers them as private.)

## Alternatives considered

- **Keep GitOps as the writer and generate a sealed file in the UI.** Rejected for the
  same reason as in ADR 0015: it is hand-sealing with extra steps, and the operator asked
  to connect the App from the UI.
- **The App manifest flow** (GitHub creates a new App from a manifest and redirects back
  with its credentials). Rejected at first on the belief that it needs a public callback
  URL, which on this lab would be a public DNS record. That was wrong, and the flow is
  adopted by the amendment below.
- **Put the Secret in `crucible-workers`, where the Role already grants Secret verbs.**
  Rejected: that is the namespace untrusted worker code runs in, and 12 keeps the App key
  on the `crucible` pods only. A narrow Role in `crucible` is the smaller blast radius.
- **Read the key from the mounted file only.** A connect would then not be in force until
  the kubelet re-projected the Secret, up to a minute or more later, and the picker right
  after a connect would fail for no reason the operator could see.

## Consequences

- The control-plane ServiceAccount gains a second grant in its own namespace, beside
  deleting the first-run Secret (ADR 0016): the
  Role `crucible-github-app` (`deploy/kubernetes/base/crucible/github-app-rbac.yaml`),
  `get` and `patch` on the one Secret by name, and `create` on Secrets, which RBAC cannot
  narrow by name. No `list`, `watch`, `update` or `delete`; the database Secret stays out
  of reach. The residual risk is the pair: a compromised control-plane process could,
  before the first connect, create `crucible-github-app` itself as a service-account
  token Secret for another account in `crucible` and then read it. The service refuses to
  use or adopt a Secret of that name whose type is not `Opaque`, which keeps it from
  mistaking one for the App credential; it cannot stop a process that is already
  compromised. `crucible` holds no account with more than this one's permissions: the
  only other bound one, `crucible-migrate` (ADR 0016), may create Secrets and patch the
  first-run Secret, and cannot read any. Both Roles grant `create` on Secrets in
  `crucible`, to different accounts; neither can read the other's Secret.
- A deployment that sealed `crucible-github-app` before this change removes it from its
  GitOps repository without pruning it (Argo's prune would delete the key), or creates
  the App again from the GitHub page afterwards. On its first write the service takes the
  Secret over: it sets its labels and replaces its data.
- Rotation is Replace the App on the GitHub page: a new App, created and installed the
  same way, each registered repository registered again with the new installation (a
  registration carries its installation id, and a new App's installations are new),
  then the old App deleted on GitHub (amended 2026-09-27; it was a new key for the same
  App through the paste form of decision 2, which is removed). The store replaces the id
  and the key together, so the two can never disagree.
|  With the Docker provider the connect flow needs the directory beside
|  `github.app.private_key_path` writable by the service; compose uses
|  a named volume that `credential-init` chowns to uid 1000 (crucible#142).

## Amendment: the one-click App (2026-09-27, crucible#168)

The operator, 2026-09-27, on the GitHub page asking for an existing App's ID and `.pem`:
"What am i supposed to do here? Should an app install be like 'Click the button' install
the app? That's what happened for chronicle." And on this dispatch: "let's do a single
dispatch to get the github app working like it works on chronicle 'click here to install
the app'".

**Correction.** The manifest flow needs no public DNS record. GitHub's `redirect_url` and
`setup_url` redirect the operator's own browser, so any address the operator's browser
reaches (the internal hostname of the UI) works; the code exchange is an outbound call
from the service to GitHub. Only a webhook needs an address GitHub itself can reach, and
Crucible polls (23), so the manifest turns the webhook off.

**Decision.**

1. The GitHub page's first action is Create GitHub App, for the operator's personal
   account or an organization they name. The manifest carries the name (default `Hades-`
   and six hex characters, editable, since App names are unique on GitHub), spec 23's
   permissions exactly, no events, `hook_attributes.active: false`, `public: false`, and
   `redirect_url` (`/ui/github/callback`) and `setup_url` (`/ui/github/installed`) on the
   external URL. It is the only way to connect an App: entering an existing App's id and
   key (decision 2) is removed (see "Correction" below).
2. The external URL is the `Origin` of the operator's form post, unless the
   `github.external_url` setting (a `provider_settings` row, on the GitHub page, the admin
   API and the CLI) overrides it.
3. A start is a row of `github_manifest_states` (migration 0026): the sha256 of a random
   `state`, the sha256 of a second random value the starting browser keeps in an
   HttpOnly, `SameSite=Lax` cookie scoped to `/ui/github` (Lax, because GitHub's redirect
   back is a cross-site navigation), the administrator who started it, and an expiry 15
   minutes later. On return the row is spent in its own transaction before GitHub is
   asked, so a state is good once, and only by its own administrator in its own browser;
   a missing, spent, expired, another administrator's or another browser's return is
   refused, recorded as `admin_refused`, and GitHub is not asked. A refusal of the last
   two leaves the start unspent, so it cannot be used to cancel someone else's. The code is exchanged once
   (`POST /app-manifests/{code}/conversions`, unauthenticated: the code is the
   credential). The App ID, key and webhook secret go straight to the store of decision
   1; GitHub's OAuth client secret is dropped where the answer is read, because Crucible
   signs as the App and never as a user. Audited as `github_app_manifest_started` and
   `github_app_connected` (`via: manifest`, the key's public fingerprint, never the key);
   the access log blanks `code` and `state` in the callback's query and in sign-in's
   percent-encoded `next`, and the transport never puts the code in a log line or an
   error. A refusal after the exchange names the App GitHub made, so the operator can
   delete it there.
4. The UI session cookie is `SameSite=Strict`, so it does not come with GitHub's
   cross-site redirect. The callback and the install return answer a cookieless arrival
   with a page that reloads the same URL from Crucible's own site (with
   `Referrer-Policy: no-referrer`), which the cookie does come with; a second cookieless
   arrival goes to sign-in and back. The cookie stays Strict.
5. The flow is the UI's alone: GitHub requires a browser to post the manifest and
   receives the operator's confirmation there, so there is no admin API or CLI verb for
   it. The setting of decision 2 has all three.

**Consequences.** A deployment whose UI is reached through a proxy that rewrites the
host should set `github.external_url`. With the Docker provider the webhook secret is kept
only where `github.app.webhook_secret_path` names a file, as before; the webhook is off
either way.

**Correction (2026-09-27, the same review).** The operator: "why even have the past your
own app - it's a complicated duplicate". Decision 2 is removed rather than kept behind a
link: no form, no `POST /v1/admin/github/app`, no `crucible admin github connect`. Replace
the App offers only "Create a new App instead". The audited store path Create uses
(`github.keep`) is unchanged, and decision 4 still counts a credential a deployment
placed itself. "Connect GitHub" elsewhere in this record and in the code names the step
that stores the App, which Create GitHub App now performs.
