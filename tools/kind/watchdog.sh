#!/usr/bin/env bash
# hades #508: watchdog that fires before the CI 20-minute GitHub Actions timeout.
#
# Usage:
#   tools/kind/watchdog.sh <timeout_seconds> <marker_file> <pytest_pid>
#
# This function forks a background process that waits `timeout_seconds`.
# If the pytest PID is still alive when the timer fires it:
#   1. Writes a marker file so the caller knows the watchdog fired.
#   2. Sends SIGABRT to the pytest process group, which triggers Python's
#      faulthandler to dump every thread's stack to stderr.
#   3. The caller's EXIT trap (which runs dump.sh on failure) then captures
#      whatever is left of the cluster state.
#
# The caller should:
#   - Pass the marker file path as $2.
#   - Pass the pytest process PID as $3 (not the shell PID).
#   - In its EXIT trap, check if the marker exists and call dump.sh.
#   - Kill the watchdog child PID (printed to stdout) on normal exit.
#
# Returns 0 immediately; the background process runs independently.

timeout_seconds=$1
marker_file=$2
pytest_pid=$3

# Fork a background process that does the actual sleeping and killing.
# Close stdout/stderr so the parent script can exit immediately even when
# the background process keeps them open (bash waits for children with
# inherited fds).
{
    sleep "$timeout_seconds"

    if kill -0 "$pytest_pid" 2>/dev/null; then
        echo "watchdog: shard timed out after ${timeout_seconds}s killing PID $pytest_pid" >&2
        if [ -n "$marker_file" ]; then
            echo "watchdog fired" > "$marker_file"
        fi
        # Send SIGABRT to the pytest process group so Python's faulthandler
        # dumps all thread stacks (not just the shell that started pytest).
        kill -SIGABRT "-${pytest_pid}" 2>/dev/null || kill -SIGABRT "$pytest_pid" 2>/dev/null || true
    fi
} </dev/null >/dev/null 2>&1 &

# Print the background PID so the caller can clean it up.
echo $!
