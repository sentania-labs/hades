"""Regression tests for Issue 136's e2e stack network cleanup."""

import pytest

from tests.e2e.conftest import NET_CONTROL, NET_WORKERS, RUN_ID, _create_stack_networks


class FakeDaemon:
    """Records network operations without requiring Docker."""

    def __init__(self, *, fail_workers: bool) -> None:
        self.fail_workers = fail_workers
        self.ensure_count = 0
        self.ensure_calls: list[tuple[str, bool, str | None]] = []
        self.removed: list[str] = []

    def ensure_network(
        self,
        name: str,
        *,
        internal: bool,
        subnet: str | None = None,
        seed: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        del subnet, max_attempts
        self.ensure_count += 1
        self.ensure_calls.append((name, internal, seed))
        if self.ensure_count == 2 and self.fail_workers:
            raise RuntimeError("subnet collision")
        return "10.100.0.0/24" if name == NET_WORKERS else ""

    def remove_network(self, name: str) -> None:
        self.removed.append(name)


def test_workers_network_failure_removes_control_network_and_propagates() -> None:
    """The second create failure does not leave the control network behind."""
    daemon = FakeDaemon(fail_workers=True)

    with pytest.raises(RuntimeError, match="subnet collision"):
        _create_stack_networks(daemon)

    assert daemon.ensure_calls == [
        (NET_CONTROL, False, None),
        (NET_WORKERS, True, RUN_ID),
    ]
    assert daemon.removed == [NET_CONTROL]


def test_successful_network_creation_preserves_normal_teardown_ownership() -> None:
    """Successful creates return the workers subnet and defer cleanup to the fixture."""
    daemon = FakeDaemon(fail_workers=False)

    assert _create_stack_networks(daemon) == "10.100.0.0/24"
    assert daemon.ensure_calls == [
        (NET_CONTROL, False, None),
        (NET_WORKERS, True, RUN_ID),
    ]
    assert daemon.removed == []
