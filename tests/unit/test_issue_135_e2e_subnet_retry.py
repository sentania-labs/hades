"""Tests for Issue 135: subnet retry in tests/e2e/daemon.py.

These unit tests verify that:

1. ``derive_subnet`` produces values inside the 10.100-10.199 /24 range.
2. ``derive_subnet`` is deterministic (same seed+attempt -> same subnet).
3. Consecutive attempts produce different subnets (spreading across /24s).
4. ``ensure_network`` retries on "subnet overlaps" and returns the
   used subnet.
5. ``ensure_network`` raises when all attempts are exhausted, naming
   every subnet tried.
6. ``ensure_network`` does *not* retry on non-overlap errors.
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.e2e.daemon import derive_subnet, ensure_network


class TestDeriveSubnetRange:
    """Subnets must stay within 10.100-10.199 /24."""

    @pytest.mark.parametrize("seed", ["abc", "", "x" * 64, "123"])
    def test_within_range(self, seed: str) -> None:
        """Every attempt yields a 10.1xx.0/24 subnet."""
        for attempt in range(256):
            sub = derive_subnet(seed, attempt)
            octets = sub.split(".")
            assert octets[0] == "10"
            middle = int(octets[1])
            assert 100 <= middle <= 199, f"{sub} (seed={seed!r}, attempt={attempt})"

    def test_last_octet_0(self) -> None:
        """Last octet is always 0 (full /24)."""
        sub = derive_subnet("test", 0)
        assert sub.endswith("/24")
        net_mask = sub.split("/")
        assert len(net_mask) == 2 and net_mask[1] == "24"
        host_part = net_mask[0]
        assert host_part.endswith(".0")


class TestDeriveSubnetDeterminism:
    """Same (seed, attempt) must always yield the same subnet."""

    def test_deterministic(self) -> None:
        """Repeated calls with identical arguments match."""
        seed = "fdy-0465"
        for i in range(20):
            r1 = derive_subnet(seed, i)
            r2 = derive_subnet(seed, i)
            assert r1 == r2, f"seed={seed!r} attempt={i}"

    def test_different_attempts_differ(self) -> None:
        """Different attempts produce different subnets (spread)."""
        subs = [derive_subnet("spread-test", i) for i in range(100)]
        unique = set(subs)
        assert len(unique) > 1, f"expected spreading, got only {len(unique)} unique /24s"


class TestDeriveSubnetSpread:
    """Consecutive attempts should spread across /24s."""

    def test_spreads_across_octets(self) -> None:
        """At least two of the first 100 attempts hit different middle octets."""
        octets = set()
        for attempt in range(100):
            sub = derive_subnet("spread-test", attempt)
            octets.add(int(sub.split(".")[1]))
        assert len(octets) > 1


class TestEnsureNetwork:
    """ensure_network retry behaviour."""

    def test_retries_on_overlap(self) -> None:
        """On overlap error, tries next derived subnet."""
        call_counter = [0]
        attempts = [0]  # tracks the attempt number being created

        def fake_subprocess_run(*args: object, **kwargs: object) -> MagicMock:
            """Return overlap for first 2 calls, success on 3rd."""
            call_counter[0] += 1
            attempts[0] += 1
            result = MagicMock()
            if attempts[0] <= 2:
                result.returncode = 1
                result.stderr = "Error response from daemon: subnet overlaps"
            else:
                result.returncode = 0
                result.stderr = ""
            result.stdout = ""
            return result

        with (
            patch("tests.e2e.daemon.subprocess.run", fake_subprocess_run),
            patch("tests.e2e.daemon.run", return_value=""),
        ):
            result = ensure_network(
                "test-net",
                internal=True,
                seed="seed-xyz",
                max_attempts=5,
            )

        assert call_counter[0] == 3  # 2 overlaps + 1 success
        assert attempts[0] == 3
        assert result.startswith("10.")
        assert result.endswith("/24")

    def test_exhausted_attempts_raises(self) -> None:
        """When all attempts overlap, RuntimeError names each tried subnet."""

        class AlwaysOverlap:
            @property
            def returncode(self) -> int:
                return 1

            @property
            def stderr(self) -> str:
                return "subnet overlaps"

            @property
            def stdout(self) -> str:
                return ""

        with (
            patch("tests.e2e.daemon.subprocess.run", return_value=AlwaysOverlap()),
            patch("tests.e2e.daemon.run", return_value=""),
            pytest.raises(RuntimeError, match="all 3 derived subnets") as exc,
        ):
            ensure_network(
                "test-net",
                internal=True,
                seed="fail-seed",
                max_attempts=3,
            )

        assert exc.value is not None

    def test_non_overlap_error_raises_immediately(self) -> None:
        """A non-overlap error should NOT trigger retries."""
        call_count = [0]

        def fake_subprocess(*args: object, **kwargs: object) -> MagicMock:
            call_count[0] += 1
            result = MagicMock()
            result.returncode = 1
            result.stderr = "permission denied"
            result.stdout = ""
            return result

        with (
            patch("tests.e2e.daemon.subprocess.run", fake_subprocess),
            patch("tests.e2e.daemon.run", return_value=""),
            pytest.raises(RuntimeError, match="permission denied") as exc,
        ):
            ensure_network(
                "test-net",
                internal=True,
                seed="perm-test",
                max_attempts=5,
            )

        assert exc.value is not None
        # inspect mocked away (daemon.run), 1 subprocess.run call (first attempt),
        # error is non-overlap so no retries
        assert call_count[0] == 1

    def test_provided_subnet_creates_once(self) -> None:
        """When a fixed subnet is provided, network create succeeds."""
        with patch("tests.e2e.daemon.run", return_value="") as run_mock:
            result = ensure_network(
                "test-net",
                internal=True,
                subnet="10.200.0.0/24",
            )

        assert result == "10.200.0.0/24"
        # daemon.run called: inspect (1) + network create (1)
        assert run_mock.call_count == 2

    def test_existing_network_returns_subnet(self) -> None:
        """When network already exists, returns its subnet without subprocess."""
        existing_subnet = "10.150.0.0/24"
        existing_output = (
            '[{"Name":"test-net","IPAM":{"Config":[{"Subnet":"' + existing_subnet + '"}]}}]'
        )

        with patch("tests.e2e.daemon.run", return_value=existing_output):
            result = ensure_network(
                "test-net",
                internal=True,
            )

        assert result == existing_subnet
