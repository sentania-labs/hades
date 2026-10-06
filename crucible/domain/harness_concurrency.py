"""Shared adapter declarations used by policy validation (05b, 12).

Keeping these in the domain lets uploads validate the same declarations as adapters
without importing adapters into the application layer.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class HarnessConcurrency:
    minimum_mode: Literal["ro", "rw-narrow", "renewer"] = "rw-narrow"
    parallel_attempts_safe: bool = False
    renewer_held: bool = False

    @property
    def allows_parallel(self) -> bool:
        return self.minimum_mode == "ro" or self.parallel_attempts_safe or self.renewer_held


HARNESS_CONCURRENCY: dict[str, HarnessConcurrency] = {
    # The setup token never refreshes; neither auth file syncs back.
    "claude_code": HarnessConcurrency("ro", True),
    # Only the supervisor renews Codex tokens. Writable rollback copies stay serial.
    "codex": HarnessConcurrency("renewer", False, renewer_held=True),
    # Google does not rotate refresh tokens on ordinary access-token renewal.
    "agy": HarnessConcurrency("rw-narrow", True),
    "hermes": HarnessConcurrency("ro"),
    "qwen_code": HarnessConcurrency("ro"),
    "script-harness": HarnessConcurrency("ro"),
}
