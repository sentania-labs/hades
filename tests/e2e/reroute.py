"""Small assertions shared by the container and kind reroute cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def first_reroute_pair(
    attempts: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Return the original attempt and its successor from an observed history.

    Collection and rerouting run beside a supervisor tick. A poll that is waiting for
    the first reroute may therefore observe a later attempt too. That later history
    does not change which two attempts demonstrate the first reroute.
    """
    if len(attempts) < 2:
        raise ValueError("the first reroute has no successor")
    return attempts[0], attempts[1]
