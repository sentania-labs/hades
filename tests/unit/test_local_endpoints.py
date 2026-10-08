from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from crucible.application.proxy_config import (
    enabled_local_endpoints,
    install_worker_proxy_config,
    worker_proxy_config,
)
from crucible.cli.wiring import enabled_database_endpoint
from crucible.ports.execution import LaunchSpec
from crucible.ports.harness import LaunchContext
from tests.wait import wait_until

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_proxy_install_waits_for_the_reload_acknowledgement(tmp_path: Path) -> None:
    path = tmp_path / "squid.conf"

    def acknowledge() -> None:
        marker = tmp_path / "reload"
        wait_until(marker.is_file, timeout=2, describe="proxy reload request")
        (tmp_path / "reloaded").write_text(marker.read_text(encoding="ascii"), encoding="ascii")

    thread = threading.Thread(target=acknowledge)
    thread.start()
    install_worker_proxy_config(path, "http_port 3128\n", reload_timeout_seconds=2)
    thread.join(timeout=2)
    assert path.read_text(encoding="utf-8") == "http_port 3128\n"


def test_launch_contracts_require_a_local_url_and_forbid_a_subscription_url() -> None:
    with pytest.raises(ValueError, match="requires endpoint_url"):
        LaunchContext(
            attempt_id="attempt",
            model="model",
            effort=None,
            timeout_seconds=30,
            identity_mount="/identity",
            report_mount="/report",
            repo_mount="/repo",
            endpoint="local",
        )
    with pytest.raises(ValueError, match="forbids endpoint_url"):
        LaunchSpec(
            attempt_id="attempt",
            task_id="task",
            external_id="EX-1",
            role="work",
            harness="codex",
            model="model",
            image="image",
            timeout_seconds=30,
            contract={},
            endpoint="subscription",
            endpoint_url="http://example.invalid/v1",
        )


def test_proxy_allows_only_the_exact_local_destination_and_plain_http_port() -> None:
    routing = {
        "models": [
            {
                "endpoint": "local",
                "endpoint_url": "http://192.0.2.41:11434/v1",
                "enabled": True,
            },
            {
                "endpoint": "local",
                "endpoint_url": "http://192.0.2.99:11434/v1",
                "enabled": False,
            },
        ]
    }
    assert enabled_local_endpoints([routing]) == ["http://192.0.2.41:11434/v1"]
    config = worker_proxy_config("10.88.0.0/24", ["github.com"], [routing])
    assert "acl local_destination_0 dst 192.0.2.41/32" in config
    assert "acl local_port_0 port 11434" in config
    assert "acl Safe_ports port 11434" in config
    assert "acl SSL_ports port 11434" not in config
    assert "192.0.2.99" not in config
    unsafe = config.index("http_access deny !Safe_ports")
    connect = config.index("http_access deny CONNECT !SSL_ports")
    local = config.index("http_access allow workers local_destination_0 local_port_0")
    final = config.index("http_access deny all")
    assert unsafe < connect < local < final


def test_proxy_allows_https_local_destinations_by_exact_name_and_port() -> None:
    config = worker_proxy_config(
        "10.88.0.0/24",
        [],
        [
            {
                "models": [
                    {
                        "endpoint": "local",
                        "endpoint_url": "https://spark.example.invalid/v1",
                        "enabled": True,
                    }
                ]
            }
        ],
    )
    assert "acl local_destination_0 dstdomain spark.example.invalid" in config
    assert "acl local_port_0 port 443" in config
    assert "acl SSL_ports port 443" in config
    assert "http_access allow workers local_destination_0 local_port_0" in config
    assert config.count("acl Safe_ports port 443") == 1


def test_make_proxy_config_does_not_authorize_the_environment_seed() -> None:
    endpoint = "http://192.0.2.41:11434/v1"
    result = subprocess.run(
        ["make", "--dry-run", "proxy-config", f"CRUCIBLE_SPARK_ENDPOINT_URL={endpoint}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--configured-local-endpoint" not in result.stdout
    assert endpoint not in result.stdout


def test_enabled_database_endpoint_is_none_with_every_local_model_disabled() -> None:
    """A restart must not put a disabled destination back on the Docker allowlist
    (crucible.cli.wiring.wire reads this to seed proxy_allowlist)."""
    routing_document = {
        "models": [
            {
                "endpoint": "local",
                "endpoint_url": "https://llm.apps.int.sentania.net/v1",
                "enabled": False,
            }
        ]
    }
    assert enabled_database_endpoint(routing_document) is None
    assert enabled_database_endpoint(None) is None
    routing_document["models"][0]["enabled"] = True
    assert enabled_database_endpoint(routing_document) == "https://llm.apps.int.sentania.net/v1"


def test_make_deploy_local_maps_the_compatibility_seed_to_the_local_endpoint() -> None:
    endpoint = "http://192.0.2.41:11434/v1"
    result = subprocess.run(
        ["make", "--dry-run", "deploy-local", f"CRUCIBLE_SPARK_ENDPOINT_URL={endpoint}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f'CRUCIBLE_LOCAL_ENDPOINT_URL="{endpoint}"' in result.stdout
