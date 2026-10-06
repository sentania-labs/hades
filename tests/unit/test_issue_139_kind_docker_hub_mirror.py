"""hades #139: docker.io mirror in deploy-kind and the kind helpers.

Two parts remain from the split:
- deploy-kind.sh now writes /etc/containerd/certs.d/docker.io/hosts.toml
  so the cluster node can pull Docker Hub images through the mirror (mirror.gcr.io)
  with registry-1.docker.io as fallback.
- verify_requests_below_limits.sh no longer pulls busybox by tag.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class TestDeployKindDockerIoMirror:
    """AC1: deploy-kind writes a docker.io hosts.toml naming mirror.gcr.io and
    registry-1.docker.io."""

    def _source(self) -> str:
        return (REPO / "tools/kind/deploy-kind.sh").read_text(encoding="utf-8")

    def test_writes_docker_io_hosts_toml_path(self) -> None:
        """deploy-kind.sh creates /etc/containerd/certs.d/docker.io/."""
        content = self._source()
        assert "mkdir -p" in content and "certs.d/docker.io" in content

    def test_hosts_toml_names_mirror_gcr_io(self) -> None:
        """The hosts.toml references mirror.gcr.io via CRUCIBLE_KIND_DOCKER_HUB_MIRROR."""
        content = self._source()
        assert "CRUCIBLE_KIND_DOCKER_HUB_MIRROR" in content

    def test_hosts_toml_names_registry_1_docker_io(self) -> None:
        """The hosts.toml references registry-1.docker.io as the fallback."""
        content = self._source()
        assert "registry-1.docker.io" in content

    def test_hosts_toml_is_written_with_curl_ca_uses_certs_d_docker_io(self) -> None:
        """The full docker.io hosts.toml write path is present."""
        content = self._source()
        assert "/etc/containerd/certs.d/docker.io/hosts.toml" in content

    def test_local_registry_config_unchanged(self) -> None:
        """deploy-kind still writes the local registry config under certs.d/."""
        content = self._source()
        # The local registry hosts.toml write must still exist
        assert "certs.d/${registry_host}:5000/hosts.toml" in content


class TestNoBusyboxByTag:
    """AC2: No script under tools/kind pulls busybox by tag."""

    KIND_DIR = REPO / "tools/kind"

    def _all_kind_scripts(self) -> list[str]:
        """Return content of every *.sh under tools/kind."""
        results: list[str] = []
        for sh in sorted(self.KIND_DIR.glob("*.sh")):
            results.append(sh.read_text(encoding="utf-8"))
        return results

    def test_no_docker_pull_busybox_with_tag(self) -> None:
        """No script under tools/kind runs `docker pull` on a busybox tag."""
        for content in self._all_kind_scripts():
            # Match patterns like `docker pull busybox:TAG` or `docker pull library/busybox:TAG`
            matches = re.findall(r"docker\s+pull\s+\S*busybox:\S+", content)
            assert not matches, f"Found busybox tag pull: {matches}"

    def test_no_kind_load_busybox_by_tag(self) -> None:
        """No script under tools/kind loads busybox by tag (only by digest)."""
        for content in self._all_kind_scripts():
            # kind load docker-image busybox:TAG should not appear
            matches = re.findall(r"kind\s+load\s+\S*busybox:\S+", content)
            assert not matches, f"Found busybox tag load: {matches}"

    def test_verify_requests_below_limits_uses_crucible_kind_pull(self) -> None:
        """verify_requests_below_limits.sh uses crucible_kind_pull with the
        digest-pinned CRUCIBLE_BUSYBOX_IMAGE."""
        content = (self.KIND_DIR / "verify_requests_below_limits.sh").read_text(encoding="utf-8")
        assert "crucible_kind_pull" in content
        assert "CRUCIBLE_BUSYBOX_IMAGE" in content
