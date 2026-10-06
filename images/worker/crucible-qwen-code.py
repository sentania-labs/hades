#!/usr/bin/python3
"""Configure Qwen's per-attempt home, then replace this process with its CLI.

The provider owns transcript capture. Like the Hermes wrapper, the full identity
in the prompt instructs the agent to write report.yaml against CompletionClaimV1;
we never manufacture a successful claim from the CLI's final prose.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def write_settings(home: Path, context_length: int) -> None:
    if context_length <= 0:
        raise ValueError("Qwen context length must be positive")
    directory = home / ".qwen"
    directory.mkdir(parents=True, exist_ok=True)
    # generationConfig lives under model in Qwen 0.25.0. Its OpenAI generator
    # clamps max_tokens to the room left in this full engine window, including
    # its output margin, rather than adding 32000 to an unbounded input budget.
    settings = {
        "tools": {"shell": {"enableInteractiveShell": False}, "useBuiltinRipgrep": False},
        "model": {
            "maxToolCallsPerTurn": 0,
            "generationConfig": {"contextWindowSize": context_length},
        },
    }
    (directory / "settings.json").write_text(json.dumps(settings) + "\n", encoding="utf-8")


def launch_argv(argv: list[str], identity: Path) -> list[str]:
    text = identity.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("Qwen identity must not be empty")
    return ["/usr/local/bin/qwen", *argv[:-1], f"{text}\n\n{argv[-1]}"]


def main() -> None:
    write_settings(Path.home(), int(os.environ["CRUCIBLE_QWEN_CONTEXT_LENGTH"]))
    argv = launch_argv(sys.argv[1:], Path(os.environ["CRUCIBLE_QWEN_IDENTITY"]))
    # exec keeps termination signals and the CLI's exact exit code intact.
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
