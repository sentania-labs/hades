#!/usr/bin/python3
"""Run Hermes, capture plain output, and enrich its usage record from per-run state.

FDY-0140:
- The task's IDENTITY.md is put in the prompt itself, ahead of the pointer, so the model
  starts from the instructions rather than having to decide to read them.
- Hermes 0.19's `-z` builds its agent with a fixed 90-turn budget and reads no turn
  setting. The limit Crucible passes is applied by running Hermes's own entry point
  under a small bootstrap that sets that budget when the agent is built. The context
  window is Hermes's own `model.context_length` setting, written to its home.
- Nothing is added to PATH. Hermes is started by the virtual environment's own Python,
  by path, so every command the model runs sees the image's toolchain, not the venv's.
  (Hermes puts the directory its `hermes` command is found in first on each subshell's
  PATH; the image links `/usr/local/bin/hermes`, already on PATH, so that is a no-op.)
- While Hermes works, a line goes to stderr each time its session store changes: `-z`
  writes nothing else until it ends, and a quiet worker is otherwise indistinguishable
  from a stuck one.

Hades #386: Hermes 0.19's verification guard files an edit to a path with no project of
its own (the report, /crucible/report/report.yaml) under the session's workspace root, so
writing the report turned the checkout's passed verification stale and the guard asked
for it all again before finishing. The bootstrap wraps
`agent.verification_evidence.mark_workspace_edited` so that a path outside the root it
resolves never marks that root edited; any path inside the root still does. The patch is
written against 0.19.0 only and Hermes refuses to start under any other version.

Hades #385: Hermes's content search falls back to `grep -r --exclude-dir='.*' ... ROOT`
when there is no ripgrep, and GNU grep applies that pattern to ROOT itself, so a search
of `.` (or of any root whose last component starts with a dot) finds nothing. The image
now carries ripgrep, and the bootstrap also runs that grep from inside the root with no
file operand, which grep never excludes, so only hidden directories below the root are
skipped. The `cd` runs in a subshell: Hermes records the shell's directory after every
command as the session's, and a search must not move the agent. Both patches are for
Hermes 0.19.0 alone: the bootstrap refuses to start any other version rather than patch
code it was not written against, and before Hermes starts, main() imports the patched
module once on its own and stops the attempt if the fallback is not the 0.19.0 one.
That check cannot be left to the import inside Hermes: Hermes's tool discovery catches
every exception and only logs it, and would start without its file tools.

Hades #388: the gateway reserves a response allowance (32000 tokens in the lab) out of
the window on every request, whether or not the request names one. Hermes was told the
window but not the allowance, so its requests carried no `max_tokens` and its compressor
budgeted against the whole window: with a 131072 window it compressed at 98304 input
tokens, while the gateway refuses input above 99072. The allowance Crucible passes is
written as Hermes's own `model.max_tokens`, which 0.19.0 sends on every request and
subtracts from the window before taking its trigger (74304 for the same window). The
routing entry's thinking setting goes on each request as `chat_template_kwargs`, through
the agent's request overrides, which carry nothing else here: `max_tokens` there would
replace the lower allowance Hermes retries with after the gateway says the input leaves
less room. Hermes's own retry boosts can ask for more than the allowance (its cap is
32768); the bootstrap caps those at the allowance and leaves lower values as they are.
Before Hermes starts, the preflight checks that 0.19.0 still reads `model.max_tokens`,
still boosts the way the cap is written for, and still computes the trigger the way
`compression_trigger` below does, and stops the attempt otherwise.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

HERMES_PYTHON = "/opt/hermes/bin/python"
# The Hermes the image pins (images/worker/Dockerfile). The usage patches in PATCHES
# and the session columns below are written against it; another version fails loudly.
HERMES_VERSION = "0.19.0"

# How often the session store is looked at for progress.
PROGRESS_SECONDS = 15.0
PROGRESS_LINE = "crucible-hermes: working, session updated"

# Run inside the Hermes virtual environment, ahead of Hermes itself. It changes the turn
# budget an agent is built with when the caller named none, and the root of the grep
# fallback of content search (hades #385). Each applies only once its module is imported
# the ordinary way, so Hermes's own import order (its approval mode is read at import)
# is untouched. #387: it also gives Hermes's usage file the failure cause an early
# return from the agent reports only in its result's `error`.
PATCHES = r"""
import importlib.abc
import importlib.metadata
import importlib.util
import inspect
import os
import sys
from pathlib import Path

GUARD = "agent.verification_evidence"

def _refuse(reason):
    print(f"crucible-hermes: {reason}", file=sys.stderr, flush=True)
    # Hermes swallows exceptions around its verification imports, so exit outright.
    os._exit(70)


EXPECTED = "@HERMES_VERSION@"
try:
    FOUND = importlib.metadata.version("hermes-agent")
except importlib.metadata.PackageNotFoundError:
    FOUND = "none"
if FOUND != EXPECTED:
    raise SystemExit(
        f"crucible-hermes: its patches are for hermes-agent {EXPECTED}, found {FOUND}; "
        "refusing to start Hermes unpatched (hades #385)"
    )

LIMIT = int(os.environ.get("CRUCIBLE_HERMES_MAX_TURNS") or 0)
# `max_iterations` is the tenth parameter of AIAgent.__init__ after self (0.19).
POSITION = 9
# Hades #388: the gateway's response allowance (0: none given) and the routing entry's
# thinking setting (None: none given).
ALLOWANCE = int(os.environ.get("CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS") or 0)
THINKING = {"true": True, "false": False}.get(os.environ.get("CRUCIBLE_HERMES_THINKING", ""))


def _capped(value):
    # Hades #388: a response cap Hermes sets for its next request, never above the
    # allowance the gateway enforces. A lower one (Hermes retrying after the gateway
    # said the input leaves less room) is kept exactly as Hermes set it.
    if ALLOWANCE > 0 and isinstance(value, int) and not isinstance(value, bool):
        return min(value, ALLOWANCE)
    return value


def _cap_retries(agent_class):
    # 0.19.0 keeps the cap for one request in `_ephemeral_max_output_tokens`, set by its
    # output-cap retry (lower) and its truncation and length retries (boosted up to
    # 32768), and read and cleared when the request is built.
    def get(self):
        return self.__dict__.get("_crucible_ephemeral_out")

    def put(self, value):
        self.__dict__["_crucible_ephemeral_out"] = _capped(value)

    agent_class._ephemeral_max_output_tokens = property(get, put)


def _thinking(overrides):
    # Hades #388: the routing entry's thinking setting on every request, unless the
    # caller already named one. Only `extra_body` is touched.
    merged = dict(overrides or {})
    extra = dict(merged.get("extra_body") or {})
    extra.setdefault("chat_template_kwargs", {"enable_thinking": THINKING})
    merged["extra_body"] = extra
    return merged


class _TurnBudget(importlib.abc.MetaPathFinder):
    # The agent as -z builds it: the turn budget (FDY-0140), and the thinking setting and
    # the retry cap of hades #388, each only when Crucible gave a value.
    def find_spec(self, name, path, target=None):
        if name != "run_agent":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module):
            execute(module)
            original = module.AIAgent.__init__

            def __init__(self, *args, **kwargs):
                if LIMIT > 0 and len(args) <= POSITION and "max_iterations" not in kwargs:
                    kwargs["max_iterations"] = LIMIT
                if THINKING is not None:
                    kwargs["request_overrides"] = _thinking(kwargs.get("request_overrides"))
                original(self, *args, **kwargs)

            module.AIAgent.__init__ = __init__
            if ALLOWANCE > 0:
                _cap_retries(module.AIAgent)

        loader.exec_module = exec_module
        return spec


def _inside(path, root, cwd):
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd or ".") / candidate
        candidate = candidate.resolve()
        return candidate == root or root in candidate.parents
    except (OSError, RuntimeError, ValueError):
        # Unknown: count it, as Hermes would.
        return True


class _ReportWriteGuard(importlib.abc.MetaPathFinder):
    # #386: an edit outside a verification root never invalidates that root's checks.
    def find_spec(self, name, path, target=None):
        if name != GUARD:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            _refuse(f"{GUARD} is missing from hermes-agent {EXPECTED}")
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module):
            execute(module)
            original = getattr(module, "mark_workspace_edited", None)
            expected = ["session_id", "cwd", "paths"]
            if original is None or list(inspect.signature(original).parameters) != expected:
                _refuse(f"{GUARD}.mark_workspace_edited is not the 0.19.0 shape")

            def mark_workspace_edited(*, session_id, cwd, paths=None):
                if paths:
                    from agent.coding_context import project_facts_for

                    facts = project_facts_for(cwd)
                    if facts:
                        # The root Hermes's own function resolves.
                        root = Path(str(facts.get("root") or Path(cwd or ".").resolve()))
                        root = root.expanduser().resolve()
                        paths = [p for p in paths if p and _inside(str(p), root, cwd)]
                        if not paths:
                            return None
                return original(session_id=session_id, cwd=cwd, paths=paths)

            mark_workspace_edited.__wrapped__ = original
            module.mark_workspace_edited = mark_workspace_edited

        loader.exec_module = exec_module
        return spec


class _FailureCause(importlib.abc.MetaPathFinder):
    # #387: when the agent returns failed rather than raising (a provider error it gave
    # up on, for example), hermes_cli.oneshot writes the usage file with no `failure`
    # and the cause is lost with the result's `error`. Pass that error on as the
    # failure. Written against 0.19.0's _write_usage_file(path, result, failure=None).
    def find_spec(self, name, path, target=None):
        if name != "hermes_cli.oneshot":
            return None
        sys.meta_path.remove(self)
        installed = importlib.metadata.version("hermes-agent")
        if installed != EXPECTED:
            raise RuntimeError(
                f"crucible-hermes: the usage-file patch is for hermes-agent "
                f"{EXPECTED}, and {installed} is installed"
            )

        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module):
            execute(module)
            original = module._write_usage_file

            def _write_usage_file(path, result, failure=None):
                error = result.get("error") if isinstance(result, dict) else None
                if failure is None and result.get("failed") and isinstance(error, str) and error:
                    failure = error
                original(path, result, failure)

            module._write_usage_file = _write_usage_file

        loader.exec_module = exec_module
        return spec

# Hades #385. The 0.19.0 fallback, exactly: these lines are what the patch replaces the
# effect of, so a Hermes whose fallback reads differently fails here, at import. Inside
# Hermes that failure would only be logged, so PREFLIGHT (below) imports the module on
# its own first, where it stops the attempt.
GREP_SHAPE = (
    "cmd_parts = [\"grep\", \"-rnH\"]",
    "cmd_parts.append(\"--exclude-dir='.*'\")",
    "cmd_parts.append(self._escape_shell_arg(path))",
    "cmd_parts.extend([\"|\", \"head\", \"-n\", str(fetch_limit)])",
    "cmd = \"set -o pipefail; \" + \" \".join(cmd_parts)",
)
GREP_HEAD = "set -o pipefail; grep -rnH "


class _RootedShell:
    # The file operations object as the fallback sees it, except that its grep command
    # runs from inside the root with no file operand. GNU grep applies --exclude-dir to
    # every operand it is given, `.` and `./` included, but never to the `.` it searches
    # when it is given none; that `.` is also left out of the names it prints. The cd is
    # in a subshell: Hermes takes the shell's `pwd -P` after each command as the
    # session's working directory, so a top-level cd would move the agent for good.
    def __init__(self, ops, root):
        self._ops = ops
        self._root = ops._escape_shell_arg(root)

    def __getattr__(self, name):
        return getattr(self._ops, name)

    def _exec(self, command, *args, **kwargs):
        operand = f" {self._root} | head -n "
        if command.startswith(GREP_HEAD) and operand in command:
            before, _, after = command.rpartition(operand)
            command = (
                f"set -o pipefail; (CDPATH= cd -- {self._root} >/dev/null || exit 2; "
                "exec grep -rnH "
                f"{before[len(GREP_HEAD):]}) | head -n {after}"
            )
        return self._ops._exec(command, *args, **kwargs)


def _rooted(root, name):
    return (root if root.endswith("/") else root + "/") + name


def _patch_grep(module):
    shell = module.ShellFileOperations
    original = shell._search_with_grep
    source = inspect.getsource(original)
    missing = [line for line in GREP_SHAPE if line not in source]
    if missing:
        raise RuntimeError(
            "crucible-hermes: Hermes's grep fallback is not the one hades #385 patches; "
            f"missing {missing}"
        )

    def _search_with_grep(self, pattern, path, file_glob, limit, offset, output_mode, context):
        probe = self._exec(f"test -d {self._escape_shell_arg(path)} && echo directory")
        if probe.stdout.strip() != "directory":
            return original(self, pattern, path, file_glob, limit, offset, output_mode, context)
        result = original(
            _RootedShell(self, path), pattern, path, file_glob, limit, offset, output_mode,
            context,
        )
        # Grep printed names relative to the root; give them back the root, as grep
        # does for an operand, so they read as they would have with ripgrep.
        for match in result.matches:
            match.path = _rooted(path, match.path)
        result.files = [_rooted(path, name) for name in result.files]
        result.counts = {_rooted(path, name): count for name, count in result.counts.items()}
        return result

    shell._search_with_grep = _search_with_grep


class _GrepRoot(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "tools.file_operations":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module):
            execute(module)
            _patch_grep(module)

        loader.exec_module = exec_module
        return spec

sys.meta_path.insert(0, _ReportWriteGuard())
sys.meta_path.insert(0, _FailureCause())
if LIMIT > 0 or ALLOWANCE > 0 or THINKING is not None:
    sys.meta_path.insert(0, _TurnBudget())
sys.meta_path.insert(0, _GrepRoot())
""".replace("@HERMES_VERSION@", HERMES_VERSION)

# Hades #388: what the allowance relies on in 0.19.0, read from the source files without
# importing them. `agent_init` makes `model.max_tokens` the agent's response cap and hands
# it to the compressor; `conversation_loop` boosts its retries with these lines, which
# the cap in PATCHES is written for.
BUDGET_SHAPE = {
    "agent.agent_init": (
        '_config_max_tokens = _model_cfg.get("max_tokens")',
        "agent.max_tokens = _parsed_max_tokens",
        "max_tokens=agent.max_tokens,",
    ),
    "agent.conversation_loop": (
        "agent._ephemeral_max_output_tokens = min(_tc_boost, _tc_boost_cap)",
        "agent._ephemeral_max_output_tokens = safe_out",
        "agent._ephemeral_max_output_tokens = min(_boost, _boost_cap)",
    ),
}

# Run by the preflight when an allowance is given: the source shape above, then the
# compression trigger Hermes computes for the window and allowance, which must be the
# one `compression_trigger` gives (passed in CRUCIBLE_HERMES_EXPECTED_TRIGGER).
BUDGET_CHECK = (
    "\nBUDGET_SHAPE = "
    + repr(BUDGET_SHAPE)
    + r"""
if ALLOWANCE > 0:
    for _module, _lines in BUDGET_SHAPE.items():
        _found = importlib.util.find_spec(_module)
        _source = open(_found.origin, encoding="utf-8").read() if _found else ""
        _missing = [line for line in _lines if line not in _source]
        if _missing:
            raise SystemExit(
                f"crucible-hermes: {_module} is not the one hades #388 relies on; "
                f"missing {_missing}"
            )
    _window = int(os.environ.get("CRUCIBLE_HERMES_CONTEXT_LENGTH") or 0)
    _expected = int(os.environ.get("CRUCIBLE_HERMES_EXPECTED_TRIGGER") or 0)
    if _window > 0:
        from agent.context_compressor import ContextCompressor as _Compressor

        _percent = _Compressor._effective_threshold_percent(_window, THRESHOLD_PERCENT)
        _trigger = _Compressor._compute_threshold_tokens(_window, _percent, ALLOWANCE)
        if _trigger != _expected:
            raise SystemExit(
                f"crucible-hermes: Hermes compresses at {_trigger} input tokens for a "
                f"{_window} window less {ALLOWANCE}, not {_expected} (hades #388)"
            )
"""
)

# Run by main() before Hermes starts: the version check above, then the patched module
# imported on its own, so a fallback of another shape exits non-zero here instead of
# being swallowed by Hermes's tool discovery (hades #385).
PREFLIGHT = (
    PATCHES
    + r"""
try:
    import tools.file_operations
    import agent.verification_evidence
except RuntimeError as error:
    raise SystemExit(str(error))
"""
    + "\nTHRESHOLD_PERCENT = @THRESHOLD@\n"
    + BUDGET_CHECK
)
PREFLIGHT_FAILED = (
    "crucible-hermes: the Hermes in this image is not the one its patches were written "
    "for (hades #385, #386, #388); not starting it"
)

BOOTSTRAP = (
    PATCHES
    + r"""
sys.argv = ["hermes", *sys.argv[1:]]
from hermes_cli.main import main

sys.exit(main())
""".replace("@HERMES_VERSION@", HERMES_VERSION)
)


# Hades #388: Hermes 0.19.0's compression trigger (agent/context_compressor.py): its
# default 50% trigger, raised to 75% for a window under 512000 tokens, taken of the window
# less the response allowance, never below 64000; where that floor would reach the
# budget, 85% of the budget instead.
THRESHOLD_PERCENT = 0.50
SMALL_WINDOW = 512_000
SMALL_WINDOW_PERCENT = 0.75
MINIMUM_CONTEXT = 64_000
MINIMUM_TRIGGER_RATIO = 0.85
PREFLIGHT = PREFLIGHT.replace("@THRESHOLD@", repr(THRESHOLD_PERCENT))


def compression_trigger(context_length: int, max_output_tokens: int = 0) -> int:
    """The input tokens at which Hermes 0.19.0 compresses, for this window and response
    allowance (0: none), computed the way its compressor does."""
    percent = THRESHOLD_PERCENT
    if context_length and context_length < SMALL_WINDOW:
        percent = max(percent, SMALL_WINDOW_PERCENT)
    budget = context_length - max(0, max_output_tokens)
    if budget <= 0:
        budget = context_length
    floored = max(int(budget * percent), MINIMUM_CONTEXT)
    if budget > 0 and floored >= budget:
        return max(1, min(int(budget * MINIMUM_TRIGGER_RATIO), budget - 1))
    return floored


def _milliseconds(started: object, ended: object) -> int | None:
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
        return max(0, int((ended - started) * 1000))
    if not isinstance(started, str) or not isinstance(ended, str):
        return None
    try:
        left = datetime.fromisoformat(started.replace("Z", "+00:00"))
        right = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((right - left).total_seconds() * 1000))


def _limit(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name) or 0))
    except ValueError:
        return 0


class SessionSchemaChanged(RuntimeError):
    """Hermes's session table lacks a column the usage enrichment reads (#387)."""


# usage field -> sessions column (hermes_state.py SCHEMA_VERSION 22 in HERMES_VERSION).
SESSION_FIELDS = (
    ("model", "model"),
    ("provider", "billing_provider"),
    ("estimated_cost_usd", "estimated_cost_usd"),
)
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
# Hermes's own session_total_tokens: prompt (input, cache read and cache write) plus
# output. The row has no total column.
TOTAL_PARTS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
SESSION_COLUMNS = frozenset(
    {"id", "parent_session_id", "started_at", "ended_at", "tool_call_count"}
    | {column for _, column in SESSION_FIELDS}
    | set(TOKEN_FIELDS)
)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _check_columns(database: sqlite3.Connection) -> None:
    tables = {
        row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "sessions" not in tables:
        raise SessionSchemaChanged("crucible-hermes: Hermes's state.db has no sessions table")
    present = {row[1] for row in database.execute("PRAGMA table_info(sessions)")}
    missing = sorted(SESSION_COLUMNS - present)
    if missing:
        raise SessionSchemaChanged(
            f"crucible-hermes: Hermes's sessions table has no {', '.join(missing)} "
            f"column; the usage enrichment is written against hermes-agent {HERMES_VERSION}"
        )


def _run_sessions(database: sqlite3.Connection) -> dict[str, object] | None:
    """#387: the whole run's session state when Hermes wrote no session id.

    The home is fresh for every launch, so every row in it is this run's. After context
    compression Hermes ends the row and opens a child (parent_session_id), and every
    later call's tokens go to the child, so the run's totals are the sum of all rows.
    The session id is the run's top-level row; model and provider are the newest row's.
    """
    root = database.execute(
        "SELECT id FROM sessions WHERE parent_session_id IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if root is None:
        return None
    newest = database.execute(
        "SELECT model, billing_provider FROM sessions ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    totals = database.execute(
        "SELECT MIN(started_at), MAX(ended_at), SUM(tool_call_count), "
        "SUM(estimated_cost_usd), "
        + ", ".join(f"SUM({column})" for column in TOKEN_FIELDS)
        + " FROM sessions"
    ).fetchone()
    started, ended, calls, cost, *tokens = totals
    return {
        "id": root[0],
        "model": newest[0],
        "billing_provider": newest[1],
        "started_at": started,
        "ended_at": ended,
        "tool_call_count": calls,
        "estimated_cost_usd": cost,
        **dict(zip(TOKEN_FIELDS, tokens, strict=True)),
    }


def _one_session(database: sqlite3.Connection, session_id: str) -> dict[str, object] | None:
    database.row_factory = sqlite3.Row
    row = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row is not None else None


def _fill_from_session(usage: dict[str, object], session: dict[str, object]) -> None:
    """#387: fill what the usage record lacks from Hermes's saved session state.

    Hermes 0.19 writes its usage file from the agent's result, which is empty when the
    agent raised and carries no tokens on an early failed return, so a failed run
    reports no model, session or tokens though the session rows hold them. A value
    Hermes wrote is never replaced or added to, and the tokens come from one source:
    when Hermes wrote any token count, all of them and the total are Hermes's.
    `failed` and `failure` are left alone; a `completed` Hermes left null on a failed
    run is false, so the adapter can read the record.
    """
    usage["duration_ms"] = _milliseconds(session["started_at"], session["ended_at"])
    usage["tool_calls"] = _integer(session["tool_call_count"])
    if usage.get("session_id") is None and session["id"] is not None:
        usage["session_id"] = session["id"]
    for field, column in SESSION_FIELDS:
        if usage.get(field) is None and session[column] is not None:
            usage[field] = session[column]
    if all(usage.get(field) is None for field in TOKEN_FIELDS):
        for field in TOKEN_FIELDS:
            usage[field] = _integer(session[field])
    if usage.get("total_tokens") is None:
        parts = [_integer(usage.get(field)) for field in TOTAL_PARTS]
        if any(part is not None for part in parts):
            usage["total_tokens"] = sum(part for part in parts if part is not None)


def _session(home: Path, session_id: object) -> dict[str, object] | None:
    path = home / "state.db"
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path)) as database:
            _check_columns(database)
            if isinstance(session_id, str):
                return _one_session(database, session_id)
            return _run_sessions(database)
    except sqlite3.Error:
        return None


def _enrich_usage(usage_path: Path, home: Path, max_turns: int = 0) -> None:
    """Add the run's duration, tool calls and, where Hermes left them out, its session,
    model and tokens to the usage record. A session table without the columns this was
    written against raises SessionSchemaChanged rather than writing silent nulls."""
    try:
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
        if max_turns > 0:
            # FDY-0140: whether the run ended on its turn budget. Hermes reports it as
            # not completed after asking the model for a summary, one call past it.
            calls = usage.get("api_calls")
            usage["max_turns"] = max_turns
            usage["turn_limit_reached"] = (
                isinstance(calls, int) and calls >= max_turns and usage.get("completed") is not True
            )
        if usage.get("failed") is True and usage.get("completed") is None:
            # #387: Hermes 0.19 writes `completed: null` when its agent raised.
            usage["completed"] = False
        try:
            session = _session(home, usage.get("session_id"))
            if session is not None:
                _fill_from_session(usage, session)
        except SessionSchemaChanged as error:
            print(error, file=sys.stderr, flush=True)
        temporary = usage_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(usage, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(usage_path)
    except (OSError, ValueError, AttributeError, TypeError):
        # The adapter records the original usage file or its parse anomaly. Enrichment
        # is secondary evidence and must not hide Hermes's own outcome.
        return


def inline_identity(argv: list[str], identity: str | None) -> list[str]:
    """The prompt after `-z`, with the identity file's text ahead of it. Unchanged when
    there is no identity file (a harness test has none) or no `-z`."""
    if not identity or "-z" not in argv:
        return argv
    try:
        text = Path(identity).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return argv
    if not text:
        return argv
    index = argv.index("-z") + 1
    if index >= len(argv):
        return argv
    prompt = f"{text}\n\n---\n\nThe task above is {identity}. {argv[index]}"
    return [*argv[:index], prompt, *argv[index + 1 :]]


def write_settings(home: Path, context_length: int, max_output_tokens: int = 0) -> None:
    """Hermes's own `model.context_length` and `model.max_tokens` (hades #388), in the
    per-run home it reads config from. Each is written only when a value was given; the
    home starts empty on every launch."""
    lines = []
    if context_length > 0:
        lines.append(f"  context_length: {context_length}\n")
    if max_output_tokens > 0:
        lines.append(f"  max_tokens: {max_output_tokens}\n")
    if not lines:
        return
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.yaml"
    config.write_text("model:\n" + "".join(lines), encoding="utf-8")


def settings_from_env(home: Path) -> tuple[int, int]:
    """The context length and response allowance Crucible passed, written to Hermes's
    config in `home`."""
    context_length = _limit("CRUCIBLE_HERMES_CONTEXT_LENGTH")
    allowance = _limit("CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS")
    write_settings(home, context_length, allowance)
    return context_length, allowance


def _session_stamp(home: Path) -> tuple[int, ...]:
    stamps = []
    for name in ("state.db", "state.db-wal"):
        try:
            stamps.append((home / name).stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    return tuple(stamps)


def watch_progress(home: Path, stop: threading.Event, interval: float = PROGRESS_SECONDS) -> None:
    """Write PROGRESS_LINE to stderr whenever Hermes's session store has changed since
    the last look. Hermes writes it after every model turn and tool call."""
    last = _session_stamp(home)
    while not stop.wait(interval):
        current = _session_stamp(home)
        if current != last:
            last = current
            print(PROGRESS_LINE, file=sys.stderr, flush=True)


def main() -> int:
    usage_path = Path(os.environ["CRUCIBLE_HERMES_USAGE"])
    home = Path(os.environ["HERMES_HOME"])
    max_turns = _limit("CRUCIBLE_HERMES_MAX_TURNS")
    context_length, allowance = settings_from_env(home)
    argv = inline_identity(sys.argv[1:], os.environ.get("CRUCIBLE_HERMES_IDENTITY"))
    # Hades #385, #388: the patches are checked before Hermes starts; the reason is on
    # stderr.
    preflight_env = {
        **os.environ,
        "CRUCIBLE_HERMES_EXPECTED_TRIGGER": str(compression_trigger(context_length, allowance)),
    }
    preflight = [HERMES_PYTHON, "-P", "-c", PREFLIGHT]
    if subprocess.run(preflight, check=False, env=preflight_env).returncode != 0:
        print(PREFLIGHT_FAILED, file=sys.stderr, flush=True)
        return 2
    # Stdout stays inherited. Crucible's launch wrapper is the sole transcript writer.
    # -P: the working directory is the task's checkout, and a module there named like
    # one of Hermes's own (`cli`, `tools`, `agent`) must never be imported in its place.
    try:
        child = subprocess.Popen([HERMES_PYTHON, "-P", "-c", BOOTSTRAP, *argv])
    except OSError:
        # An identity too long for one argument (E2BIG): the pointer alone still works.
        child = subprocess.Popen([HERMES_PYTHON, "-P", "-c", BOOTSTRAP, *sys.argv[1:]])

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    stop = threading.Event()
    watcher = threading.Thread(target=watch_progress, args=(home, stop), daemon=True)
    watcher.start()
    code = child.wait()
    stop.set()
    _enrich_usage(usage_path, home, max_turns)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
