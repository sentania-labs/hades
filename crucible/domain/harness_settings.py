"""A harness's run settings an administrator saves (FDY-0140).

They are kept as the `provider_settings` row `harness.<name>` and reach the adapter
through the launch spec, so a change applies to the next launch in every process. Hermes
is the one harness with any: how many model turns one run may take, the context window
it is told the gateway model has, and the response allowance (hades #388).

Hades #388: the gateway reserves a response allowance out of the window on every request
whether or not the request names one, so Hermes is told that allowance too. Its context
compressor then budgets input against the window less that reservation, and its requests
carry `max_tokens`. The context length, the allowance and the routing entry's thinking
setting a launch uses are resolved once per attempt and recorded on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Hermes 0.19 stops `-z` at 90 turns and assumes a 256k window when the gateway does not
# say. A task that reads a repository, edits several files and runs its checks can use
# several hundred turns; 300 leaves room for that without letting a looping model run
# for ever. 131072 is a common window for the coding models a local gateway serves and
# above Hermes's 64000 floor; it is not read from the gateway, so a model with a smaller
# window needs its real figure saved, or 0 to let Hermes probe for it.
# Qwen's full engine window when the routing entry does not override it (#448).
DEFAULT_QWEN_CONTEXT_LENGTH = 131_072
# hades #498: Qwen's response cap when the saved limits name none, the same allowance
# Hermes runs with; Qwen clamps it to the room left in the window.
DEFAULT_QWEN_MAX_OUTPUT_TOKENS = 32_000
DEFAULT_HERMES_MAX_TURNS = 300
DEFAULT_HERMES_CONTEXT_LENGTH = 131_072
# Hades #388: the response allowance the lab's gateway applies when a request names none.
DEFAULT_HERMES_MAX_OUTPUT_TOKENS = 32_000
MAX_TURNS_RANGE = (10, 5000)
# 0 means "let Hermes find the window itself".
CONTEXT_LENGTH_RANGE = (64_000, 2_000_000)
MAX_OUTPUT_TOKENS_RANGE = (1024, 1_000_000)


def setting_name(harness: str) -> str:
    """The provider_settings row that holds one harness's run settings."""
    return f"harness.{harness}"


@dataclass(frozen=True, slots=True)
class HermesRunLimits:
    max_turns: int = DEFAULT_HERMES_MAX_TURNS
    context_length: int = DEFAULT_HERMES_CONTEXT_LENGTH
    max_output_tokens: int = DEFAULT_HERMES_MAX_OUTPUT_TOKENS
    # The routing entry's thinking setting, added at launch; never saved with the limits.
    thinking: bool = False

    def as_dict(self) -> dict[str, int]:
        """The limits an administrator saves."""
        return {
            "max_turns": self.max_turns,
            "context_length": self.context_length,
            "max_output_tokens": self.max_output_tokens,
        }


def hermes_run_limits(document: Mapping[str, Any] | None) -> HermesRunLimits:
    """The saved limits, with the defaults for anything absent or not a whole number."""
    values = document or {}

    def whole(name: str, default: int) -> int:
        value = values.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    return HermesRunLimits(
        max_turns=whole("max_turns", DEFAULT_HERMES_MAX_TURNS),
        context_length=whole("context_length", DEFAULT_HERMES_CONTEXT_LENGTH),
        max_output_tokens=whole("max_output_tokens", DEFAULT_HERMES_MAX_OUTPUT_TOKENS),
        thinking=values.get("thinking") is True,
    )


def qwen_effective_settings(
    document: Mapping[str, Any] | None, *, context_length: int | None
) -> dict[str, int | bool]:
    """hades #498: what one Qwen attempt runs with, recorded on the attempt at launch as
    the Hermes values are: the window (the routing entry's, else the default), the
    response cap from the saved limits, and thinking, which is always off for Qwen."""
    limits = hermes_run_limits(document)
    saved = (document or {}).get("max_output_tokens")
    return {
        "context_length": context_length or DEFAULT_QWEN_CONTEXT_LENGTH,
        "max_output_tokens": limits.max_output_tokens
        if isinstance(saved, int) and not isinstance(saved, bool)
        else DEFAULT_QWEN_MAX_OUTPUT_TOKENS,
        "thinking": False,
    }


def effective_settings(
    document: Mapping[str, Any] | None, *, thinking: bool
) -> dict[str, int | bool]:
    """Hades #388: the context length, response allowance and thinking setting one
    attempt runs with, from the saved limits and its routing entry. Recorded on the
    attempt at its launch; every later spec of that attempt reuses the record."""
    limits = hermes_run_limits(document)
    return {
        "context_length": limits.context_length,
        "max_output_tokens": limits.max_output_tokens,
        "thinking": thinking,
    }


def hermes_limit_problems(
    max_turns: int,
    context_length: int,
    max_output_tokens: int = DEFAULT_HERMES_MAX_OUTPUT_TOKENS,
) -> list[str]:
    """Why a set of limits cannot be saved, in words; empty when they can."""
    problems = []
    low, high = MAX_TURNS_RANGE
    if not low <= max_turns <= high:
        problems.append(f"max turns must be between {low} and {high}")
    low, high = CONTEXT_LENGTH_RANGE
    if context_length != 0 and not low <= context_length <= high:
        problems.append(
            f"the context length must be 0 (Hermes finds it) or between {low} and {high} tokens"
        )
    low, high = MAX_OUTPUT_TOKENS_RANGE
    if not low <= max_output_tokens <= high:
        problems.append(f"the max output tokens must be between {low} and {high}")
    elif context_length and max_output_tokens >= context_length:
        problems.append("the max output tokens must be less than the context length")
    return problems
