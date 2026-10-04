#!/bin/sh
# The e2e "harness" (18): a script implementing the adapter's launch contract.
#
# It reads the identity bundle, does the work the throwaway repository asks for,
# writes a CompletionClaimV1 to the report directory, and exits with the code the
# case wants. No model, no credential, no subscription: it proves the real provider
# path, not a harness.
#
# The case comes from the file `e2e-behavior` committed in the repository, so the
# test chooses the behaviour by building the repository, never by a flag Crucible
# would have to carry.
set -u

# C7a exercises the interactive-login container path with the same real image and
# daemon as the e2e worker tier. This mode emits harmless device-flow markers and
# writes a non-secret result only into the selected credential mount.
if [ "${1:-}" = "login-stub" ]; then
  printf 'Open https://example.invalid/device\n'
  printf 'Device code C7AA-TEST\n'
  printf '{"authenticated":true}\n' > "${CRUCIBLE_LOGIN_DIR:?}/session.json"
  sleep 1
  exit 0
fi

ID=${CRUCIBLE_IDENTITY_DIR:-/crucible/identity}
REPORT=${CRUCIBLE_REPORT_DIR:-/crucible/report}
REPO=${CRUCIBLE_REPO_DIR:-/crucible/repo}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# 06: the bundle is the task. No bundle, no run.
if [ ! -r "$ID/IDENTITY.md" ] || [ ! -r "$ID/contract.json" ]; then
  log "no identity bundle at $ID"
  exit 70
fi
log "read identity bundle $(wc -c < "$ID/IDENTITY.md") bytes"

CONTRACT="$ID/contract.json"
behavior=$(cat "$REPO/e2e-behavior" 2>/dev/null || echo succeed)
log "behavior=$behavior"

# FDY-0140: Hermes under `-z` writes nothing while it works, but its session store in
# the home directory changes after every turn. This works the same way, silently, for
# `e2e-silent-seconds` (committed in the repository, default 30), then carries on.
if [ "$behavior" = "silent-work" ]; then
  silent=$(cat "$REPO/e2e-silent-seconds" 2>/dev/null || echo 30)
  mkdir -p "$HOME/.e2e-session"
  end=$(( $(date +%s) + silent ))
  turn=0
  while [ "$(date +%s)" -lt "$end" ]; do
    turn=$((turn + 1))
    printf 'turn %s\n' "$turn" > "$HOME/.e2e-session/state"
    sleep 2
  done
fi

case "$behavior" in
  hang)      log "sleeping until Crucible drains me"; while : ; do sleep 1; done ;;
  environment) log "bad environment"; exit 70 ;;
  crash)     log "crashing"; exit 3 ;;
  noreport)  log "exiting 0 with no report"; exit 0 ;;
  blocked)
    printf '# Blocked\n\nThe e2e script harness needs a decision.\n' > "$REPORT/blocked.md"
    exit 75 ;;
esac

# `curl` exits 0 on any HTTP response, including the egress proxy's own 403, so every
# probe uses --fail. Control-plane probes also bypass the proxy with --noproxy: the
# question is whether the worker can reach the socket, the database or the API itself,
# not whether the proxy will answer for them.
probe() {
  name=$1; shift
  if "$@" >/dev/null 2>&1; then result=reached; else result=refused; fi
  printf '%s\t%s\n' "$name" "$result" >> "$REPORT/isolation.tsv"
  log "probe $name $result"
}
direct() { curl -sS -f --noproxy '*' --max-time 5 "$@"; }

if [ "$behavior" = "isolation" ]; then
  : > "$REPORT/isolation.tsv"
  probe docker-socket-unix direct --unix-socket /var/run/docker.sock http://localhost/version
  probe docker-socket-run direct --unix-socket /run/docker.sock http://localhost/version
  probe socket-proxy direct http://docker-socket-proxy:2375/version
  probe socket-proxy-ip direct http://172.17.0.1:2375/version
  probe database sh -c 'exec 3<>/dev/tcp/postgres/5432'
  probe database-gateway sh -c 'exec 3<>/dev/tcp/172.17.0.1/5432'
  probe crucible-api direct http://crucible:8080/v1/ready
  probe other-credential-codex test -r /home/worker/.codex/auth.json
  probe other-credential-claude test -r /home/worker/.claude/.credentials.json
  probe other-credential-agy test -r /home/worker/.gemini/oauth_creds.json
  probe other-credential-root test -d /var/lib/crucible/credentials
  probe git-push git -C "$REPO" -c credential.helper= push origin HEAD
  probe egress-not-allowlisted curl -sS -f --max-time 10 https://example.com/
  probe egress-direct-by-ip curl -sS -f --noproxy '*' --max-time 10 https://140.82.112.3/
  probe egress-direct-by-name curl -sS -f --noproxy '*' --max-time 10 https://github.com/
  probe write-root touch /crucible-root-probe
  probe write-identity touch "$ID/probe"
  probe write-usr touch /usr/local/bin/probe
  probe chown-report chown 0:0 "$REPORT"
  probe mknod mknod /tmp/probe-dev b 8 0
  log "isolation probes done"
fi

# --- do the work ---------------------------------------------------------
target=$(jq -r '.scope.allowed_paths[0] // "src/**"' "$CONTRACT" \
  | sed 's|/\*\*$|/e2e_change.txt|; s|\*\*|e2e_change.txt|; s|\*|e2e|')
mkdir -p "$(dirname "$REPO/$target")"
printf 'Written by the e2e script harness for %s.\n' \
  "$(jq -r '.external_id // .task_external_id // "unknown"' "$CONTRACT")" > "$REPO/$target"

if [ "$behavior" = "out-of-scope" ]; then
  printf 'outside the contract\n' > "$REPO/outside-the-contract.txt"
fi
if [ "$behavior" = "injected" ]; then
  mkdir -p "$REPO/.crucible" && printf 'x\n' > "$REPO/.crucible/identity.md"
  git -C "$REPO" add -f .crucible/identity.md
fi
if [ "$behavior" = "break-verification" ]; then
  printf 'exit 1\n' >> "$REPO/checks/test.sh"
fi

# FDY-0140: `no-commit` leaves its edit in the working tree, as a model that forgot to
# commit does; the collector commits it.
if [ "$behavior" != "no-commit" ]; then
  git -C "$REPO" add -A
  trailer=$(jq -r '.commit_trailer // "Crucible-Attempt"' "$CONTRACT" 2>/dev/null || echo Crucible-Attempt)
  git -C "$REPO" commit -q \
    -m "e2e: change for ${CRUCIBLE_TASK_EXTERNAL_ID:-unknown}" \
    -m "$trailer: ${CRUCIBLE_ATTEMPT_ID:-unknown}" || log "nothing to commit"
fi
head=$(git -C "$REPO" rev-parse HEAD)
commits=$(git -C "$REPO" rev-list --count "$(jq -r '.repository.base_ref // "main"' "$CONTRACT")"..HEAD 2>/dev/null || echo 1)
log "committed $head"

# --- run every required verification, capturing each log -----------------
checks="[]"
jq -r '.required_verification[] | select((.kind // "command") == "command") | .id + "\t" + .command' \
  "$CONTRACT" > /tmp/checks.tsv
while IFS="$(printf '\t')" read -r cid ccmd; do
  [ -n "$cid" ] || continue
  ( cd "$REPO" && sh -c "$ccmd" ) > "$REPORT/$cid.log" 2>&1
  code=$?
  log "check $cid exited $code"
  checks=$(printf '%s' "$checks" | jq --arg id "$cid" --arg cmd "$ccmd" --argjson exit "$code" \
    --arg logf "$cid.log" '. + [{id: $id, command: $cmd, exit: $exit, log: $logf}]')
done < /tmp/checks.tsv

# --- run evidence the contract asks for ----------------------------------
for path in $(jq -r '.required_verification[] | select(.kind == "artifact") | .path' "$CONTRACT"); do
  rel=${path#report/}
  mkdir -p "$(dirname "$REPORT/$rel")"
  printf '# Run evidence\n\nThe e2e script harness ran %s at %s.\n' "$behavior" "$head" \
    > "$REPORT/$rel"
done

# --- the claim (11). JSON is valid YAML, so report.yaml carries it -------
jq -n \
  --arg external "$(jq -r '.external_id // .task_external_id' "$CONTRACT")" \
  --arg branch "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" \
  --arg head "$head" \
  --argjson commits "${commits:-1}" \
  --arg target "$target" \
  --argjson checks "$checks" \
  --slurpfile contract "$CONTRACT" \
  '{
     schema_version: "1.0",
     task_external_id: $external,
     summary: "The e2e script harness made the requested change and ran every check.",
     self_review: {
       documentation: ["No documentation update needed for this synthetic e2e change."],
       acceptance_criteria: [
         $contract[0].acceptance_criteria[]? | { id: .id, status: "met", evidence: "run-evidence.md" }
       ],
       omissions: []
     },
     changed_files: [$target],
     refs: { branch: $branch, head_sha: $head, commits: $commits },
     checks: $checks,
     acceptance_mapping: [
       $contract[0].acceptance_criteria[]? | { id: .id, status: "met", evidence: "run-evidence.md" }
     ],
     run_evidence: [
       $contract[0].required_verification[]? | select(.kind == "artifact") | (.path | sub("^report/"; ""))
     ],
     proposed_pull_request: {
       title: ($contract[0].title // "e2e change"),
       body: "Produced by the e2e script harness.",
       closes: []
     },
     limitations: [],
     risks: [],
     blockers: [],
     follow_ups: []
   }' > "$REPORT/report.yaml"

log "wrote report.yaml"
exit 0
