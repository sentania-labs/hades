#!/usr/bin/env bash
# Exercises e2e-kind.sh's cleanup() against a stubbed docker, proving that a daemon
# which cannot confirm removal fails the run (correction to 77) while a busybox pull
# failure alone still does not (77's own intent, kept). Wired into `make test-shell`
# (issue 163): a bare `make test` (and CI's test job) now covers it.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
failures=0
cleanup_dirs=()
cleanup_test_dirs() {
  [ "${#cleanup_dirs[@]}" -eq 0 ] || rm -rf "${cleanup_dirs[@]}"
}
trap cleanup_test_dirs EXIT HUP INT TERM

run_case() {
  local name=$1 stub_dir=$2 expect_status=$3 expect_grep=$4 test_scratch status output

  test_scratch=$(mktemp -d -t crucible-kind-cleanup-test.XXXXXX)
  cleanup_dirs+=("$test_scratch")
  set +e
  output=$( {
    PATH="$stub_dir:$PATH"
    export PATH
    export CRUCIBLE_KIND_TEST_HOOK=1
    # shellcheck source=tools/kind/e2e-kind.sh
    source "$root/tools/kind/e2e-kind.sh"
    trap - EXIT HUP INT TERM
    rm -rf "$scratch"
    cluster_created=1
    registry_started=1
    tag_created=1
    cluster="fake-cluster"
    registry="fake-registry"
    registry_ref="fake-ref"
    scratch="$test_scratch"
    kubeconfig="$scratch/kubeconfig"
    cleanup
  } 2>&1 )
  status=$?
  set -e

  if [ "$status" -ne "$expect_status" ]; then
    echo "FAIL: $name: expected exit $expect_status, got $status" >&2
    echo "$output" >&2
    failures=$((failures + 1))
    return
  fi
  if [ -n "$expect_grep" ] && ! printf '%s\n' "$output" | grep -Fq "$expect_grep"; then
    echo "FAIL: $name: expected output to contain '$expect_grep'" >&2
    echo "$output" >&2
    failures=$((failures + 1))
    return
  fi
  echo "PASS: $name (exit $status)"
}

daemon_down=$(mktemp -d -t crucible-kind-cleanup-stub.XXXXXX)
cleanup_dirs+=("$daemon_down")
cat > "$daemon_down/docker" <<'EOF'
#!/bin/sh
echo "docker: cannot connect to the Docker daemon" >&2
exit 1
EOF
cat > "$daemon_down/kind" <<'EOF'
#!/bin/sh
exit 1
EOF
# crucible_kind_pull's retry backoff (sleep 2/4/8) has nothing to do with what this
# test proves; skip the wait so the stubbed pull failure resolves instantly.
cat > "$daemon_down/sleep" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$daemon_down/docker" "$daemon_down/kind" "$daemon_down/sleep"

daemon_up_busybox_pull_fails=$(mktemp -d -t crucible-kind-cleanup-stub.XXXXXX)
cleanup_dirs+=("$daemon_up_busybox_pull_fails")
cat > "$daemon_up_busybox_pull_fails/docker" <<'EOF'
#!/bin/sh
case "$1" in
  info) exit 0 ;;
  inspect) exit 1 ;;
  rm) exit 0 ;;
  image)
    case "$2" in
      inspect) exit 1 ;;
      rm) exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  network)
    case "$2" in
      inspect) exit 1 ;;
      rm) exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  run) exit 1 ;;
  pull) exit 1 ;;
  *) exit 1 ;;
esac
EOF
cat > "$daemon_up_busybox_pull_fails/kind" <<'EOF'
#!/bin/sh
case "$1" in
  delete) exit 0 ;;
  get) exit 0 ;;
  *) exit 1 ;;
esac
EOF
cat > "$daemon_up_busybox_pull_fails/sleep" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$daemon_up_busybox_pull_fails/docker" "$daemon_up_busybox_pull_fails/kind" \
  "$daemon_up_busybox_pull_fails/sleep"

run_case "daemon unreachable during cleanup fails the run: cluster" "$daemon_down" 1 \
  "cannot confirm cluster fake-cluster removed"
run_case "daemon unreachable during cleanup fails the run: registry" "$daemon_down" 1 \
  "cannot confirm registry fake-registry removed"
run_case "daemon unreachable during cleanup fails the run: image tag" "$daemon_down" 1 \
  "cannot confirm image tag fake-ref removed"
run_case "daemon reachable, busybox pull alone fails, run stays green" \
  "$daemon_up_busybox_pull_fails" 0 ""

if [ "$failures" -ne 0 ]; then
  echo "$failures case(s) failed" >&2
  exit 1
fi
echo "all cleanup cases passed"
