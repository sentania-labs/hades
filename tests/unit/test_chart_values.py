"""The chart's deployer values (hades #601) and the workers Role's patch verb (#575).

helm is not in the worker image, so these read the chart's files: the values, the
schema, and which value each template line renders. The CI chart job (`make chart`,
`make chart-sync-check`) renders the chart and runs `tools/chart/flow.py` on the
renders, which is the proof that the values reach the objects.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.chart.flow import problems
from tools.chart.sync import without_namespaces

ROOT = Path(__file__).parents[2]
CHART = ROOT / "charts/hades"
TEMPLATES = CHART / "templates"
SCHEMA: dict[str, Any] = json.loads((CHART / "values.schema.json").read_text())
VALUES: dict[str, Any] = yaml.safe_load((CHART / "values.yaml").read_text())
LAB: dict[str, Any] = yaml.safe_load((CHART / "values-lab-example.yaml").read_text())
BASE_VALUES: dict[str, Any] = yaml.safe_load((ROOT / "tools/chart/values-base.yaml").read_text())
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


def _resolve(node: dict[str, Any]) -> dict[str, Any]:
    reference = node.get("$ref")
    if reference:
        resolved: dict[str, Any] = SCHEMA["$defs"][reference.rsplit("/", 1)[-1]]
        return _resolve(resolved)
    return node


def _type_ok(expected: str, value: Any) -> bool:
    checks: dict[str, bool] = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
    }
    return checks[expected]


def _errors(node: dict[str, Any], value: Any, where: str = "") -> list[str]:
    """The subset of JSON Schema values.schema.json uses, enough to check a values file."""
    node = _resolve(node)
    found: list[str] = []
    if "type" in node and not _type_ok(node["type"], value):
        return [f"{where}: {value!r} is not {node['type']}"]
    if "const" in node and value != node["const"]:
        found.append(f"{where}: {value!r} is not {node['const']!r}")
    if "enum" in node and value not in node["enum"]:
        found.append(f"{where}: {value!r} is not one of {node['enum']}")
    if "not" in node and not _errors(node["not"], value, where):
        found.append(f"{where}: {value!r} matches what is excluded")
    if "anyOf" in node and all(_errors(option, value, where) for option in node["anyOf"]):
        found.append(f"{where}: {value!r} matches no option")
    if "if" in node and not _errors(node["if"], value, where) and "then" in node:
        found.extend(_errors(node["then"], value, where))
    if isinstance(value, str):
        if "pattern" in node and not re.search(node["pattern"], value):
            found.append(f"{where}: {value!r} does not match {node['pattern']}")
        if len(value) < node.get("minLength", 0):
            found.append(f"{where}: {value!r} is too short")
        if len(value) > node.get("maxLength", len(value)):
            found.append(f"{where}: {value!r} is longer than {node['maxLength']}")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < node.get("minimum", value):
            found.append(f"{where}: {value} is below {node['minimum']}")
        if value > node.get("maximum", value):
            found.append(f"{where}: {value} is above {node['maximum']}")
    if isinstance(value, list):
        if len(value) < node.get("minItems", 0):
            found.append(f"{where}: too few items")
        if node.get("uniqueItems") and len({json.dumps(v) for v in value}) != len(value):
            found.append(f"{where}: items repeat")
        for index, item in enumerate(value):
            if "items" in node:
                found.extend(_errors(node["items"], item, f"{where}[{index}]"))
    if isinstance(value, dict):
        properties = node.get("properties", {})
        for key in node.get("required", []):
            if key not in value:
                found.append(f"{where}: {key} is required")
        names = node.get("propertyNames")
        for key, item in value.items():
            path = f"{where}.{key}" if where else key
            if names:
                found.extend(_errors(names, key, f"{path} (name)"))
            if key in properties:
                found.extend(_errors(properties[key], item, path))
            elif node.get("additionalProperties") is False:
                found.append(f"{path}: not allowed")
            elif isinstance(node.get("additionalProperties"), dict):
                found.extend(_errors(node["additionalProperties"], item, path))
    return found


def _schema_node(dotted: str) -> dict[str, Any]:
    node = SCHEMA
    for part in dotted.split("."):
        node = _resolve(node["properties"][part])
    return node


def _value(values: dict[str, Any], dotted: str) -> Any:
    node: Any = values
    for part in dotted.split("."):
        node = node[part]
    return node


def _with(values: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    copy: dict[str, Any] = json.loads(json.dumps(values))
    parts = dotted.split(".")
    node = copy
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return copy


def _settings_template() -> dict[str, str]:
    """Each ConfigMap setting and the template expression that renders it."""
    text = (TEMPLATES / "configmap.yaml").read_text()
    body = text.split('{{- define "hades.settings" }}', 1)[1]
    result = {}
    lines = body.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(CRUCIBLE_[A-Z0-9_]+): ?(.*)$", line)
        if match:
            value = match[2]
            if value == ">-":
                value = lines[index + 1].strip()
            result[match[1]] = value
    return result


# hades #601, item by item: the value, its schema type, and the setting it renders.
TYPED_SETTINGS = {
    "CRUCIBLE_KUBERNETES__CLUSTER_DNS_IP": ("cluster.dnsIp", "string"),
    "CRUCIBLE_SERVICE__RENDER_TIMEZONE": ("cluster.renderTimezone", "string"),
    "CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_NAMESPACE": ("cluster.localEndpoint.namespace", None),
    "CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_POD_LABELS": ("cluster.localEndpoint.podLabels", "object"),
    "CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_PORT": ("cluster.localEndpoint.port", "integer"),
    "CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_CIDRS": ("cluster.localEndpoint.cidrs", "array"),
    "CRUCIBLE_KUBERNETES__DNS_NAMESPACE": ("cluster.dnsNamespace", "string"),
    "CRUCIBLE_KUBERNETES__DNS_POD_LABELS": ("cluster.dnsPodLabels", "object"),
    "CRUCIBLE_KUBERNETES__POD_PID_LIMIT_OVERRIDE": ("cluster.podPidLimitOverride", "integer"),
    "CRUCIBLE_KUBERNETES__STORAGE_CLASS": ("provider.workspaceStorageClass", "string"),
    "CRUCIBLE_KUBERNETES__IMAGE_PULL_SECRET": ("cluster.imagePullSecret", None),
    "CRUCIBLE_KUBERNETES__WORKSPACE_SIZE": ("provider.workspaceSize", "string"),
    "CRUCIBLE_KUBERNETES__MAX_CONCURRENCY": ("provider.maxConcurrency", "integer"),
    "CRUCIBLE_KUBERNETES__LAUNCH_TIMEOUT_SECONDS": ("provider.launchTimeoutSeconds", "integer"),
    "CRUCIBLE_KUBERNETES__PREPARE_TIMEOUT_SECONDS": ("provider.prepareTimeoutSeconds", "integer"),
    "CRUCIBLE_KUBERNETES__COLLECTOR_TIMEOUT_SECONDS": (
        "provider.collectorTimeoutSeconds",
        "integer",
    ),
    "CRUCIBLE_KUBERNETES__VERIFIER_TIMEOUT_SECONDS": (
        "provider.verifierTimeoutSeconds",
        "integer",
    ),
}

NAME_SETTINGS = {
    "CRUCIBLE_KUBERNETES__NAMESPACE": '{{ include "hades.namespace" . | quote }}',
    "CRUCIBLE_KUBERNETES__WORKERS_NAMESPACE": '{{ include "hades.workersNamespace" . | quote }}',
    "CRUCIBLE_KUBERNETES__SERVICE_ACCOUNT": '{{ printf "%s-worker" $name | quote }}',
    "CRUCIBLE_KUBERNETES__CACHE_CLAIM": '{{ printf "%s-reference-cache" $name | quote }}',
}


@pytest.mark.parametrize(
    "path",
    [
        "charts/hades/values.yaml",
        "charts/hades/values-lab-example.yaml",
        "tools/chart/values-base.yaml",
    ],
)
def test_every_values_file_satisfies_the_schema(path: str) -> None:
    values = yaml.safe_load((ROOT / path).read_text())
    assert _errors(SCHEMA, values) == []
    assert set(values) == set(SCHEMA["required"])


def test_the_overrides_exist_typed_and_default_to_the_base_names() -> None:
    for key in ("nameOverride", "namespaceOverride", "workersNamespaceOverride"):
        assert VALUES[key] == ""
        assert _errors(SCHEMA["properties"][key], "hades") == []
        assert _errors(SCHEMA["properties"][key], "Not_A_Name") != []
    helpers = (TEMPLATES / "_helpers.tpl").read_text()
    assert '.Values.nameOverride | default "hades"' in helpers
    assert '.Values.namespaceOverride | default (include "hades.name" .)' in helpers
    assert (
        '.Values.workersNamespaceOverride | default (printf "%s-workers" '
        '(include "hades.namespace" .))'
    ) in helpers


@pytest.mark.parametrize(
    ("overrides", "accepted"),
    [
        # The longest suffix that must stay a 63-character DNS label: the bundled
        # `<name>-postgres` StatefulSet, whose controller-revision-hash label value is
        # the name plus an 11-character hash (52 + 11), so 43 for the name.
        ({"nameOverride": "n" * 43}, True),
        ({"nameOverride": "n" * 44}, False),
        # The default workers namespace is `<namespace>-workers`: 55 + 8.
        ({"namespaceOverride": "s" * 55}, True),
        ({"namespaceOverride": "s" * 56}, False),
        # A workers namespace of its own frees the control plane's to 63.
        ({"namespaceOverride": "s" * 63, "workersNamespaceOverride": "w" * 63}, True),
        ({"namespaceOverride": "s" * 64, "workersNamespaceOverride": "w" * 63}, False),
        ({"workersNamespaceOverride": "w" * 64}, False),
    ],
)
def test_the_overrides_are_bounded_by_the_names_they_compose(
    overrides: dict[str, str], accepted: bool
) -> None:
    values = json.loads(json.dumps(VALUES))
    values.update(overrides)
    assert (_errors(SCHEMA, values) == []) is accepted


@pytest.mark.parametrize(
    "path",
    [
        "cluster.imagePullSecret",
        "postgres.databaseSecretName",
        "secrets.githubApp",
        "secrets.githubAppPrivateKey",
        "secrets.githubAppWebhook",
        "secrets.firstRunToken",
    ],
)
def test_secret_references_take_dns_subdomain_names(path: str) -> None:
    assert _errors(SCHEMA, _with(VALUES, path, "ghcr.io-pull")) == []
    assert _errors(SCHEMA, _with(VALUES, path, "x" * 253)) == []
    for wrong in ("x" * 254, "Ghcr.io", "ghcr..io", ".ghcr", "ghcr.io."):
        assert _errors(SCHEMA, _with(VALUES, path, wrong)) != [], wrong


def test_the_names_flow_into_the_settings_the_service_reads() -> None:
    settings = _settings_template()
    for key, expression in NAME_SETTINGS.items():
        assert settings[key] == expression
    secrets = settings["CRUCIBLE_KUBERNETES__CREDENTIAL_SECRETS"]
    for harness in ("claude-code", "codex", "agy", "hermes"):
        assert f'"{{{{ $name }}}}-harness-{harness}"' in secrets
    assert "crucible-harness" not in secrets


def test_no_template_hardcodes_a_crucible_namespace_or_name() -> None:
    for template in TEMPLATES.glob("*.yaml"):
        text = template.read_text()
        assert not re.search(r"^\s+namespace: crucible(-workers)?$", text, re.M), template.name
        names = re.findall(
            r"^\s+(?:- )?(?:name|claimName|serviceAccountName|serviceName): (crucible\S*)$",
            text,
            re.M,
        )
        assert names == [], template.name


@pytest.mark.parametrize("setting", sorted(TYPED_SETTINGS))
def test_each_cluster_fact_and_provider_knob_is_a_typed_value_in_its_setting(
    setting: str,
) -> None:
    path, kind = TYPED_SETTINGS[setting]
    node = _schema_node(path)
    if kind is None:
        assert node.get("anyOf"), path
    else:
        assert node.get("type") == kind, path
    _value(VALUES, path)
    assert f".Values.{path} " in _settings_template()[setting]


def test_the_provider_knobs_left_room_runner() -> None:
    moved = {"workspaceSize", "maxConcurrency"} | {
        f"{role}TimeoutSeconds" for role in ("launch", "prepare", "collector", "verifier")
    }
    room = SCHEMA["properties"]["roomRunner"]["properties"]
    assert not moved & set(room)
    assert not moved & set(VALUES["roomRunner"])
    assert moved <= set(VALUES["provider"])
    assert ".Values.roomRunner" not in "".join(
        line
        for line in (TEMPLATES / "configmap.yaml").read_text().splitlines(True)
        if "CRUCIBLE_KUBERNETES__" in line
    )
    assert _errors(SCHEMA, _with(VALUES, "roomRunner.maxConcurrency", 3)) != []


def test_the_optional_settings_render_only_when_set() -> None:
    text = (TEMPLATES / "configmap.yaml").read_text()
    for path, key in (
        ("provider.workspaceStorageClass", "STORAGE_CLASS"),
        ("cluster.imagePullSecret", "IMAGE_PULL_SECRET"),
        ("cluster.podPidLimitOverride", "POD_PID_LIMIT_OVERRIDE"),
    ):
        assert f"{{{{- if .Values.{path} }}}}\nCRUCIBLE_KUBERNETES__{key}:" in text
    assert VALUES["cluster"]["podPidLimitOverride"] == 0
    assert _errors(SCHEMA, _with(VALUES, "cluster.podPidLimitOverride", -1)) != []
    for template in ("api.yaml", "supervisor.yaml", "migrate-job.yaml"):
        assert (
            "      {{- if .Values.cluster.imagePullSecret }}\n      imagePullSecrets:\n"
            "        - name: {{ .Values.cluster.imagePullSecret | quote }}\n"
        ) in (TEMPLATES / template).read_text()


def test_extra_settings_merge_last_and_the_schema_admits_only_string_settings() -> None:
    text = (TEMPLATES / "configmap.yaml").read_text()
    assert '{{- $settings := include "hades.settings" . | fromYaml }}' in text
    assert "toYaml (mustMergeOverwrite $settings .Values.extraSettings)" in text
    assert VALUES["extraSettings"] == {}
    ok = _with(VALUES, "extraSettings", {"CRUCIBLE_SUPERVISOR__HOLDER": "hades-kubernetes"})
    assert _errors(SCHEMA, ok) == []
    assert _errors(SCHEMA, _with(VALUES, "extraSettings", {"CRUCIBLE_X": 1})) != []
    assert _errors(SCHEMA, _with(VALUES, "extraSettings", {"lower_case": "x"})) != []
    assert _errors(SCHEMA, _with(VALUES, "anythingElse", {})) != []


def test_every_settings_value_is_quoted_so_the_merged_map_holds_strings() -> None:
    for key, expression in _settings_template().items():
        if expression.startswith("{{"):
            assert expression.endswith("| quote }}"), key


def test_buildkit_is_off_by_default_and_gates_its_namespace_and_objects() -> None:
    assert VALUES["buildkit"] == {"enabled": False, "namespace": "hades-buildkit"}
    assert _schema_node("buildkit.enabled")["type"] == "boolean"
    namespace = SCHEMA["properties"]["buildkit"]["properties"]["namespace"]
    assert _errors(namespace, "crucible-buildkit") == []
    assert _errors(namespace, "Not_A_Name") != []
    buildkit = (TEMPLATES / "buildkit.yaml").read_text()
    before, inside = buildkit.split("{{- if .Values.buildkit.enabled }}\n", 1)
    assert "apiVersion:" not in before
    assert inside.rstrip().endswith("{{- end }}")
    namespaces = (TEMPLATES / "namespaces.yaml").read_text()
    gated = namespaces.split("{{- if .Values.buildkit.enabled }}", 1)[1]
    assert "  name: {{ .Values.buildkit.namespace }}\n" in gated
    assert 'kubernetes.io/metadata.name: {{ include "hades.workersNamespace" . }}' in buildkit
    assert BASE_VALUES["buildkit"]["enabled"] is True


def test_each_claim_has_its_own_storage_class() -> None:
    helpers = (TEMPLATES / "_helpers.tpl").read_text()
    pvcs = (TEMPLATES / "pvcs.yaml").read_text()
    buildkit = (TEMPLATES / "buildkit.yaml").read_text()
    for claim, template in (
        ("artifacts", pvcs),
        ("referenceCache", pvcs),
        ("buildkitCache", buildkit),
    ):
        assert f".Values.storage.{claim}StorageClass | default .Values.storage.storageClass" in (
            helpers
        )
        assert f'{{{{- with include "hades.{claim}StorageClass" . }}}}' in template
        assert _schema_node(f"storage.{claim}StorageClass")["type"] == "string"
    assert ".Values.storage.storageClass" not in pvcs + buildkit
    assert (
        "storageClassName: {{ .Values.postgres.storageClass | quote }}"
        in (TEMPLATES / "postgres.yaml").read_text()
    )
    assert LAB["storage"]["artifactsStorageClass"] != LAB["postgres"]["storageClass"]


def test_postgres_user_and_database_are_values() -> None:
    assert (VALUES["postgres"]["user"], VALUES["postgres"]["database"]) == ("crucible",) * 2
    template = (TEMPLATES / "postgres.yaml").read_text()
    assert "value: {{ .Values.postgres.user | quote }}" in template
    assert "value: {{ .Values.postgres.database | quote }}" in template
    probe = (
        'command: ["pg_isready", "-U", {{ .Values.postgres.user | quote }}, '
        '"-d", {{ .Values.postgres.database | quote }}]'
    )
    assert template.count(probe) == 2
    assert '"crucible"' not in template
    assert _errors(SCHEMA, _with(VALUES, "postgres.user", "Not-A-Role")) != []


def test_the_lab_example_pins_real_digests_and_the_schema_requires_one() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    for image in ("serviceImage", "workerImage"):
        assert LAB[image]["tag"] == chart["appVersion"]
        assert DIGEST.match(LAB[image]["digest"])
        assert _errors(SCHEMA, _with(LAB, f"{image}.digest", "")) != []
        assert _errors(SCHEMA, _with(VALUES, f"{image}.tag", "0.12.0")) != []
    assert LAB["serviceImage"]["digest"] != LAB["workerImage"]["digest"]
    assert (LAB["nameOverride"], LAB["namespaceOverride"]) == ("hades", "hades")
    assert LAB["buildkit"]["enabled"] is False


def test_the_chart_app_version_is_current() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["appVersion"] == "0.12.0"


def test_the_migrate_job_explains_its_hook_policy_under_argo() -> None:
    template = (TEMPLATES / "migrate-job.yaml").read_text()
    header = template.split("---\n", 1)[0]
    assert "`.Release.Revision` is always 1" in header
    assert "BeforeHookCreation" in header
    assert "argocd.argoproj.io/hook-delete-policy: BeforeHookCreation" in template


def _role_verbs(text: str) -> dict[str, set[str]]:
    role = next(
        doc
        for doc in yaml.safe_load_all(re.sub(r"\{\{[^}]*\}\}", "x", text))
        if doc and doc["kind"] == "Role" and doc["metadata"]["name"].endswith("-supervisor")
    )
    verbs: dict[str, set[str]] = {}
    for rule in role["rules"]:
        for resource in rule["resources"]:
            verbs.setdefault(resource, set()).update(rule["verbs"])
    return verbs


def test_every_kind_the_provider_patches_is_patchable_in_the_workers_role() -> None:
    source = "".join(path.read_text() for path in (ROOT / "crucible").rglob("*.py"))
    patched = set(re.findall(r'client\.patch\s*[(,]\s*"([a-z/]+)"', source))
    assert "networkpolicies" in patched
    for role in (
        ROOT / "deploy/kubernetes/base/workers/role.yaml",
        TEMPLATES / "rbac.yaml",
    ):
        verbs = _role_verbs(role.read_text())
        assert "patch" in verbs["networkpolicies"], role
        for kind in patched & set(verbs):
            assert "patch" in verbs[kind], (role, kind)


def test_deployment_md_documents_the_chart() -> None:
    text = (ROOT / "docs/deployment.md").read_text()
    section = text.split("## The Helm chart", 1)[1].split("\n## ", 1)[0]
    assert "oci://ghcr.io/sentania-labs/charts/hades" in section
    for value in (
        "nameOverride",
        "namespaceOverride",
        "workersNamespaceOverride",
        "extraSettings",
        "buildkit.enabled",
        "cluster.dnsIp",
        "provider.",
        "values-lab-example.yaml",
        "chart-sync-check",
    ):
        assert value in section, value


def test_no_em_dash_in_the_chart_tools_or_docs() -> None:
    paths = [*CHART.rglob("*"), *(ROOT / "tools/chart").glob("*"), ROOT / "docs/deployment.md"]
    assert not [path for path in paths if path.is_file() and chr(0x2014) in path.read_text()]


def test_the_sync_check_renders_base_values_and_the_defaults() -> None:
    script = (ROOT / "tools/chart/sync.sh").read_text()
    assert "-f tools/chart/values-base.yaml" in script
    assert "--without-namespace hades-buildkit" in script
    assert (BASE_VALUES["nameOverride"], BASE_VALUES["namespaceOverride"]) == ("hades",) * 2
    assert BASE_VALUES["workersNamespaceOverride"] == "hades-workers"
    assert BASE_VALUES["buildkit"]["namespace"] == "hades-buildkit"
    found: dict[tuple[str, str, str], dict[str, Any]] = {
        ("Namespace", "", "hades-buildkit"): {},
        ("Deployment", "hades-buildkit", "hades-buildkit"): {},
        ("Deployment", "hades", "hades-api"): {},
    }
    assert set(without_namespaces(found, {"hades-buildkit"})) == {
        ("Deployment", "hades", "hades-api")
    }


def _render(name: str, namespace: str, workers: str, *, stale: bool = False) -> list[Any]:
    owner = "crucible" if stale else name
    secrets = {h: f"{owner}-harness-{h.replace('_', '-')}" for h in ("claude_code", "codex")}
    secrets |= {h: f"{owner}-harness-{h}" for h in ("agy", "hermes")}
    pod = {
        "serviceAccountName": f"{name}-supervisor",
        "containers": [{"envFrom": [{"configMapRef": {"name": f"{name}-settings"}}]}],
    }
    docs: list[Any] = [
        {"kind": "Namespace", "metadata": {"name": namespace}},
        {"kind": "Namespace", "metadata": {"name": workers}},
        {
            "kind": "ConfigMap",
            "metadata": {"name": f"{name}-settings", "namespace": namespace},
            "data": {
                "CRUCIBLE_KUBERNETES__NAMESPACE": namespace,
                "CRUCIBLE_KUBERNETES__WORKERS_NAMESPACE": workers,
                "CRUCIBLE_KUBERNETES__SERVICE_ACCOUNT": f"{owner}-worker",
                "CRUCIBLE_KUBERNETES__CACHE_CLAIM": f"{name}-reference-cache",
                "CRUCIBLE_KUBERNETES__BUILDKIT_NAMESPACE": "hades-buildkit",
                "CRUCIBLE_ROOMS__API_NAMESPACE": namespace,
                "CRUCIBLE_KUBERNETES__CREDENTIAL_SECRETS": json.dumps(secrets),
            },
        },
        {"kind": "ServiceAccount", "metadata": {"name": f"{name}-worker", "namespace": workers}},
        {
            "kind": "ServiceAccount",
            "metadata": {"name": f"{name}-supervisor", "namespace": namespace},
        },
        {
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"{name}-reference-cache", "namespace": workers},
        },
        {
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"{name}-artifacts", "namespace": namespace},
        },
        {"kind": "Role", "metadata": {"name": f"{name}-supervisor", "namespace": workers}},
        {
            "kind": "RoleBinding",
            "metadata": {"name": f"{name}-supervisor", "namespace": workers},
            "subjects": [
                {"kind": "ServiceAccount", "name": f"{name}-supervisor", "namespace": namespace}
            ],
        },
        {"kind": "ResourceQuota", "metadata": {"name": f"{name}-workers", "namespace": workers}},
    ]
    for component in ("api", "supervisor"):
        docs.append(
            {
                "kind": "Deployment",
                "metadata": {"name": f"{name}-{component}", "namespace": namespace},
                "spec": {"template": {"spec": pod}},
            }
        )
    return docs


def _check(docs: list[Any], **extra: Any) -> list[str]:
    return problems(
        docs, name="hades", namespace="hades", workers_namespace="hades-workers", **extra
    )


def test_the_flow_check_passes_a_followed_render_and_names_a_stale_setting() -> None:
    assert _check(_render("hades", "hades", "hades-workers"), settings={}) == []
    stale = _check(_render("hades", "hades", "hades-workers", stale=True), settings={})
    assert "setting CRUCIBLE_KUBERNETES__SERVICE_ACCOUNT is 'crucible-worker', expected " in (
        "".join(stale)
    )
    assert any("credential Secret for hermes" in problem for problem in stale)
    assert _check(
        _render("hades", "hades", "hades-workers"),
        settings={"CRUCIBLE_KUBERNETES__CLUSTER_DNS_IP": "10.43.0.10"},
        buildkit=True,
    ) == [
        "buildkit is on but nothing renders for it",
        "setting CRUCIBLE_KUBERNETES__CLUSTER_DNS_IP is None, expected '10.43.0.10'",
    ]
