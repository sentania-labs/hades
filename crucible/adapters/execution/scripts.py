"""The shell the collector, the bundle verifier, and the verifier run (08, 11).

These are Crucible's own scripts, not the worker's. They run inside throwaway
containers built from the same hardened shape as a worker, so nothing here needs to
trust the tree it is reading: everything read out of the workspace is data.

Git in the collector runs with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
and the `-c` overrides 08 names, so a `.git/config` or a committed hook cannot make it
run anything.

Nothing a contract carries is ever pasted into a command line as text. Every such value
is bound to a shell variable from a single-quoted literal and referenced quoted, so a
ref, a path or a check id is data to `sh` whatever it contains. The contract refuses a
ref outside `[A-Za-z0-9._/-]` on top of that (05): quoting is what stops it executing,
validation is what stops it being an option.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crucible.domain.gates import injected_shim_text
from crucible.domain.secrets import secret_pattern_expressions
from crucible.ports.execution import (
    OUTPUT_MOUNT,
    PACKAGE_CACHE_LEAF,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFIER_CACHE_LEAF,
    VERIFY_MOUNT,
    WORK_MOUNT,
)

__all__ = [
    "ACTIVITY_SCRIPT",
    "ACTIVITY_WALK_SECONDS",
    "BUNDLE_MOUNT",
    "BUNDLE_VERIFY_SCRIPT",
    "COMMIT_HOOK_DIR",
    "MANIFEST",
    "PUBLISH_BUNDLE_LEAF",
    "PUBLISH_LEAF",
    "PUBLISH_LEAF_MARKER",
    "REPO_MOUNT",
    "VERIFY_MOUNT",
    "collector_script",
    "commit_msg_hook",
    "encode_check_id",
    "gate_probe_checkout_script",
    "gate_probe_script",
    "parse_activity",
    "preparer_script",
    "publish_leaf_script",
    "publisher_script",
    "quota_checkpoint_push_script",
    "verifier_script",
]

# The reference cache: bare mirrors keyed by repository URL, shared across attempts. The
# refresher (a Job on Kubernetes, 26; a container of its own with Docker, hades #137)
# is its one writer; every preparer mounts it read-only and clones with `--reference`.
CACHE_MOUNT = "/crucible/cache"
ORIGIN_MOUNT = "/crucible/origin"
# Where a GitHub installation token lives inside the one container that uses it: the
# publisher's (23, S10) and a private repository's preparer and cache refresher (ADR
# 0019). A tmpfs with the Docker provider, a Secret volume on Kubernetes; never a path
# inside the workspace.
TOKEN_MOUNT = "/run/crucible-token"

# git's credential helper protocol: git writes `protocol=`, `host=` and friends on
# stdin and reads `username=` and `password=` back. The helper answers only for
# https on the one configured host, because a helper that answers unconditionally hands
# the token to whatever remote git was pointed at (S10). `store` and `erase` are ignored.
#
# The helper is run through `sh` rather than executed: a Docker tmpfs is mounted
# `noexec` unless `exec` is asked for, and `/tmp` keeps `noexec`. git runs a helper
# value beginning with `!` through the shell, which is what this uses. It reads the
# token from `$CRUCIBLE_TOKEN_FILE` when git asks, so the value is never in an
# environment variable, an argument, or the helper's own text.
_CRED_HELPER_SCRIPT = r"""
cat > /tmp/cred-helper.sh <<'HELPER'
#!/bin/sh
[ "${1:-}" = "get" ] || exit 0
protocol= host=
while IFS='=' read -r key value; do
  [ -z "$key" ] && break
  case "$key" in
    protocol) protocol=$value ;;
    host) host=$value ;;
  esac
done
[ "$protocol" = "https" ] || exit 0
[ "$host" = "$CRUCIBLE_CREDENTIAL_HOST" ] || exit 0
printf 'username=x-access-token\n'
printf 'password=%s\n' "$(cat "$CRUCIBLE_TOKEN_FILE")"
HELPER
chmod 0600 /tmp/cred-helper.sh
"""

# The safe.directory exception every preparer and refresher needs (see the preparer).
_SAFE_GITCONFIG = "printf '[safe]\\n\\tdirectory = *\\n' > /tmp/gitconfig"


def _safe_git_setup() -> str:
    """Git setup shared by every Crucible-owned checkout container."""
    return f"""# git ignores `safe.directory` from the command line, and cache-backed
# directories can belong to the host uid rather than this container's uid (S9 Test E).
# Put the exception in this container's own tmpfs, from Crucible's own text.
{_SAFE_GITCONFIG}
export GIT_CONFIG_GLOBAL=/tmp/gitconfig"""


def _checkout_credential(source: str, credential_host: str) -> str:
    """ADR 0019: give git a read-only installation token for one clone, then take it back.

    `source` is where the token is: `stdin` (the Docker provider writes it to the
    container's stdin, and this puts it on the container's own tmpfs, as the publisher
    does) or `file` (the Kubernetes provider mounts it from a per-attempt Secret). The
    helper answers only for https on `credential_host`. `drop_checkout_token` removes
    the helper and resets the git configuration to the safe.directory exception alone,
    and removes the token file when it is on the container's tmpfs (a Secret volume is
    read-only and goes with the Pod); it runs once the network steps are done and again
    on exit, so no path out of the script leaves the helper behind. `GIT_TRACE*` and
    `GIT_CURL_VERBOSE` print the Authorization header, so they are unset rather than
    trusted (S10)."""
    if source not in ("stdin", "file"):
        raise ValueError(f"unknown token source {source!r}")
    receive = ""
    if source == "stdin":
        receive = """umask 077
cat > "$CRUCIBLE_TOKEN_FILE"
umask 022
"""
    return f"""unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE GIT_TRACE_PACKET GIT_TRACE2 || true
export CRUCIBLE_TOKEN_FILE={_quote(TOKEN_MOUNT + "/token")}
export CRUCIBLE_CREDENTIAL_HOST={_quote(credential_host)}
drop_checkout_token() {{
  {'rm -f "$CRUCIBLE_TOKEN_FILE"' if source == "stdin" else ":"}
  rm -f /tmp/cred-helper.sh
  {_SAFE_GITCONFIG}
}}
trap drop_checkout_token EXIT
{receive}if [ ! -s "$CRUCIBLE_TOKEN_FILE" ]; then
  echo "no checkout token arrived for this private repository" >&2
  exit 3
fi
{_CRED_HELPER_SCRIPT.strip()}
printf '[credential]\\n\\thelper = "!sh /tmp/cred-helper.sh"\\n' >> /tmp/gitconfig
"""


# The hook every worker's checkout runs on `git commit` (hades FDY-0135). It lives in the
# identity bundle, which is Crucible's own text mounted read-only, and the preparer points
# the checkout's `core.hooksPath` at that directory, so the repository's own hooks never
# run and the worker cannot edit this one. It adds `<commit_trailer>: <external_id>` to
# the message unless a trailer with that key is already there, so a harness that adds
# the trailer itself, an amend, or a second run never gets a duplicate. It reads and
# writes only the message file git hands it: no network, no repository content.
# `--no-divider`: a `---` line in a body is prose, not the start of a patch, so the
# trailer goes at the end of the message. The trailer is a courtesy: since 2026-09-29
# nothing checks it, and the task record is the paper trail.
COMMIT_HOOK_DIR = "hooks"


def commit_msg_hook(*, trailer: str, value: str) -> str:
    """The `commit-msg` hook text: the trailer key and value bound as quoted literals."""
    return f"""#!/bin/sh
# Crucible's commit-msg hook: every commit on this attempt carries its trailer.
set -eu
KEY={_quote(trailer)}
VALUE={_quote(value)}
[ -n "${{1:-}}" ] || exit 0
exec git interpret-trailers --in-place --no-divider --if-exists doNothing \\
  --if-missing add --trailer "$KEY: $VALUE" "$1"
"""


def _commit_policy_check(git: str) -> str:
    """The collector's author check, whose answer the `commit_policy` gate shows the
    reviewer (hades FDY-0135). Since 2026-09-29 it is information, not a refusal:
    neither the gate nor the publisher stops a branch on it.

    `commit_policy_check RANGE DIR` writes `DIR/author-problems.txt` (sha, tab, author
    email) for each commit in RANGE whose author email is not `$POLICY_AUTHOR_EMAIL`.
    `git` is the command the collector runs git with.

    It returns non-zero when git cannot list or read the commits, so a check that did
    not run is never taken for one that found nothing."""
    return f"""commit_policy_check() {{
  : > "$2/author-problems.txt"
  shas=$({git} rev-list "$1") || return 1
  for sha in $shas; do
    who=$({git} show -s --format='%ae' "$sha") || return 1
    if [ "$who" != "$POLICY_AUTHOR_EMAIL" ]; then
      printf '%s\\t%s\\n' "$sha" "$who" >> "$2/author-problems.txt"
    fi
  done
}}
"""


# `log.showSignature=false`: a worker-written `.git/config` could otherwise have `git
# show` verify a planted signature with a `gpg.program` of its choosing.
# Keep text visible even when the worker lowers its binary detection threshold.
GIT = (
    "git -c advice.graftFileDeprecated=false -c core.commitGraph=false "
    "-c core.fsmonitor= -c diff.external= -c core.pager=cat "
    "-c core.bigFileThreshold=512m -c core.hooksPath=/dev/null "
    "-c log.showSignature=false -c 'safe.directory=*'"
)
CHECKPOINT_GIT = (
    "git -c advice.graftFileDeprecated=false -c core.commitGraph=false "
    "-c core.fsmonitor= -c diff.external= -c core.pager=cat "
    "-c core.hooksPath=\"$EMPTY_HOOKS\" -c 'safe.directory=*'"
)
# hades #344 and #369: disable replace objects, grafts and commit graphs, so a replace
# ref or a graft the worker wrote cannot make the collected diff, the scanned content or
# the merge base show anything other than the objects the bundle and the tree carry.
# A nonexistent file also avoids older Git bundle commands emitting graft advice
# before reading advice.graftFileDeprecated.
GIT_ENV = (
    "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "
    "GIT_TERMINAL_PROMPT=0 GIT_ASKPASS= HOME=/home/worker LC_ALL=C "
    "GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/nonexistent"
)

# 08: the copy step rejects symlinks, hard links, devices, and files above the policy
# size cap, and records each rejection. `find -P` (the default) never descends through
# a symlink, so a path whose component is a symlink never reaches the copy at all.
_COPY_REPORT = r"""
copy_report() {
  src="$1"; dst="$2"; cap="$3"
  mkdir -p "$dst"
  find -P "$src" -mindepth 1 | LC_ALL=C sort | while IFS= read -r p; do
    rel=${p#"$src"/}
    if [ -h "$p" ]; then
      printf 'symlink\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    if [ -d "$p" ]; then mkdir -p "$dst/$rel"; continue; fi
    if [ ! -f "$p" ]; then
      printf 'not-a-regular-file\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    links=$(find -P "$p" -prune -printf '%n' 2>/dev/null || echo 1)
    if [ "${links:-1}" -gt 1 ]; then
      printf 'hard-link\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    size=$(wc -c < "$p")
    if [ "$size" -gt "$cap" ]; then
      printf 'over-size-cap\t%s\n' "$rel" >> "$OUT/copy-rejections.tsv"; continue
    fi
    mkdir -p "$dst/$(dirname "$rel")"
    cat < "$p" > "$dst/$rel"
  done
}
"""


# hades #191: how long the refresh waits for the remote to answer before it leaves the
# mirror as it is. A failed refresh costs time, never correctness, so it should cost
# seconds: an unanswered connect otherwise waits out the kernel's SYN retries, about 135
# seconds, once for the fetch and once more for the clone.
CACHE_REFRESH_CONNECT_SECONDS = 20

# Collected artifacts are read into memory before they enter artifact storage. Keep the
# review diff within that existing ingestion bound as well as the configured report cap.
DIFF_ARTIFACT_CAP_BYTES = 4 * 1024 * 1024

# hades #344: the collector's own output directory for the review diff. It is outside
# `report/`, which holds the worker's copied files, so neither can overwrite the other.
REVIEW_DIFF_DIR = "crucible-review"

# hades #398: every blob the worker added or changed, by object id, for the secret
# scanner. Unbounded on purpose: every byte the worker added or changed is scanned. The
# bundle carries each of these once already, so the Kubernetes reader keeps them out of
# the output archive and streams them through the scanner on their own (26).
CHANGED_BLOBS_DIR = "changed-blobs"

# Every diff the collector runs: no textconv, no external diff driver. Never `--text`
# on the raw diff: a binary must stay "Binary files differ" there, or its bytes fill the
# output. The secret scanner reads every changed blob itself (hades #398).
_DIFF_FLAGS = "--no-ext-diff --no-textconv"

# Paths whose `diff` attribute is unset (`-diff`, the `binary` macro) or names a driver:
# the worker's .gitattributes can make such a text file read as binary, so the review
# copy shows these, and only these, with `--text`.
_ATTR_DIFF_PATHSPEC = "-- . ':(exclude,attr:!diff)' ':(exclude,attr:diff)'"

# What the collector writes into its output directory. It is cleared before each run so
# nothing an earlier collection of the same attempt left is read as this one's.
_COLLECTOR_OUTPUTS = (
    "base.txt head.txt branch.txt diffstat.txt diff.patch changed.txt log.txt "
    "diff-raw.txt commit-raw.txt base-injected.txt injected-blobs "
    "injected-blob-ids.txt injected-blob-ids.sorted injected-error.txt "
    "commit-paths.txt work_branch.bundle bundle.log commits.txt commit-policy tree "
    "clone.log report copy-rejections.tsv collection-failed.txt checkpoint-refusal.txt "
    "leftover-committed.txt leftover-refusal.txt collector.ok attr-text.patch "
    f"{CHANGED_BLOBS_DIR} changed-blob-ids.txt {REVIEW_DIFF_DIR}"
)


def _cache_refresh(cache_dir: str) -> str:
    """Fetch into the bare mirror, or clone it when it is absent or will not fetch; and
    neither when the remote does not answer a ref listing within
    CACHE_REFRESH_CONNECT_SECONDS."""
    return f"""
if timeout {CACHE_REFRESH_CONNECT_SECONDS} {GIT} ls-remote -- "$CLONE_URL" HEAD >/dev/null; then
  if [ -d "{cache_dir}" ]; then
    {GIT} --git-dir "{cache_dir}" fetch --prune origin || rm -rf "{cache_dir}"
  fi
  if [ ! -d "{cache_dir}" ]; then
    {GIT} clone --mirror -- "$CLONE_URL" "{cache_dir}" || true
  fi
else
  echo "crucible: the remote did not answer within {CACHE_REFRESH_CONNECT_SECONDS}s;" \
    "the reference cache is left as it is" >&2
fi
"""


def cache_refresh_script(
    *,
    url: str,
    cache_name: str,
    checkout_token: str | None = None,
    credential_host: str = "github.com",
) -> str:
    """The one writer of the reference cache, run on its own before a preparer that
    mounts the cache read-only: the refresher Job on Kubernetes (26, crucible#55) and
    the refresher container with Docker (hades #137). A refresh that fails leaves no
    mirror rather than a half-fetched one, and the preparer then clones from the remote
    directly; so this script exits 0 either way.

    `checkout_token` names where a private repository's read-only token is (ADR 0019):
    `stdin` with the Docker provider, `file` on Kubernetes; None for a public
    repository, which fetches with no credential at all."""
    cache_dir = f"{CACHE_MOUNT}/{cache_name}.git"
    credential = ""
    if checkout_token is not None:
        credential = _checkout_credential(checkout_token, credential_host)
    return f"""set -eu
{GIT_ENV}
printf '[safe]\\n\\tdirectory = *\\n' > /tmp/gitconfig
export GIT_CONFIG_GLOBAL=/tmp/gitconfig
CLONE_URL={_quote(url)}
{credential}{_cache_refresh(cache_dir)}"""


def preparer_script(
    *,
    url: str,
    base_ref: str,
    work_branch: str,
    from_remote_branch: bool,
    cache_name: str | None,
    author_name: str,
    author_email: str,
    origin_placeholder: str,
    claude_md_wins: bool,
    shims: tuple[str, ...],
    exclude_entries: tuple[str, ...],
    identity_mount: str,
    resume_bundle: str | None = None,
    resume_bundle_head: str | None = None,
    resume_bundle_sha256: str | None = None,
    resume_bundle_ancestor: str | None = None,
    checkout_token: str | None = None,
    credential_host: str = "github.com",
) -> str:
    """Clone, position, and seal the checkout (08).

    The reference cache is a bare mirror the checkout is cloned from with
    `--dissociate`, so the checkout owns its objects and nothing shared is ever
    mounted into a worker. This script only reads the mirror, from a read-only mount;
    the refresher keeps it fresh on its own before this runs (`cache_refresh_script`:
    the Kubernetes provider's refresher Job, 26, and the Docker provider's refresher
    container, hades #137). The origin URL is replaced with a placeholder before the
    worker sees it, and no credential helper is configured, so a push cannot start.

    A private repository's clone uses a read-only installation token (ADR 0019):
    `checkout_token` names where it is (`stdin` with the Docker provider, `file` on
    Kubernetes) and None means a public repository and no credential at all.
    git stops using it once it has cloned: the helper and its configuration go before
    the checkout is positioned, and again on every exit. From a tmpfs (`stdin`) the
    token file goes with them; a Secret volume (`file`) is read-only and goes with the
    Pod.
    """
    cache_dir = f"{CACHE_MOUNT}/{cache_name}.git" if cache_name else ""
    refresh = ""
    if cache_name:
        refresh = f"""
if [ -d "{cache_dir}" ]; then
  REFERENCE="--reference {cache_dir} --dissociate"
fi
"""
    resume = "1" if from_remote_branch else "0"
    bundle_resume = ""
    if resume_bundle is not None:
        secret_patterns = " ".join(_quote(pattern) for pattern in secret_pattern_expressions())
        bundle_resume = f"""
if [ ! -f {_quote(resume_bundle)} ] || [ -L {_quote(resume_bundle)} ]; then
  printf 'previous attempt bundle is gone\\n' >&2
  exit 4
fi
ACTUAL_SEAL=$(sha256sum {_quote(resume_bundle)} | cut -d' ' -f1)
if [ "$ACTUAL_SEAL" != {_quote(resume_bundle_sha256 or "")} ]; then
  printf 'previous attempt bundle does not match its seal\\n' >&2
  exit 4
fi
{GIT} bundle verify {_quote(resume_bundle)} >/dev/null
{GIT} fetch {_quote(resume_bundle)} "$WORK_BRANCH:refs/crucible/resume"
ACTUAL_HEAD=$({GIT} rev-parse refs/crucible/resume)
if [ "$ACTUAL_HEAD" != {_quote(resume_bundle_head or "")} ]; then
  printf 'previous attempt bundle head does not match its record\\n' >&2
  exit 4
fi
# Verify both the recorded published head and the branch observed in this clone.
for ANCESTOR in {_quote(resume_bundle_ancestor or "")} "refs/remotes/origin/$WORK_BRANCH"; do
  if [ -z "$ANCESTOR" ]; then continue; fi
  if [ "$ANCESTOR" = "refs/remotes/origin/$WORK_BRANCH" ] \
    && ! {GIT} rev-parse --verify --quiet "$ANCESTOR" >/dev/null; then continue; fi
  if ! {GIT} merge-base --is-ancestor "$ANCESTOR" "$ACTUAL_HEAD"; then
    printf 'previous attempt bundle does not descend from task head %s\\n' "$ANCESTOR" >&2
    exit 4
  fi
done
{GIT} checkout -B "$WORK_BRANCH" refs/crucible/resume --
# A seal authenticates the failed tree but does not make its contents safe. Scan the
# restored tracked tree before any worker or harness credential can reach it.
for SECRET_PATTERN in {secret_patterns}; do
  if {GIT} grep -P -q -e "$SECRET_PATTERN" HEAD --; then
    printf 'previous attempt bundle contains a secret pattern; refusing restored tree\n' >&2
    exit 4
  else
    SCAN_STATUS=$?
    if [ "$SCAN_STATUS" -ne 1 ]; then
      printf 'previous attempt bundle secret scan failed\n' >&2
      exit 4
    fi
  fi
done
STARTED="$ACTUAL_HEAD"
"""
    credential = drop = ""
    if checkout_token is not None:
        credential = _checkout_credential(checkout_token, credential_host)
        # The last network step is the clone: from here on git reads only the checkout.
        drop = "drop_checkout_token\n"
    # Bound as literals, referenced quoted, and never concatenated into a command.
    bindings = "\n".join(
        (
            f"WORK_BRANCH={_quote(work_branch)}",
            f"BASE_REF={_quote(base_ref)}",
            f"CLONE_URL={_quote(url)}",
            f"ORIGIN_PLACEHOLDER={_quote(origin_placeholder)}",
            f"AUTHOR_NAME={_quote(author_name)}",
            f"AUTHOR_EMAIL={_quote(author_email)}",
            f"IDENTITY_MOUNT={_quote(identity_mount)}",
            f"CLAUDE_MD_WINS={_quote('1' if claude_md_wins else '0')}",
        )
    )
    shim_list = " ".join(_quote(name) for name in shims)
    exclude_block = "\n".join(
        f'grep -qxF {_quote(entry)} "$REPO/.git/info/exclude" '
        f"|| printf '%s\\n' {_quote(entry)} >> \"$REPO/.git/info/exclude\""
        for entry in exclude_entries
    )
    return f"""set -eu
{GIT_ENV}
# Checkout containers get it; the collector keeps GIT_CONFIG_GLOBAL=/dev/null because
# what it reads is a tree a worker wrote.
{_safe_git_setup()}
{bindings}
OUT={WORK_MOUNT}/output
REPO={WORK_MOUNT}/repo
mkdir -p "$OUT"
rm -rf "$REPO"
{credential}REFERENCE=""
{refresh}
# shellcheck disable=SC2086
{GIT} clone --no-hardlinks --no-checkout $REFERENCE -- "$CLONE_URL" "$REPO"
{drop}cd "$REPO"
# Record the trusted base before the worker can move refs. Output is mounted only
# into Crucible-owned containers, never into the worker.
if ! PREPARED_BASE=$({GIT} rev-parse --verify --quiet "refs/remotes/origin/$BASE_REF^{{commit}}" \
  || {GIT} rev-parse --verify --quiet "$BASE_REF^{{commit}}"); then
  printf 'base ref %s does not exist in the clone\\n' "$BASE_REF" >&2
  exit 3
fi
printf '%s\\n' "$PREPARED_BASE" > "$OUT/prepared-base.txt"
STARTED=""
if [ -n {_quote(resume_bundle or "")} ]; then
  :
  {bundle_resume}
elif [ "{resume}" = "1" ] \
  && {GIT} rev-parse --verify --quiet "refs/remotes/origin/$WORK_BRANCH" >/dev/null; then
  {GIT} checkout -B "$WORK_BRANCH" "origin/$WORK_BRANCH" --
  STARTED="origin/$WORK_BRANCH"
else
  if {GIT} rev-parse --verify --quiet "refs/remotes/origin/$BASE_REF" >/dev/null; then
    TARGET="refs/remotes/origin/$BASE_REF"
  elif {GIT} rev-parse --verify --quiet "$BASE_REF" >/dev/null; then
    TARGET="$BASE_REF"
  else
    printf 'base ref %s does not exist in the clone\n' "$BASE_REF" >&2
    exit 3
  fi
  {GIT} checkout -B "$WORK_BRANCH" "$TARGET" --
  STARTED="$BASE_REF"
fi
{GIT} remote set-url origin "$ORIGIN_PLACEHOLDER"
{GIT} remote set-url --push origin "$ORIGIN_PLACEHOLDER"
{GIT} config user.name "$AUTHOR_NAME"
{GIT} config user.email "$AUTHOR_EMAIL"
# hades FDY-0135: the worker's commits run Crucible's commit-msg hook, which adds the
# attempt trailer, from the read-only identity bundle; never a hook the repository has.
{GIT} config core.hooksPath "$IDENTITY_MOUNT/{COMMIT_HOOK_DIR}"
{GIT} config credential.helper ""
{GIT} config http.extraHeader ""

# 06: a shim only where the checkout has none, and every shim listed in
# .git/info/exclude so its absence from the diff stays a gate (11). The container
# writes them because the checkout belongs to container uid 1000, which is not the
# uid the Crucible process runs as in every arrangement (S9 Test E).
mkdir -p "$REPO/.git/info"
SHIM_TEXT={_quote(injected_shim_text(identity_mount))}
for shim in {shim_list}; do
  # Claude Code uses AGENTS.md only when the project has no own CLAUDE.md.
  # CLAUDE.md wins under its default instructionFiles setting. Other harnesses
  # do not read CLAUDE.md, so it does not suppress their shim.
  if [ "$CLAUDE_MD_WINS" = "1" ] && [ "$shim" = "AGENTS.md" ] \
    && {{ [ -e "$REPO/CLAUDE.md" ] || [ -L "$REPO/CLAUDE.md" ]; }}; then
    continue
  fi
  if [ ! -e "$REPO/$shim" ]; then
    printf '%s\\n' "$SHIM_TEXT" > "$REPO/$shim"
  fi
done
touch "$REPO/.git/info/exclude"
{exclude_block}

# 12: the per-attempt credential copy lives here, created by this container so it
# belongs to the worker's own uid with no other reader (S9 Test E). The provider seeds
# it through the daemon before the worker starts and removes it right after the
# sync-back.
mkdir -m 0700 -p {WORK_MOUNT}/credential
# FDY-0140: the package caches, the worker's and the verifier's, owned by the worker's
# uid like the checkout.
mkdir -p {WORK_MOUNT}/{PACKAGE_CACHE_LEAF} {WORK_MOUNT}/{VERIFIER_CACHE_LEAF}

mkdir -p "$OUT"
{GIT} rev-parse HEAD > "$OUT/prepared-head.txt"
printf '%s\n' "$STARTED" > "$OUT/started-from.txt"
"""


# The policy's commit identity (05b `git`), with the defaults every shipped policy uses.
_GIT_DEFAULTS = {
    "author_name": "crucible-worker",
    "author_email": "crucible-worker@users.noreply.github.com",
    "commit_trailer": "Crucible-Attempt",
}


def policy_git(policy: Mapping[str, Any], name: str) -> str:
    """One of the policy's `git` values: `author_name`, `author_email` or
    `commit_trailer`."""
    value = (policy.get("git") or {}).get(name)
    return str(value) if value else _GIT_DEFAULTS[name]


# FDY-0140: what the leftover commit never takes, as git pathspecs.
_LEFTOVER_EXCLUDED = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/node_modules/**",
    "**/.venv/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.tox/**",
    "**/*.egg-info/**",
    "**/.coverage",
)
_LEFTOVER_EXCLUDES = " ".join(f"':(exclude,glob){pattern}'" for pattern in _LEFTOVER_EXCLUDED)
# The same paths as positive pathspecs, to unstage what the worker already added: an
# exclusion only stops `git add` from adding, it does not take an entry out of the index.
_LEFTOVER_EXCLUDED_PATHS = " ".join(f"':(glob){pattern}'" for pattern in _LEFTOVER_EXCLUDED)


def _injected_collection_script() -> str:
    """Export raw records and bounded blobs using only the worker image's tools.

    Unicode and content classification runs in read_outputs on the service. Worker
    images, including script-harness, need no Python interpreter.
    """
    return rf'''mkdir -p "$OUT/injected-blobs"
  {GIT} -C "$REPO" diff {_DIFF_FLAGS} --raw -z --no-renames --no-abbrev "$MB" HEAD \
    > "$OUT/diff-raw.txt" || echo diff > "$OUT/injected-error.txt"
  {GIT} -C "$REPO" log {_DIFF_FLAGS} --root --full-history --diff-merges=separate \
    --topo-order --raw -z --no-renames --no-abbrev --format='' "$BASE..HEAD" \
    > "$OUT/commit-raw.txt" || echo history > "$OUT/injected-error.txt"
  {GIT} -C "$REPO" ls-tree -r --name-only -z "$MB" \
    > "$OUT/base-injected.txt" || echo base > "$OUT/injected-error.txt"
  # This is only a broad transport filter, never the gate's classifier. Export
  # every non-ASCII/control name (normalization may change it), plus ASCII names
  # containing any instruction/harness stem. Ordinary source blobs stay out.
  # Alternate headers and paths so a header-shaped path remains data.
  LC_ALL=C awk 'BEGIN {{ RS="\0" }}
    skip {{
      if (blob != "" && tolower($0) ~ /agents|claude|gemini|codex|hermes|crucible|[^ -~]/)
        print blob
      skip=0; next
    }}
    {{ sub(/^\n+/, "") }}
    /^:/ {{
      skip=1; blob=""
      if ($4 ~ /^[0-9a-f]+$/ && (length($4)==40 || length($4)==64) && $4 !~ /^0+$/)
        blob=$4
    }}' "$OUT/diff-raw.txt" "$OUT/commit-raw.txt" > "$OUT/injected-blob-ids.txt" \
    || echo blobs > "$OUT/injected-error.txt"
  sort -u "$OUT/injected-blob-ids.txt" > "$OUT/injected-blob-ids.sorted"
  while IFS= read -r blob; do
    size=$({GIT} -C "$REPO" cat-file -s "$blob" 2>/dev/null) || continue
    if [ "$size" -le 8388608 ]; then
      {GIT} -C "$REPO" cat-file blob "$blob" > "$OUT/injected-blobs/$blob" 2>/dev/null \
        || rm -f "$OUT/injected-blobs/$blob"
    fi
  done < "$OUT/injected-blob-ids.sorted"'''


def _changed_blobs_script() -> str:
    """Export each blob the worker added or changed for the secret scanner (hades #398).

    The ids come from the raw diff against the merge base, which names the new blob of
    every changed path; a deletion has none and a submodule's commit is not content
    here. `cat-file blob` reads the object as stored: the worker's attributes, textconv
    and filters never apply, so a path marked binary, or holding a NUL, is scanned as
    the bytes it holds. The service streams each file through the scanner.
    """
    return rf'''mkdir -p "$OUT/{CHANGED_BLOBS_DIR}"
  # Alternate headers and paths so a header-shaped path remains data.
  LC_ALL=C awk 'BEGIN {{ RS="\0" }}
    skip {{ skip=0; next }}
    {{ sub(/^\n+/, "") }}
    /^:/ {{
      skip=1
      if ($2 != "160000" && $5 !~ /^D/ && $4 ~ /^[0-9a-f]+$/ &&
          (length($4)==40 || length($4)==64) && $4 !~ /^0+$/)
        print $4
    }}' "$OUT/diff-raw.txt" | sort -u > "$OUT/changed-blob-ids.txt" || true
  while IFS= read -r blob; do
    {GIT} -C "$REPO" cat-file blob "$blob" > "$OUT/{CHANGED_BLOBS_DIR}/$blob" 2>/dev/null \
      || rm -f "$OUT/{CHANGED_BLOBS_DIR}/$blob"
  done < "$OUT/changed-blob-ids.txt"'''


def collector_script(
    *,
    base_ref: str,
    work_branch: str,
    size_cap_bytes: int,
    attempt_id: str = "",
    quota_checkpoint: bool = False,
    author_name: str = "crucible-worker",
    author_email: str = "crucible-worker@users.noreply.github.com",
    commit_trailer: str = "Crucible-Attempt",
    trailer_value: str = "",
) -> str:
    """Produce the full diff, the path list, the head, the log, the bundle, and a copy
    of the report directory (08). Never a push, never a network: `--network none`.

    With `attempt_id`, what the worker left uncommitted is committed first, as the
    policy's author with the trailer the worker's own commits get (`trailer_value`, the
    task's external id as the commit hook writes it; the attempt id when none is given),
    so edits a model forgot to commit are collected and reviewed rather than lost
    (FDY-0140). A quota checkpoint (16) is the
    same commit with a `wip` subject; it is refused, and the collection fails, when the
    repository's `.git` is not a plain directory or the commit cannot be made. Otherwise
    such a commit is skipped with a note and the collection goes on with what the worker
    committed itself.

    It also checks each new commit's author (from the remote work branch when the
    checkout has one, else base_ref), so the `commit_policy` gate can show the reviewer
    a commit authored by someone other than the policy's author (hades FDY-0135). The
    answer goes to `commit-policy/`, with `checked` written only once the check has
    finished."""
    return f"""set -eu
{GIT_ENV}
OUT={OUTPUT_MOUNT}
REPO={REPO_MOUNT}
WORK_BRANCH={_quote(work_branch)}
BASE_REF={_quote(base_ref)}
SIZE_CAP={_quote(str(size_cap_bytes))}
DIFF_ARTIFACT_CAP={_quote(str(min(size_cap_bytes, DIFF_ARTIFACT_CAP_BYTES)))}
COMMIT_ATTEMPT={_quote(attempt_id)}
QUOTA={_quote("1" if quota_checkpoint else "")}
POLICY_AUTHOR_EMAIL={_quote(author_email)}
TRAILER={_quote(commit_trailer)}
TRAILER_VALUE={_quote(trailer_value or attempt_id)}
REVIEW_DIFF_ERROR=
mkdir -p "$OUT"
for stale in {_COLLECTOR_OUTPUTS}; do rm -rf "$OUT/$stale"; done
: > "$OUT/copy-rejections.tsv"
{_COPY_REPORT}
{_commit_policy_check(GIT + ' -C "$REPO"')}LEFTOVER=0
refuse_checkpoint() {{
  if [ -n "$QUOTA" ]; then
    printf '%s\n' "$1" > "$OUT/checkpoint-refusal.txt"
    printf '%s\n' "$1" >&2
    exit 4
  fi
  printf 'uncommitted work was not committed: %s\n' "$1" | tee "$OUT/leftover-refusal.txt" >&2
  LEFTOVER=0
}}
if [ -n "$COMMIT_ATTEMPT" ]; then
  LEFTOVER=1
  if [ ! -d "$REPO/.git" ] || [ -L "$REPO/.git" ]; then
    refuse_checkpoint "checkpoint refused: repository .git is not a real directory"
  elif [ -e "$REPO/.git/commondir" ] || [ -L "$REPO/.git/commondir" ]; then
    refuse_checkpoint "checkpoint refused: repository .git contains a commondir redirect"
  elif [ ! -f "$REPO/.git/config" ] || [ -L "$REPO/.git/config" ]; then
    refuse_checkpoint "checkpoint refused: repository .git/config is not a regular file"
  fi
fi
if [ "$LEFTOVER" = "1" ]; then
  # The worker can edit both .git/config and .gitattributes. Replace its local
  # configuration while Git stages and commits, then restore it before collection
  # continues. With no filter.* commands, a filter attribute is a no-op. The author
  # comes from the environment, so no value the policy carries is parsed as config.
  ORIGINAL_CONFIG=/tmp/crucible-worker-git-config.$$
  EMPTY_HOOKS=/tmp/crucible-empty-hooks.$$
  cp "$REPO/.git/config" "$ORIGINAL_CONFIG"
  restore_worker_git_config() {{
    rm -f "$REPO/.git/config"
    cp "$ORIGINAL_CONFIG" "$REPO/.git/config"
    rm -rf "$ORIGINAL_CONFIG" "$EMPTY_HOOKS"
  }}
  trap restore_worker_git_config EXIT
  trap 'restore_worker_git_config; trap - EXIT; exit 4' HUP INT TERM
  rm -f "$REPO/.git/config"
  mkdir -p "$EMPTY_HOOKS"
  cat > "$REPO/.git/config" <<EOF
[core]
  repositoryformatversion = 0
  bare = false
  logallrefupdates = true
  hooksPath = $EMPTY_HOOKS
  fsmonitor = false
[commit]
  gpgsign = false
[tag]
  gpgsign = false
EOF
  if [ -n "$QUOTA" ]; then
    SUBJECT="wip(crucible): attempt $COMMIT_ATTEMPT"
  else
    SUBJECT="crucible: commit what attempt $COMMIT_ATTEMPT left uncommitted"
  fi
  checkpoint_git() {{
    GIT_DIR="$REPO/.git" GIT_COMMON_DIR="$REPO/.git" \\
      GIT_AUTHOR_NAME={_quote(author_name)} GIT_AUTHOR_EMAIL={_quote(author_email)} \\
      GIT_COMMITTER_NAME={_quote(author_name)} GIT_COMMITTER_EMAIL={_quote(author_email)} \\
      {CHECKPOINT_GIT} -C "$REPO" "$@"
  }}
  # Build output and caches a repository forgot to ignore are never swept in: the
  # commit is for the worker's edits, not what running its checks left behind.
  if ! checkpoint_git add -A -- . {_LEFTOVER_EXCLUDES}; then
    refuse_checkpoint "checkpoint refused: the working tree could not be staged"
  elif ! checkpoint_git reset -q -- {_LEFTOVER_EXCLUDED_PATHS}; then
    refuse_checkpoint "checkpoint refused: build output could not be unstaged"
  elif checkpoint_git diff --cached --quiet; then
    :
  elif checkpoint_git commit -q -m "$SUBJECT" -m "$TRAILER: $TRAILER_VALUE"; then
    printf '%s\n' "$COMMIT_ATTEMPT" > "$OUT/leftover-committed.txt"
  else
    refuse_checkpoint "checkpoint refused: the working tree could not be committed"
  fi
  restore_worker_git_config
  trap - EXIT HUP INT TERM
fi
rm -rf "$OUT/commit-policy"
# Never resolve the base from refs in the worker-controlled checkout.
BASE=$(cat "$OUT/prepared-base.txt" 2>/dev/null || true)
if ! printf '%s\\n' "$BASE" | grep -Eq '^([0-9a-f]{{40}}|[0-9a-f]{{64}})$' \
  || [ "$({GIT} -C "$REPO" cat-file -t "$BASE" 2>/dev/null || true)" != "commit" ]; then
  printf '%s\\n' "collection failed: prepared base commit is missing or invalid" \
    > "$OUT/collection-failed.txt"
  cat "$OUT/collection-failed.txt" >&2
  exit 1
fi
printf '%s\\n' "$BASE" > "$OUT/base.txt"
{GIT} -C "$REPO" rev-parse HEAD > "$OUT/head.txt"
{GIT} -C "$REPO" rev-parse --abbrev-ref HEAD > "$OUT/branch.txt"
if [ -n "$BASE" ]; then
  if ! MB=$({GIT} -C "$REPO" merge-base "$BASE" HEAD); then
    printf '%s\\n' "collection failed: cannot resolve merge base between $BASE and HEAD" \
      > "$OUT/collection-failed.txt"
    cat "$OUT/collection-failed.txt" >&2
    exit 1
  fi
  # Resolving MB explicitly gives diff --name-only "$BASE"...HEAD semantics for
  # all three diffs, and fails collection if the histories are unrelated. Commits
  # the base branch gained after the fork do not become worker changes.
  # The log stays two-dot: it enumerates only commits
  # reachable from HEAD and not BASE, and unions in every path those commits touched.
  # `--no-textconv --no-ext-diff`: the worker's own .git/config is back in place, so
  # a diff.<driver>.textconv or external command it set with a matching .gitattributes
  # would otherwise run here and could hide hunks from every diff. A path the worker's
  # attributes mark binary still reads "Bin" in the stat (git's --stat ignores --text);
  # the review copy shows its hunks, see build_review_diff.
  {GIT} -C "$REPO" diff {_DIFF_FLAGS} --stat "$MB" HEAD > "$OUT/diffstat.txt" \
    || REVIEW_DIFF_ERROR="git diff --stat failed"
  {GIT} -C "$REPO" diff {_DIFF_FLAGS} --no-color "$MB" HEAD > "$OUT/diff.patch" \
    || REVIEW_DIFF_ERROR="git diff failed"
  {GIT} -C "$REPO" diff {_DIFF_FLAGS} --name-only -z "$MB" HEAD > "$OUT/changed.txt" || true
  {GIT} -C "$REPO" log --format='%H%x1f%s%x1f%an%x1e' "$BASE"..HEAD > "$OUT/log.txt" || true
  # --root (hades #369): a root commit's files are listed whatever log.showRoot the
  # worker-writable .git/config sets.
  {GIT} -C "$REPO" log --root --name-only -z --format='' "$BASE"..HEAD \
    | LC_ALL=C sort -zu > "$OUT/commit-paths.txt" || true
  # #400: classify every raw path before filtering; Git pathspecs cannot normalize
  # Unicode. Read blobs by object id, including those in earlier commits.
  {_injected_collection_script()}
  {_changed_blobs_script()}
  {GIT} -C "$REPO" bundle create "$OUT/work_branch.bundle" \
    "$BASE..$WORK_BRANCH" > "$OUT/bundle.log" 2>&1 || true
  {GIT} -C "$REPO" rev-list --count "$BASE"..HEAD > "$OUT/commits.txt" \
    || echo 0 > "$OUT/commits.txt"
  if {GIT} -C "$REPO" rev-parse --verify --quiet "refs/remotes/origin/$WORK_BRANCH" \
      >/dev/null; then
    POLICY_FROM="refs/remotes/origin/$WORK_BRANCH"
  else
    POLICY_FROM="$BASE"
  fi
  mkdir -p "$OUT/commit-policy"
  if commit_policy_check "$POLICY_FROM..HEAD" "$OUT/commit-policy"; then
    echo done > "$OUT/commit-policy/checked"
  fi
else
  REVIEW_DIFF_ERROR="the base ref could not be resolved"
  : > "$OUT/diffstat.txt"; : > "$OUT/diff.patch"; : > "$OUT/changed.txt"
  : > "$OUT/log.txt"; : > "$OUT/commit-paths.txt"; echo 0 > "$OUT/commits.txt"
  : > "$OUT/diff-raw.txt"; : > "$OUT/commit-raw.txt"; : > "$OUT/base-injected.txt"
fi
# A fresh tree from the collected state, which is what the verifier runs against (11).
rm -rf "$OUT/tree"
{GIT} clone --no-hardlinks --quiet "$REPO" "$OUT/tree" > "$OUT/clone.log" 2>&1 || true
copy_report "{REPORT_MOUNT}" "$OUT/report" "$SIZE_CAP"
# hades #344: the review diff, written where only the collector writes (never under the
# worker's report directory) and separate from the raw diff above, whose content and
# location are inputs to existing gates. It streams the stat and the patch already on
# disk into a bounded copy with an explicit marker when it cannot hold the whole patch,
# and any failure leaves a short marker rather than failing the collection.
build_review_diff() {{
  target="$OUT/{REVIEW_DIFF_DIR}/diff.patch"
  rm -rf "$OUT/{REVIEW_DIFF_DIR}" || return 1
  mkdir -p "$OUT/{REVIEW_DIFF_DIR}" || return 1
  if [ -n "$REVIEW_DIFF_ERROR" ]; then
    printf 'diff unavailable: %s\n' "$REVIEW_DIFF_ERROR" > "$target" || return 1
    return 0
  fi
  # Paths the worker's attributes keep out of a text diff, as text, ahead of the whole
  # patch so a large diff cannot push them out of the bounded copy. `head -c` bounds
  # what is read, so a large binary marked this way costs no more than the cap.
  attr_text="$OUT/attr-text.patch"
  {{ {GIT} -C "$REPO" diff {_DIFF_FLAGS} --text --no-color "$MB" HEAD \
      {_ATTR_DIFF_PATHSPEC} || echo "[crucible: attribute-marked paths unavailable]"; }} \
    | head -c "$DIFF_ARTIFACT_CAP" > "$attr_text" || return 1
  attr_heading=''
  if [ -s "$attr_text" ]; then
    attr_heading='[crucible: paths .gitattributes marks binary or gives a diff driver, as text]'
  fi
  review_body() {{
    cat "$OUT/diffstat.txt"; printf '\n'
    if [ -n "$attr_heading" ]; then
      printf '%s\n' "$attr_heading"; cat "$attr_text"; printf '\n[crucible: full diff]\n'
    fi
    cat "$OUT/diff.patch"
  }}
  marker='[crucible: diff truncated]'
  stat_size=$(wc -c < "$OUT/diffstat.txt") || return 1
  patch_size=$(wc -c < "$OUT/diff.patch") || return 1
  body_size=$((stat_size + 1 + patch_size))
  if [ -n "$attr_heading" ]; then
    attr_size=$(wc -c < "$attr_text") || return 1
    framing=$(printf '%s\n\n[crucible: full diff]\n' "$attr_heading" | wc -c)
    body_size=$((body_size + attr_size + framing))
  fi
  if [ "$body_size" -le "$DIFF_ARTIFACT_CAP" ]; then
    review_body > "$target" || return 1
    rm -f "$attr_text"
    return 0
  fi
  keep=$((DIFF_ARTIFACT_CAP - $(printf '\n%s\n' "$marker" | wc -c)))
  if [ "$keep" -gt 0 ]; then
    review_body | head -c "$keep" > "$target" || return 1
    printf '\n%s\n' "$marker" >> "$target" || return 1
  else
    printf '%s\n' "$marker" | head -c "$DIFF_ARTIFACT_CAP" > "$target" || return 1
  fi
  rm -f "$attr_text"
}}
if ! build_review_diff 2>/dev/null; then
  rm -rf "$OUT/{REVIEW_DIFF_DIR}" "$OUT/attr-text.patch" 2>/dev/null || true
  mkdir -p "$OUT/{REVIEW_DIFF_DIR}" 2>/dev/null || true
  printf 'diff unavailable: %s\n' "the review diff could not be written" \
    > "$OUT/{REVIEW_DIFF_DIR}/diff.patch" 2>/dev/null || true
fi
echo done > "$OUT/collector.ok"
"""


def quota_checkpoint_push_script(work_branch: str) -> str:
    """Push an already collected checkpoint after Crucible's safety gates pass."""
    return f"""set -eu
{GIT_ENV}
REPO={REPO_MOUNT}
ORIGIN={ORIGIN_MOUNT}
WORK_BRANCH={_quote(work_branch)}
HEAD=$({GIT} -C "$REPO" rev-parse HEAD)
{GIT} -C "$REPO" remote set-url origin "$ORIGIN"
{GIT} -C "$REPO" remote set-url --push origin "$ORIGIN"
{GIT} -C "$REPO" \
  -c 'remote.origin.receivepack=git -c safe.directory=* receive-pack' \
  push origin "HEAD:refs/heads/$WORK_BRANCH"
REMOTE=$({GIT} --git-dir "$ORIGIN" rev-parse "refs/heads/$WORK_BRANCH")
test "$REMOTE" = "$HEAD"
"""


# FDY-0140: what the Kubernetes provider runs inside a live worker to see whether it is
# working. The checkout, the report directory and the home directory, where a harness
# keeps its own session state (Hermes writes its session store after every model turn
# while `-z` writes nothing to stdout), are walked without crossing into another
# filesystem, so a credential mount under the home is never entered. It prints one
# line: the newest modification time in microseconds, the entry count and the total
# bytes. Nothing is written and nothing the worker controls is run.
ACTIVITY_MARKER = "# crucible: activity"
# The walk is bounded in time: a tree too large to walk in ACTIVITY_WALK_SECONDS answers
# `incomplete`, which the supervisor reads as "could not tell", never as activity. find
# exits 1 when it could not read a path, which is still a whole walk.
ACTIVITY_WALK_SECONDS = 10
ACTIVITY_SCRIPT = f"""{ACTIVITY_MARKER}
{{ timeout {ACTIVITY_WALK_SECONDS} find {REPO_MOUNT} {REPORT_MOUNT} "${{HOME:-/home/worker}}" \\
    -xdev -printf '%T@ %s\\n' 2>/dev/null; echo "status $?"; }} \\
  | awk 'BEGIN {{ newest = 0; files = 0; total = 0; status = 2 }}
         $1 == "status" {{ status = $2; next }}
         {{ if ($1 + 0 > newest) newest = $1 + 0; files += 1; total += $2 }}
         END {{
           if (status > 1) {{ print "incomplete"; exit }}
           printf "activity %.0f %.0f %.0f\\n", newest * 1000000, files, total
         }}'
"""


def parse_activity(stdout: bytes) -> tuple[int, int, int] | None:
    """The fingerprint ACTIVITY_SCRIPT printed, or None for anything else."""
    fields = stdout.decode("ascii", "replace").split()
    if len(fields) != 4 or fields[0] != "activity":
        return None
    try:
        newest, files, total = (int(value) for value in fields[1:])
    except ValueError:
        return None
    return newest, files, total


BUNDLE_VERIFY_SCRIPT = f"""set -eu
{GIT_ENV}
if [ ! -s {OUTPUT_MOUNT}/work_branch.bundle ]; then
  echo "no bundle was produced" >&2
  exit 2
fi
# `git bundle verify` checks the bundle's prerequisites against a repository, so it
# runs from the fresh tree the collector made, which holds base_ref. The mount is
# read-only and this container runs no command the repository defines.
cd {OUTPUT_MOUNT}/tree
{GIT} bundle verify {OUTPUT_MOUNT}/work_branch.bundle
"""


# The characters a verification id may keep in a file name. Everything else is
# percent-encoded, so two ids that differ only outside this set still get two files:
# `a/b` becomes `a%2Fb` and `a_b` stays `a_b`, which a plain substitution would have
# collapsed into one file and one exit code.
_FILENAME_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
MANIFEST = "ids.tsv"


def encode_check_id(check_id: str) -> str:
    """Percent-encode a verification id into a file name, reversibly and injectively."""
    out: list[str] = []
    for char in check_id:
        if char in _FILENAME_SAFE and not (char == "." and not out):
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return "".join(out) or "%00"


def verifier_script(checks: list[tuple[str, str]]) -> str:
    """Re-run each `required_verification` command from the collected tree (11).

    Each command's exit, log and wall-clock seconds go to the verify directory, which is
    the only place this container may write besides its own tree copy. The commands come from the
    repository, so this container is the one that runs worker-influenced code: it
    never sees the collector's output directory, only its own tree.

    The command is executed as the contract gave it, which is the point of the gate.
    Everything else, the id and the file names it becomes, is bound as a shell variable
    from a literal and never concatenated into a command."""
    lines = [
        "set -u",
        f"cd {REPO_MOUNT}",
        "export HOME=/home/worker LC_ALL=C",
        f"V={_quote(VERIFY_MOUNT)}",
        'mkdir -p "$V"',
        f'MANIFEST="$V/{MANIFEST}"',
        ': > "$MANIFEST"',
    ]
    for check_id, command in checks:
        encoded = encode_check_id(check_id)
        lines.append(f"ID={_quote(check_id)}")
        lines.append(f"F={_quote(encoded)}")
        lines.append(f"CMD={_quote(command)}")
        lines.append('printf \'%s\\t%s\\n\' "$F" "$ID" >> "$MANIFEST"')
        lines.append('printf \'%s\\n\' "$CMD" > "$V/$F.cmd"')
        # hades #184: the wall-clock seconds each command took, measured in this
        # container, so the evidence says what a required check costs where it runs.
        lines.append("S=$(date +%s)")
        lines.append('sh -c "$CMD" > "$V/$F.log" 2>&1; echo $? > "$V/$F.exit"')
        lines.append('echo $(( $(date +%s) - S )) > "$V/$F.seconds"')
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


# ----- the publisher (23, S10) -------------------------------------------

BUNDLE_MOUNT = "/crucible/bundle"
PUBLISH_MOUNT = "/crucible/publish"

# The two leaves of a Kubernetes workspace claim the publisher mounts as subPaths: the
# bundle, where the collector writes it, and the directory its outcome files go to.
PUBLISH_BUNDLE_LEAF = "output/work_branch.bundle"
PUBLISH_LEAF = "publish"
# The line that names the claim-leaf script, so a reader of a Job (or the fake cluster)
# can tell it from the preparer's checkout script, which runs in the same role.
PUBLISH_LEAF_MARKER = "# crucible: prepare the publish leaf"


def publish_leaf_script(root: str = WORK_MOUNT) -> str:
    """Make the claim ready for a publisher Pod, or say by exit code why it is not.

    A subPath that does not exist is created by the kubelet as root when the Pod starts,
    and on NFS that directory may be unwritable by the worker uid or refused outright, so
    the Pod fails setup with nothing in its log. Neither leaf the publisher mounts is left
    to the kubelet: this runs as the worker uid with the whole claim mounted, as the
    preparer that made `output/` does, and creates `publish/` with `output/`'s mode.
    A setgid bit is kept, never stripped: on the fsGroup claim `publish/` inherits it
    from its parent, and it keeps the publisher's files in the fsGroup as `output/`'s
    are (hades #184). It grants no one access.

    Exit 7: no bundle, or not a regular file (the kubelet would make a directory there).
    Exit 8: `publish/` is not a directory the worker uid owns and can write, as it owns
    and writes `output/`."""
    bundle = _quote(PUBLISH_BUNDLE_LEAF)
    leaf = _quote(PUBLISH_LEAF)
    return f"""set -eu
{PUBLISH_LEAF_MARKER}
cd {_quote(root)}
if [ ! -f {bundle} ] || [ -L {bundle} ]; then
  echo "no branch bundle at {PUBLISH_BUNDLE_LEAF} on the workspace claim" >&2; exit 7
fi
if [ -L {leaf} ] || {{ [ -e {leaf} ] && [ ! -d {leaf} ]; }}; then
  echo "{PUBLISH_LEAF} on the workspace claim is not a directory" >&2; exit 8
fi
mkdir -p {leaf}
chmod "$(stat -c '%a' output)" {leaf} 2>/dev/null || true
if [ "$(stat -c '%u' {leaf})" != "$(stat -c '%u' output)" ] || [ ! -w {leaf} ]; then
  echo "{PUBLISH_LEAF} on the workspace claim is not owned and writable as output is" >&2
  exit 8
fi
"""


# The publisher's git configuration: the helper above, no hooks, no pager, and the
# policy's author. The value of `helper` has spaces, so it lives in a git config file in
# the container's own tmpfs rather than on a command line, which is the same shape the
# preparer uses (C3).
_CRED_HELPER = (
    _CRED_HELPER_SCRIPT
    + r"""{
  printf '[credential]\n\thelper = "!sh /tmp/cred-helper.sh"\n'
  printf '[core]\n\thooksPath = /dev/null\n\tpager = cat\n'
  printf '[user]\n\tname = %s\n\temail = %s\n' \
    "$CRUCIBLE_AUTHOR_NAME" "$CRUCIBLE_AUTHOR_EMAIL"
} > /tmp/gitconfig
chmod 0600 /tmp/gitconfig
export GIT_CONFIG_GLOBAL=/tmp/gitconfig
"""
)


def publisher_script(
    *,
    clone_url: str,
    work_branch: str,
    base_ref: str,
    expected_head: str,
    author_name: str,
    author_email: str,
    credential_host: str = "github.com",
    token_source: str = "stdin",
    bundle_sha256: str = "",
    owned_remote_heads: tuple[str, ...] = (),
) -> str:
    """Fetch the base from the remote and the branch from the bundle, then push (23).

    The bundle is `base_ref..work_branch`, so it names prerequisite commits and neither
    `git bundle verify` nor `git fetch` will look at it until the repository has them.
    The publisher fetches `base_ref` from the real remote first, which is the only tree
    it ever sees: it never touches the worker's checkout or the worker's `.git`, and the
    bundle is the only carrier of the worker's commits.

    The token arrives on stdin and is written to a tmpfs file before anything else
    happens; `docker cp` cannot reach a tmpfs inside a read-only container and even
    without `--read-only` it targets the writable layer, which is disk (S10). Nothing
    here ever carries the value on argv: the credential helper reads the file, and no
    curl runs at all, because the API calls stay on Crucible's side.

    `GIT_TRACE*` and `GIT_CURL_VERBOSE` print the Authorization header, so the script
    unsets them rather than trusting the environment it inherited (S10 risks).

    `token_source` is `stdin` for the Docker provider, as above, or `file` for the
    Kubernetes provider, which mounts the token from a per-push Secret at the same path
    (a Secret volume is memory-backed and read-only, so the script neither writes nor
    removes it; the Secret's deletion is the removal). `bundle_sha256` is the seal the
    collector recorded: the bundle is hashed here, inside the container, before any
    remote is contacted, and a bundle that no longer matches is refused (exit 7)."""
    if token_source not in ("stdin", "file"):
        raise ValueError(f"unknown token source {token_source!r}")
    receive = 'cat > "$TOKDIR/token"\n' if token_source == "stdin" else ""
    secure = 'chmod 0600 "$TOKDIR/token"\n' if token_source == "stdin" else ""
    drop = 'rm -f "$TOKDIR/token"' if token_source == "stdin" else ":"
    return f"""set -eu
umask 077
TOKDIR={_quote(TOKEN_MOUNT)}
OUT={_quote(PUBLISH_MOUNT)}
BUNDLE={_quote(BUNDLE_MOUNT)}/work_branch.bundle
WORK_BRANCH={_quote(work_branch)}
BASE_REF={_quote(base_ref)}
EXPECTED={_quote(expected_head)}
CLONE_URL={_quote(clone_url)}
SEAL={_quote(bundle_sha256)}
OWNED_HEADS={_quote(" ".join(owned_remote_heads))}
drop_token() {{ {drop}; }}
mkdir -p "$OUT"
# A retried publication of the same attempt writes into the same directory; nothing a
# previous run left may be read back as this run's outcome.
find "$OUT" -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + 2>/dev/null || true
{receive}if [ ! -s "$TOKDIR/token" ]; then
  echo "no token arrived for this push" > "$OUT/error.txt"; echo no-token > "$OUT/step.txt"; exit 3
fi
{secure}# Back to the ordinary mask before anything is written to the output directory: what
# lands there is Crucible's own record of the run, and under the rootless daemon this
# container's uid is not the one that reads it back (S9 Test E).
umask 022
stat -L -c '%a' "$TOKDIR/token" > "$OUT/token-mode.txt"
unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE GIT_TRACE_PACKET GIT_TRACE2 || true
export GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
export HOME=/home/worker LC_ALL=C
export CRUCIBLE_TOKEN_FILE="$TOKDIR/token"
export CRUCIBLE_CREDENTIAL_HOST={_quote(credential_host)}
export CRUCIBLE_AUTHOR_NAME={_quote(author_name)}
export CRUCIBLE_AUTHOR_EMAIL={_quote(author_email)}
{_CRED_HELPER}
# The token must be readable by this uid through the very helper the push will use, or
# the push fails at the remote with an authentication error that says nothing about why.
# Only whether a password came back is recorded; the value goes to grep and nowhere else.
echo credential > "$OUT/step.txt"
if ! printf 'protocol=https\\nhost=%s\\n\\n' "$CRUCIBLE_CREDENTIAL_HOST" \\
    | git credential fill 2>> "$OUT/publisher.log" | grep -q '^password=.'; then
  echo "the credential helper could not read the token" > "$OUT/error.txt"; drop_token; exit 3
fi
cd /home/worker
rm -rf publish && mkdir publish && cd publish
echo bundle-seal > "$OUT/step.txt"
if [ ! -f "$BUNDLE" ]; then
  echo "no branch bundle where the collector left it" > "$OUT/error.txt"; drop_token; exit 7
fi
if [ -n "$SEAL" ]; then
  ACTUAL=$(sha256sum "$BUNDLE" | cut -d' ' -f1)
  if [ "$ACTUAL" != "$SEAL" ]; then
    echo "the branch bundle no longer matches its sealed sha256" > "$OUT/error.txt"
    drop_token; exit 7
  fi
fi
echo init > "$OUT/step.txt"
git init --quiet -b "$BASE_REF" >> "$OUT/publisher.log" 2>&1
git remote add origin "$CLONE_URL"
echo fetch-base > "$OUT/step.txt"
git fetch --quiet origin "refs/heads/$BASE_REF:refs/remotes/origin/$BASE_REF" \
  >> "$OUT/publisher.log" 2>&1
echo bundle-verify > "$OUT/step.txt"
git bundle verify "$BUNDLE" >> "$OUT/publisher.log" 2>&1
echo fetch-bundle > "$OUT/step.txt"
git fetch --quiet "$BUNDLE" "refs/heads/$WORK_BRANCH:refs/heads/crucible-publish" \
  >> "$OUT/publisher.log" 2>&1
HEAD_SHA=$(git rev-parse refs/heads/crucible-publish)
printf '%s\n' "$HEAD_SHA" > "$OUT/bundle-head.txt"
if [ "$HEAD_SHA" != "$EXPECTED" ]; then
  echo "the bundle head $HEAD_SHA is not the collected head $EXPECTED" > "$OUT/error.txt"
  echo head-mismatch > "$OUT/step.txt"; exit 4
fi
echo fetch-work-branch > "$OUT/step.txt"
# Listing errors are not evidence that the branch is absent.
git ls-remote --heads origin "refs/heads/$WORK_BRANCH" > "$OUT/ls-remote-before.txt" \
  2>> "$OUT/publisher.log"
REMOTE=""
if [ -s "$OUT/ls-remote-before.txt" ]; then
  git fetch --quiet --no-tags origin "refs/heads/$WORK_BRANCH" \
    >> "$OUT/publisher.log" 2>&1
  REMOTE=$(git rev-parse FETCH_HEAD)
fi
printf '%s\n' "$REMOTE" > "$OUT/remote-head-before.txt"
if [ -n "$REMOTE" ]; then
  echo remote-ownership > "$OUT/step.txt"
  OWNED=no
  case " $OWNED_HEADS " in *" $REMOTE "*) OWNED=yes ;; esac
  if git show -s --format='%(trailers:key=Crucible-Attempt,valueonly)' "$REMOTE" \
      | grep -q '[^[:space:]]'; then
    OWNED=yes
  fi
  if [ "$OWNED" != yes ]; then
    AUTHOR=$(git show -s --format='%an <%ae>' "$REMOTE")
    printf 'foreign remote commit %s by %s; no Hades push record or attempt trailer\n' \
      "$REMOTE" "$AUTHOR" > "$OUT/error.txt"
    drop_token; exit 5
  fi
fi
echo push > "$OUT/step.txt"
# Hades owns its work branches (issue 403): a tip it pushed, such as a quota checkpoint
# of ungated partial work, is replaced by the accepted head whether or not the head
# descends from it. An empty lease requires the branch to remain absent; an exact tip
# protects against every writer racing the fetch, including another Hades checkpoint.
if git push --quiet origin "refs/heads/crucible-publish:refs/heads/$WORK_BRANCH" \
    --force-with-lease="refs/heads/$WORK_BRANCH:$REMOTE" \
    2> "$OUT/push.err"; then
  echo ok > "$OUT/push.txt"
else
  echo failed > "$OUT/push.txt"
  cp "$OUT/push.err" "$OUT/error.txt" 2>/dev/null || true
  echo push > "$OUT/step.txt"
  drop_token
  exit 5
fi
echo done > "$OUT/step.txt"
drop_token
chmod 0644 "$OUT"/* 2>/dev/null || true
exit 0
"""


# The line that names a merge-main run, so a reader of a publisher Job (or the fake
# cluster) can tell it from a push of a bundle, which runs in the same role.
MERGE_MAIN_MARKER = "# crucible: merge the base into the remote work branch"
# Its exit codes beside the publisher's: the remote branch moved off the known tip (4),
# and git stopped on conflicts (6). Neither pushed anything.
MERGE_MAIN_HEAD_MOVED = 4
MERGE_MAIN_CONFLICT = 6


def merge_main_script(
    *,
    clone_url: str,
    work_branch: str,
    base_ref: str,
    expected_head: str,
    author_name: str,
    author_email: str,
    credential_host: str = "github.com",
    token_source: str = "stdin",
    token_dir: str = TOKEN_MOUNT,
    out_dir: str = PUBLISH_MOUNT,
    work_root: str = "/home/worker",
) -> str:
    """Merge `base_ref` into the remote work branch tip and push it, or report conflicts.

    hades #411: merge the remote work branch with an exact-tip lease. The
    work branch is fetched from the remote, not from any bundle, and must still be at
    `expected_head`, the head Crucible pushed or adopted; anything else stops here (exit
    4). The merge resolves nothing: a conflict writes the conflicting paths to
    `conflicts.txt` and exits 6 with the remote untouched. A clean merge is committed as
    `author_name` and pushed with `--force-with-lease` against `expected_head`, so a push
    that raced it is never overwritten. An up-to-date branch pushes nothing and exits 0
    without `push.txt`.

    The token handling is the publisher's (`publisher_script`): stdin to a tmpfs for the
    Docker provider, a Secret volume for Kubernetes, read only through the helper."""
    if token_source not in ("stdin", "file"):
        raise ValueError(f"unknown token source {token_source!r}")
    receive = 'cat > "$TOKDIR/token"\n' if token_source == "stdin" else ""
    secure = 'chmod 0600 "$TOKDIR/token"\n' if token_source == "stdin" else ""
    drop = 'rm -f "$TOKDIR/token"' if token_source == "stdin" else ":"
    return f"""set -eu
{MERGE_MAIN_MARKER}
umask 077
TOKDIR={_quote(token_dir)}
OUT={_quote(out_dir)}
WORK_ROOT={_quote(work_root)}
WORK_BRANCH={_quote(work_branch)}
BASE_REF={_quote(base_ref)}
EXPECTED={_quote(expected_head)}
CLONE_URL={_quote(clone_url)}
drop_token() {{ {drop}; }}
mkdir -p "$OUT"
find "$OUT" -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + 2>/dev/null || true
{receive}if [ ! -s "$TOKDIR/token" ]; then
  echo "no token arrived for this merge" > "$OUT/error.txt"; echo no-token > "$OUT/step.txt"; exit 3
fi
{secure}umask 022
unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE GIT_TRACE_PACKET GIT_TRACE2 || true
export GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
export HOME="$WORK_ROOT" LC_ALL=C
export CRUCIBLE_TOKEN_FILE="$TOKDIR/token"
export CRUCIBLE_CREDENTIAL_HOST={_quote(credential_host)}
export CRUCIBLE_AUTHOR_NAME={_quote(author_name)}
export CRUCIBLE_AUTHOR_EMAIL={_quote(author_email)}
{_CRED_HELPER}
echo credential > "$OUT/step.txt"
if ! printf 'protocol=https\\nhost=%s\\n\\n' "$CRUCIBLE_CREDENTIAL_HOST" \\
    | git credential fill 2>> "$OUT/publisher.log" | grep -q '^password=.'; then
  echo "the credential helper could not read the token" > "$OUT/error.txt"; drop_token; exit 3
fi
cd "$WORK_ROOT"
rm -rf merge-main && mkdir merge-main && cd merge-main
echo init > "$OUT/step.txt"
git init --quiet >> "$OUT/publisher.log" 2>&1
git remote add origin "$CLONE_URL"
echo fetch > "$OUT/step.txt"
if ! git fetch --quiet origin \\
    "+refs/heads/$BASE_REF:refs/remotes/origin/$BASE_REF" \\
    "+refs/heads/$WORK_BRANCH:refs/remotes/origin/$WORK_BRANCH" \\
    >> "$OUT/publisher.log" 2>&1; then
  echo "the base or the work branch could not be fetched" > "$OUT/error.txt"
  drop_token; exit {MERGE_MAIN_HEAD_MOVED}
fi
REMOTE=$(git rev-parse "refs/remotes/origin/$WORK_BRANCH")
printf '%s\\n' "$REMOTE" > "$OUT/remote-head-before.txt"
if [ "$REMOTE" != "$EXPECTED" ]; then
  echo "the remote work branch is at $REMOTE, not the known tip $EXPECTED" > "$OUT/error.txt"
  echo head-moved > "$OUT/step.txt"; drop_token; exit {MERGE_MAIN_HEAD_MOVED}
fi
git checkout --quiet -B crucible-merge-main "$REMOTE" >> "$OUT/publisher.log" 2>&1
if git merge-base --is-ancestor "refs/remotes/origin/$BASE_REF" HEAD; then
  echo "the work branch already contains $BASE_REF" > "$OUT/error.txt"
  echo up-to-date > "$OUT/step.txt"; drop_token
  chmod 0644 "$OUT"/* 2>/dev/null || true
  exit 0
fi
echo merge > "$OUT/step.txt"
if ! git merge --no-ff --no-edit -m "Merge origin/$BASE_REF into $WORK_BRANCH" \\
    "refs/remotes/origin/$BASE_REF" >> "$OUT/publisher.log" 2>&1; then
  git diff --name-only --diff-filter=U > "$OUT/conflicts.txt" 2>> "$OUT/publisher.log" || true
  git merge --abort >> "$OUT/publisher.log" 2>&1 || true
  echo "merging origin/$BASE_REF into the work branch stopped on conflicts" > "$OUT/error.txt"
  echo conflict > "$OUT/step.txt"; drop_token
  chmod 0644 "$OUT"/* 2>/dev/null || true
  exit {MERGE_MAIN_CONFLICT}
fi
git rev-parse HEAD > "$OUT/merge-head.txt"
echo push > "$OUT/step.txt"
if git push --quiet --force-with-lease="refs/heads/$WORK_BRANCH:$EXPECTED" origin \\
    "HEAD:refs/heads/$WORK_BRANCH" 2> "$OUT/push.err"; then
  echo ok > "$OUT/push.txt"
else
  echo failed > "$OUT/push.txt"
  cp "$OUT/push.err" "$OUT/error.txt" 2>/dev/null || true
  drop_token
  chmod 0644 "$OUT"/* 2>/dev/null || true
  exit 5
fi
echo done > "$OUT/step.txt"
drop_token
chmod 0644 "$OUT"/* 2>/dev/null || true
exit 0
"""


def gate_probe_checkout_script(
    url: str,
    base_ref: str,
    checkout_dir: str,
    checkout_token: str | None = None,
    credential_host: str = "github.com",
) -> str:
    """Clone the unchanged base into an isolated handoff volume."""
    credential = (
        _checkout_credential(checkout_token, credential_host) if checkout_token is not None else ""
    )
    drop_token = "drop_checkout_token" if checkout_token is not None else ":"
    return f"""set -eu
{GIT_ENV}
{_safe_git_setup()}
{credential}export GIT_TERMINAL_PROMPT=0
root={_quote(checkout_dir)}
trap '{drop_token}' 0
checkout_failed() {{
  code=$1
  printf 'gate probe checkout failed (exit %s): ' "$code" >&2
  tail -c 1000 /tmp/checkout-stderr >&2
  exit "$code"
}}
rm -rf "$root"
git clone --no-checkout -- {_quote(url)} "$root" \
  > /dev/null 2> /tmp/checkout-stderr || checkout_failed $?
cd "$root" 2> /tmp/checkout-stderr || checkout_failed $?
git checkout --detach {_quote(base_ref)} \
  > /dev/null 2> /tmp/checkout-stderr || checkout_failed $?
{drop_token}
"""


def gate_probe_script(
    checkout_dir: str,
    checks: list[dict[str, Any]],
    timeout: int,
) -> str:
    """Run checks in the isolated checkout and keep forged results out of its log."""
    # Never depend on an interpreter that policy required_programs does not
    # guarantee: every worker image has sh, jq and Debian coreutils (timeout).
    program = f"""set -eu
root=$(mktemp -d)
trap 'rm -rf "$root"' 0
cd {_quote(checkout_dir)}
probe_check() {{
  check_id=$1
  command=$2
  code=0
  timeout --signal=KILL {_quote(str(timeout))} sh -c '
    code=0
    sh -c "$1" || code=$?
    printf "%s\n" "$code" > "$2"
  ' sh "$command" "$root/status" > "$root/output" 2>&1 || code=$?
  if [ "$code" -ne 0 ]; then
    printf '%s: command timed out or runner failed (exit %s)\n' "$check_id" "$code" >&2
    exit "$code"
  fi
  read -r code < "$root/status"
  : > "$root/detail"
  if [ "$code" -eq 127 ]; then
    tail -c 1000 "$root/output" > "$root/detail"
  fi
  jq -cn --arg id "$check_id" --arg command "$command" --argjson exit "$code" \
    --rawfile detail "$root/detail" '{{id: $id, command: $command, exit: $exit, detail: $detail}}'
}}
"""
    for check in checks:
        program += f"probe_check {_quote(check['id'])} {_quote(check['command'])}\n"
    return program
