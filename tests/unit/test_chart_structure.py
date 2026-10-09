"""Structural contract for the Helm chart derived from the kustomize base."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
CHART = ROOT / "charts/hades"
VALUE_REFERENCE = re.compile(r"\.Values\.([A-Za-z0-9_.]+)")


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


def test_every_template_value_is_declared_in_schema() -> None:
    schema = json.loads((CHART / "values.schema.json").read_text())
    paths = [*CHART.glob("templates/*"), *CHART.glob("base/**/*.yaml")]
    referenced = {match for path in paths for match in VALUE_REFERENCE.findall(path.read_text())}
    assert referenced
    assert not {path for path in referenced if not _schema_has_path(schema, path)}


def test_every_kustomize_base_object_has_a_chart_template() -> None:
    source_files = [
        path
        for path in (ROOT / "deploy/kubernetes/base").glob("**/*.yaml")
        if path.name != "kustomization.yaml"
    ]
    missing = []
    for source in source_files:
        relative = source.relative_to(ROOT / "deploy/kubernetes/base")
        chart_source = CHART / "base" / relative
        assert chart_source.is_file()
        source_objects = _identities(source)
        # Names and kinds are deliberately literal even where fields are templated.
        chart_text = chart_source.read_text()
        for kind, name in source_objects:
            if f"kind: {kind}" not in chart_text or f"name: {name}" not in chart_text:
                missing.append((kind, name, str(relative)))
    assert not missing


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
