"""Structural contract for the Helm chart derived from the kustomize base."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tools.chart.sync import objects

ROOT = Path(__file__).parents[2]
CHART = ROOT / "charts/hades"
VALUE_REFERENCE = re.compile(r"\.Values\.([A-Za-z0-9_.]+)")
# The name helpers render the kustomize base's names when no override is set.
DEFAULT_NAMES = {
    '{{ include "hades.name" . }}': "hades",
    '{{ include "hades.namespace" . }}': "hades",
    '{{ include "hades.workersNamespace" . }}': "hades-workers",
    "{{ .Values.buildkit.namespace }}": "hades-buildkit",
}


def _schema_has_path(schema: dict[str, Any], dotted: str) -> bool:
    node = schema
    for part in dotted.split("."):
        properties = node.get("properties", {})
        if part not in properties:
            return False
        node = properties[part]
        reference = node.get("$ref")
        if reference:
            node = schema["$defs"][reference.rsplit("/", 1)[-1]]
    return True


def _identities(path: Path) -> set[tuple[str, str]]:
    result = set()
    for document in yaml.safe_load_all(path.read_text()):
        if document and document.get("kind") != "Kustomization":
            result.add((document["kind"], document["metadata"]["name"]))
    return result


def test_chart_metadata_values_and_schema_parse() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    schema = json.loads((CHART / "values.schema.json").read_text())
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "hades"
    assert isinstance(chart["appVersion"], str) and chart["appVersion"]
    assert set(values) == set(schema["required"])


def test_chart_and_deploy_examples_contain_no_credential_values() -> None:
    example_files = [
        *CHART.glob("**/*"),
        ROOT / "docs/deploy.md",
        Path(__file__),
    ]
    credential_prefixes = (
        "sk-" + "ant-",
        "gh" + "p_",
        "gh" + "s_",
        "github" + "_pat_",
        "AK" + "IA",
    )
    offenders = {
        str(path.relative_to(ROOT)): prefix
        for path in example_files
        if path.is_file()
        for prefix in credential_prefixes
        if prefix in path.read_text()
    }
    assert not offenders


def test_every_template_value_is_declared_in_schema() -> None:
    schema = json.loads((CHART / "values.schema.json").read_text())
    paths = list(CHART.glob("templates/*"))
    referenced = {match for path in paths for match in VALUE_REFERENCE.findall(path.read_text())}
    assert referenced
    assert not {path for path in referenced if not _schema_has_path(schema, path)}


def test_every_kustomize_base_object_has_a_chart_template() -> None:
    source_files = [
        path
        for path in (ROOT / "deploy/kubernetes/base").glob("**/*.yaml")
        if path.name != "kustomization.yaml"
    ]
    template_objects = set()
    for template in (CHART / "templates").glob("*.yaml"):
        for document in template.read_text().split("---\n"):
            kind = re.search(r"^kind: (\S+)$", document, re.M)
            name = re.search(r"^metadata:\n  name: (.+)$", document, re.M)
            if kind and name:
                normalized = name[1].replace("-{{ .Release.Revision }}", "")
                for helper, default in DEFAULT_NAMES.items():
                    normalized = normalized.replace(helper, default)
                template_objects.add((kind[1], normalized))
    source_objects = {identity for source in source_files for identity in _identities(source)}
    assert source_objects == template_objects


def test_component_layout_and_document_boundaries() -> None:
    expected = {
        "namespaces",
        "configmap",
        "serviceaccounts",
        "rbac",
        "pvcs",
        "postgres",
        "migrate-job",
        "api",
        "supervisor",
        "workers-quota",
        "workers-networkpolicy",
        "buildkit",
    }
    templates = list((CHART / "templates").glob("*.yaml"))
    assert {path.stem for path in templates} == expected
    assert (CHART / "templates/_helpers.tpl").is_file()
    assert not (CHART / "base").exists()
    for path in templates:
        text = path.read_text()
        assert ".Files." not in text and "tpl " not in text
        assert "-}}" not in text  # Right trimming can consume a separator's newline.
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("---"):
                assert line == "---"
                assert lines[index + 1].startswith("apiVersion:")
            if line.startswith("apiVersion:"):
                assert index > 0 and lines[index - 1] == "---"


def test_chart_sync_comparison_fails_on_injected_drift(tmp_path: Path) -> None:
    original = tmp_path / "original.yaml"
    drifted = tmp_path / "drifted.yaml"
    original.write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: example\n")
    drifted.write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: example\nspec:\n  type: NodePort\n"
    )
    result = subprocess.run(
        ["uv", "run", "python", "tools/chart/sync.py", str(original), str(drifted)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "drift in Service//example" in result.stdout


def test_chart_secret_settings_reach_application_and_mounts() -> None:
    config = (CHART / "templates/configmap.yaml").read_text()
    assert "CRUCIBLE_GITHUB__APP__SECRET_NAME: {{ .Values.secrets.githubApp | quote }}" in config
    assert (
        "CRUCIBLE_KUBERNETES__FIRST_RUN_SECRET_NAME: {{ .Values.secrets.firstRunToken | quote }}"
        in config
    )
    for component in ("api", "supervisor"):
        template = (CHART / f"templates/{component}.yaml").read_text()
        assert "projected:" in template
        for field, key in (
            ("githubAppPrivateKey", "app.pem"),
            ("githubAppWebhook", "webhook.secret"),
        ):
            assert f".Values.secrets.{field} | default .Values.secrets.githubApp" in template
            assert f"key: {key}\n                      path: {key}" in template


def test_migration_job_name_changes_with_release_revision() -> None:
    template = (CHART / "templates/migrate-job.yaml").read_text()
    assert '  name: {{ include "hades.name" . }}-migrate-{{ .Release.Revision }}\n' in template
    assert "helm.sh/hook" not in template  # Dependencies are ordinary release resources.


def test_migration_name_normalization_preserves_image_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.yaml"
    chart = tmp_path / "chart.yaml"
    job: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "hades-migrate", "namespace": "hades"},
        "spec": {"template": {"spec": {"containers": [{"name": "migrate", "image": "svc:v1"}]}}},
    }
    source.write_text(yaml.safe_dump(job))
    job["metadata"]["name"] = "hades-migrate-2"
    chart.write_text(yaml.safe_dump(job))
    assert objects(source) == objects(chart)
    chart.write_text(chart.read_text().replace("svc:v1", "svc:v2"))
    assert objects(source) != objects(chart)
