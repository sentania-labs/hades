"""Deadline-based waiting helpers for tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import overload

_POLL_INTERVAL = 0.01


def wait_until[T](condition: Callable[[], T], *, timeout: float = 5.0, describe: str) -> T:
    """Return the first truthy observed state, or fail with useful context."""
    deadline = time.monotonic() + timeout
    while True:
        observed = condition()
        if observed:
            return observed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"Timed out waiting for {describe}; last observed state: {observed!r}"
            )
        time.sleep(min(_POLL_INTERVAL, remaining))


@overload
async def async_wait_until[T](
    condition: Callable[[], Awaitable[T]], *, timeout: float = 5.0, describe: str
) -> T: ...


@overload
async def async_wait_until[T](
    condition: Callable[[], T], *, timeout: float = 5.0, describe: str
) -> T: ...


async def async_wait_until[T](
    condition: Callable[[], Awaitable[T] | T], *, timeout: float = 5.0, describe: str
) -> T:
    """Async counterpart to :func:`wait_until`."""
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        observed = await result if isinstance(result, Awaitable) else result
        if observed:
            return observed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"Timed out waiting for {describe}; last observed state: {observed!r}"
            )
        await asyncio.sleep(min(_POLL_INTERVAL, remaining))
