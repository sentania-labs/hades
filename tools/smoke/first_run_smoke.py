#!/usr/bin/env python3
"""The first-run path on a disposable kind cluster (crucible#119, #120, #121, #123, #79).

`make first-run-kind` runs this after `tools/kind/deploy-kind.sh` has brought the
deployment manifests up from nothing. It does what an operator does on a fresh deploy,
through the deployed API and the rendered UI, against stand-ins for the two outside
services (tools/smoke/first_run_stubs.py, run as a Pod in `crucible-stubs`):

1. Status names the real missing steps for Hermes and lists no test fixture;
2. the gateway URL and key are set in one step and tested, in plain words;
3. a model is picked from the gateway's own list, which writes a routing version;
4. the Kubernetes egress selectors name the in-cluster gateway, and the combined worker
   image is promoted;
5. the GitHub App is created with one click (crucible#168): the GitHub page's Create
   GitHub App posts the manifest to the GitHub stand-in, whose confirm button redirects
   the browser back with a code; the service checks the state, exchanges the code once,
   and creates `hades-github-app` in `hades`, and the mounted copy appears in the
   api Pod readable by its user (the #79 fsGroup question); the page's Install button
   installs the App and GitHub sends the browser back to the picker;
6. a repository is picked from the stand-in installation;
7. Status reaches "ready" for Hermes, in the document and on the rendered page;
8. a private repository is picked (crucible#157, ADR 0019) from a git stand-in that
   answers a clone only with a read-only token the GitHub stand-in minted for it, and a
   script-harness task on it is prepared: the refresher and the preparer clone with the
   token, the token Secret is gone before the worker runs, the worker's Pod never
   references it, nothing the worker can read holds it, and GitHub was asked to revoke
   it.

Nothing here calls the real GitHub API or a real model. The App's key is made by the
GitHub stand-in in its own Pod and handed to the service once, in the conversion's
answer; this proof never sees it. The browser's side of the flow runs over a second
port forward, to the stand-in, with the stand-in's in-cluster URL rewritten to it. The git
stand-in's certificate is signed by the run's own CA (`CRUCIBLE_DEPLOY_KIND_CA_DIR`),
which deploy-kind.sh also adds to the script-harness image it pushes for this run, so
the preparer verifies the stand-in the way it verifies GitHub.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import html
import http.cookiejar
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HERE = Path(__file__).resolve().parent
STUB_NAMESPACE = "crucible-stubs"
STUB_URL = f"http://crucible-stubs.{STUB_NAMESPACE}.svc.cluster.local:8080"
APP_ID = 4242
APP_NAME = "Hades-kind"
APP_SLUG = "hades-kind"
MODELS = ["kind-fast", "kind-large"]
REPOSITORY = "octo-lab/widgets"
PRIVATE_REPOSITORY = "octo-lab/secret-plans"
# The git stand-in: a headless Service, so the name resolves to the Pod's own address,
# which is what a NetworkPolicy's address rule can match (26, crucible#91).
GIT_HOST = f"crucible-git.{STUB_NAMESPACE}.svc.cluster.local"
GIT_PORT = 8443
PRIVATE_URL = f"https://{GIT_HOST}:{GIT_PORT}/{PRIVATE_REPOSITORY}"


def _smoke_module() -> Any:
    """The deploy smoke's port forward, request, kubectl and token helpers, reused."""
    spec = importlib.util.spec_from_file_location("kubernetes_smoke", HERE / "kubernetes_smoke.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kubernetes_smoke"] = module
    spec.loader.exec_module(module)
    return module


KS = _smoke_module()
log = KS.log
request = KS.request
kubectl = KS.kubectl
SmokeError = KS.SmokeError


def step(title: str) -> None:
    log(f"\n=== {title}")


def show(label: str, value: Any) -> None:
    log(f"{label}: {json.dumps(value, indent=2, sort_keys=True)}")


def git_tls(ca_dir: Path) -> tuple[str, str]:
    """A one-day serving certificate for the git stand-in, signed by the run's CA."""
    ca_key = serialization.load_pem_private_key((ca_dir / "ca.key").read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate((ca_dir / "ca.crt").read_bytes())
    assert isinstance(ca_key, rsa.RSAPrivateKey)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, GIT_HOST)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(GIT_HOST)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode(),
    )


def git_container(git_image: str) -> dict[str, Any]:
    """The git stand-in, from the worker image (git and Python), beside the GitHub half
    it asks whether a token may clone."""
    return {
        "name": "git",
        "image": git_image,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "python3",
            "/stubs/first_run_stubs.py",
            "--config",
            "/stubs/config.json",
            "--git-root",
            "/srv/git",
            "--port",
            str(GIT_PORT),
            "--tls-cert",
            "/tls/tls.crt",
            "--tls-key",
            "/tls/tls.key",
            "--auth-url",
            "http://127.0.0.1:8080",
        ],
        "ports": [{"containerPort": GIT_PORT}],
        "readinessProbe": {"tcpSocket": {"port": GIT_PORT}, "periodSeconds": 2},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "stubs", "mountPath": "/stubs"},
            {"name": "tls", "mountPath": "/tls", "readOnly": True},
            {"name": "git", "mountPath": "/srv/git"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {
            "requests": {"cpu": "20m", "memory": "64Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"},
        },
    }


def deploy_stubs(
    image: str,
    config: dict[str, Any],
    *,
    git_image: str | None = None,
    tls: tuple[str, str] | None = None,
) -> None:
    """The stand-ins as one Pod behind one Service, in their own namespace so the workers'
    egress selector can name them (a selector may not name `hades`). With `git_image`
    the Pod also runs the git stand-in, behind a headless Service of its own."""
    script = (HERE / "first_run_stubs.py").read_text(encoding="utf-8")
    objects = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": STUB_NAMESPACE}},
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "crucible-stubs", "namespace": STUB_NAMESPACE},
            "data": {"first_run_stubs.py": script, "config.json": json.dumps(config)},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "crucible-stubs", "namespace": STUB_NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "crucible-stubs"}},
                "template": {
                    "metadata": {"labels": {"app": "crucible-stubs"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "stubs",
                                "image": image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python",
                                    "/stubs/first_run_stubs.py",
                                    "--config",
                                    "/stubs/config.json",
                                    "--port",
                                    "8080",
                                ],
                                "ports": [{"containerPort": 8080}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/health/readiness", "port": 8080},
                                    "periodSeconds": 2,
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [{"name": "stubs", "mountPath": "/stubs"}],
                                "resources": {
                                    "requests": {"cpu": "20m", "memory": "64Mi"},
                                    "limits": {"cpu": "500m", "memory": "256Mi"},
                                },
                            }
                        ],
                        "volumes": [{"name": "stubs", "configMap": {"name": "crucible-stubs"}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "crucible-stubs", "namespace": STUB_NAMESPACE},
            "spec": {
                "selector": {"app": "crucible-stubs"},
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        },
    ]
    if git_image and tls:
        deployment: Any = next(o for o in objects if o["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        pod["containers"].append(git_container(git_image))
        pod["volumes"].extend(
            [
                {"name": "tls", "secret": {"secretName": "crucible-git-tls"}},
                {"name": "git", "emptyDir": {}},
                {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
            ]
        )
        objects.insert(
            1,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "kubernetes.io/tls",
                "metadata": {"name": "crucible-git-tls", "namespace": STUB_NAMESPACE},
                "stringData": {"tls.crt": tls[0], "tls.key": tls[1]},
            },
        )
        objects.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "crucible-git", "namespace": STUB_NAMESPACE},
                "spec": {
                    "clusterIP": "None",
                    "selector": {"app": "crucible-stubs"},
                    "ports": [{"port": GIT_PORT, "targetPort": GIT_PORT}],
                },
            }
        )
    # The manifest may carry the git stand-in's throwaway key: a private file in a
    # private directory, removed as soon as kubectl has read it.
    with tempfile.TemporaryDirectory(prefix="crucible-stubs-") as scratch:
        manifest = Path(scratch) / "stubs.json"
        manifest.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))
        manifest.chmod(0o600)
        kubectl(["apply", "-f", str(manifest)])
    kubectl(
        ["-n", STUB_NAMESPACE, "rollout", "status", "deployment/crucible-stubs", "--timeout=180s"]
    )
    log(f"the stand-ins answer at {STUB_URL}")


def readiness(base_url: str, token: str) -> dict[str, Any]:
    document = request("GET", f"{base_url}/v1/admin/status", token=token)
    return dict(document["readiness"])


def hermes_of(document: dict[str, Any]) -> dict[str, Any]:
    return next(h for h in document["harnesses"] if h["name"] == "hermes")


def gateway(base_url: str, token: str, key: str) -> None:
    step("2. set the gateway URL and key in one step, and test both (#119)")
    endpoint = f"{STUB_URL}/v1"
    result = request(
        "POST",
        f"{base_url}/v1/admin/gateway",
        token=token,
        body={"reason": "first-run kind proof", "endpoint_url": endpoint, "api_key": key},
    )
    show("test", result["test"])
    expected = f"Gateway {endpoint} reachable, key accepted, {len(MODELS)} models."
    if result["test"]["summary"] != expected or not result["test"]["passed"]:
        raise SmokeError(f"the gateway test said {result['test']!r}, not {expected!r}")
    if key in json.dumps(result):
        raise SmokeError("the gateway answer carried the key")

    step("3. pick a model from what the key can see (#121)")
    listing = request("GET", f"{base_url}/v1/admin/gateway/models", token=token)
    show(
        "models",
        [{k: r[k] for k in ("id", "offered", "in_policy", "note")} for r in listing["models"]],
    )
    offered = [row["id"] for row in listing["models"] if row["offered"]]
    if offered != MODELS:
        raise SmokeError(f"the gateway listed {offered}, not {MODELS}")
    saved = request(
        "POST",
        f"{base_url}/v1/admin/gateway/models",
        token=token,
        body={
            "reason": "first-run kind proof",
            "models": [{"id": MODELS[0], "enabled": True, "enable_thinking": False}],
        },
    )
    show("saved", {k: saved[k] for k in ("routing_policy", "enabled", "added")})


def egress_and_image(base_url: str, token: str) -> None:
    step("4. name the in-cluster gateway for the workers' egress, promote the worker image")
    request(
        "POST",
        f"{base_url}/v1/admin/kubernetes/egress",
        token=token,
        body={
            "reason": "first-run kind proof: the gateway runs in the cluster",
            "dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
            "local_endpoint": {
                "namespace": STUB_NAMESPACE,
                "pod_labels": {"app": "crucible-stubs"},
                "port": 8080,
            },
        },
    )
    deadline = time.monotonic() + 180
    while True:
        listing = request("GET", f"{base_url}/v1/admin/images", token=token)
        items = [i for i in listing["items"] if "hermes" in (i.get("harnesses") or {})]
        if items:
            break
        if time.monotonic() > deadline:
            raise SmokeError(f"no image carrying hermes is visible: {json.dumps(listing)[:800]}")
        time.sleep(3)
    image = items[0]
    request(
        "POST",
        f"{base_url}/v1/admin/images/{image['digest']}/promote",
        token=token,
        # Promotion is per harness (ADR 0018): this names Hermes, the harness under proof.
        body={"harness": "hermes", "reason": "first-run kind proof: the Hermes worker image"},
    )
    log(f"promoted {image['reference']} ({image['digest']}) for hermes")


class StubForward:
    """`kubectl port-forward` to the GitHub stand-in: the browser's half of the manifest
    flow talks to github.com, which here is the stand-in's Service."""

    def __init__(self) -> None:
        self.port = KS.free_port()
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def browser(self, url: str) -> str:
        """An in-cluster stand-in URL as the browser reaches it."""
        if not url.startswith(STUB_URL):
            raise SmokeError(f"{url!r} is not on the GitHub stand-in")
        return self.url + url[len(STUB_URL) :]

    def __enter__(self) -> StubForward:
        self.process = subprocess.Popen(
            [
                "kubectl",
                "-n",
                STUB_NAMESPACE,
                "port-forward",
                "service/crucible-stubs",
                f"{self.port}:8080",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/health/readiness", timeout=5):
                    return self
            except OSError:
                time.sleep(1)
        raise SmokeError("the GitHub stand-in's port forward never answered")

    def __exit__(self, *_: object) -> None:
        if self.process is not None:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)


class _Stay(urllib.request.HTTPRedirectHandler):
    """Report a redirect instead of following it, as a test of where it points."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def browse(
    opener: Any, method: str, url: str, form: dict[str, str] | None = None, **headers: str
) -> tuple[int, str, str]:
    """One browser request, redirects not followed: (status, Location, body)."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=180) as response:
            return response.status, "", response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", ""), exc.read().decode("utf-8", "replace")


def ui_opener(jar: http.cookiejar.CookieJar | None = None) -> Any:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _Stay())


def github(base_url: str, token: str, jar: http.cookiejar.CookieJar) -> None:
    step("5. create the GitHub App with one click; the service keeps its key (#168, #79)")
    before = request("GET", f"{base_url}/v1/admin/github", token=token)
    show("before", {k: before[k] for k in ("configured", "key_present", "stored_in")})
    browser = ui_opener(jar)
    web = ui_opener()
    _, _, page = browse(browser, "GET", f"{base_url}/ui/github")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page)
    if csrf is None or "Create GitHub App" not in page:
        raise SmokeError("the GitHub page offers no Create GitHub App button")
    if 'name="private_key"' in page or "existing=1" in page:
        raise SmokeError("the GitHub page still offers to paste an existing App's id and key")
    status, _, started = browse(
        browser,
        "POST",
        f"{base_url}/ui/actions/github-create-app",
        {"csrf": csrf.group(1), "return_to": "/ui/github", "app_name": APP_NAME},
        Origin=base_url,
    )
    action = re.search(r'id="github-manifest"[^>]*action="([^"]+)"', started)
    posted = re.search(r'name="manifest" value="([^"]+)"', started)
    if status != 200 or action is None or posted is None:
        raise SmokeError(f"Create GitHub App answered {status} without the GitHub form")
    target = html.unescape(action.group(1))
    manifest = json.loads(html.unescape(posted.group(1)))
    show("manifest", manifest)
    wanted = {
        "metadata": "read",
        "contents": "write",
        "pull_requests": "write",
        "checks": "read",
        "actions": "read",
        "issues": "read",
    }
    if (
        manifest["default_permissions"] != wanted
        or manifest["default_events"] != []
        or manifest["hook_attributes"]["active"] is not False
        or manifest["redirect_url"] != f"{base_url}/ui/github/callback"
        or manifest["public"] is not False
    ):
        raise SmokeError("the manifest is not the one spec 23 and #168 describe")
    if not target.startswith(f"{STUB_URL}/settings/apps/new?state="):
        raise SmokeError(f"the manifest goes to {target!r}")

    with StubForward() as stub:
        # github.com's side: the confirm page, and its redirect back to Crucible.
        status, _, confirm = browse(
            web, "POST", stub.browser(target), {"manifest": json.dumps(manifest)}
        )
        pending = re.search(r"name='pending' value='([^']+)'", confirm)
        if status != 200 or pending is None:
            raise SmokeError(f"the stand-in's create page answered {status}")
        status, back, _ = browse(
            web, "POST", f"{stub.url}/_stub/apps/confirm", {"pending": pending.group(1)}
        )
        if status != 302 or not back.startswith(f"{base_url}/ui/github/callback?code="):
            raise SmokeError(f"GitHub's redirect back is {status} to {back!r}")
        log(f"GitHub sent the browser back to {back.split('?')[0]}?code=...&state=...")

        # The redirect is cross-site, so the Strict session cookie does not come with it:
        # the first arrival reloads itself from Crucible's own site.
        status, _, hop = browse(ui_opener(), "GET", back)
        if status != 200 or 'http-equiv="refresh"' not in hop:
            raise SmokeError(f"a cookieless return answered {status} without the reload page")
        if stub_json("/_stub/manifests")["conversions"]:
            raise SmokeError("the code was exchanged for a browser that is not signed in")

        status, landed, _ = browse(browser, "GET", back)
        landed = urllib.parse.unquote(landed)
        log(f"the callback: HTTP {status}, then {landed}")
        if status != 303 or "kind=ok" not in landed or f"(App {APP_ID})" not in landed:
            raise SmokeError(f"the callback did not connect the App: {landed!r}")
        status, again, _ = browse(browser, "GET", back)
        if "used already" not in urllib.parse.unquote(again):
            raise SmokeError(f"a second return with the same state was not refused: {again!r}")
        conversions = stub_json("/_stub/manifests")["conversions"]
        show("the stand-in's conversions (never a key)", conversions)
        if len(conversions) != 1:
            raise SmokeError(f"the code was exchanged {len(conversions)} times")

        secret = json.loads(
            kubectl(
                ["-n", "hades", "get", "secret", "hades-github-app", "-o", "json"],
                redact=True,
            )
        )
        labels = secret["metadata"].get("labels") or {}
        show(
            "the Secret (keys and labels only)",
            {"keys": sorted(secret.get("data") or {}), "labels": labels},
        )
        if labels.get("app.kubernetes.io/managed-by") != "crucible":
            raise SmokeError("hades-github-app is not labelled as the service's own")
        stored_id = base64.b64decode(secret["data"]["app-id"]).decode()
        if stored_id != str(APP_ID) or "app.pem" not in secret["data"]:
            raise SmokeError(f"the Secret holds App id {stored_id}")

        # Install: the page's button opens the App's page; GitHub sends the browser back
        # to the picker.
        _, _, page = browse(browser, "GET", f"{base_url}/ui/github")
        install = f"{STUB_URL}/apps/{APP_SLUG}/installations/new"
        if f'href="{install}"' not in page or "Install on GitHub" not in page:
            raise SmokeError("the GitHub page has no Install button for the new App")
        status, setup, _ = browse(web, "POST", stub.browser(install))
        if status != 302 or not setup.startswith(f"{base_url}/ui/github/installed?"):
            raise SmokeError(f"the install sent the browser to {setup!r}")
        status, landed, _ = browse(browser, "GET", setup)
        if "Installed on GitHub" not in urllib.parse.unquote(landed):
            raise SmokeError(f"the install's return landed on {landed!r}")
        log("installed; GitHub sent the browser back to the repository picker")
    audit = json.dumps(request("GET", f"{base_url}/v1/admin/audit?limit=200", token=token))
    if "PRIVATE KEY" in audit or "github_app_manifest_started" not in audit:
        raise SmokeError("the audit lacks the start, or carries a key")

    # #79: the mounted copy, as the api's own user sees it. The kubelet projects a new
    # Secret into an optional volume on its next sync, so this waits for it.
    probe = (
        "import os, stat; p = '/var/lib/crucible/credentials/github/app.pem'; "
        "s = os.stat(p); print(oct(stat.S_IMODE(s.st_mode)), os.getuid(), s.st_uid, "
        "s.st_gid, os.access(p, os.R_OK), len(open(p, 'rb').read()) > 0)"
    )
    deadline = time.monotonic() + 180
    while True:
        seen = kubectl(
            ["-n", "hades", "exec", "deployment/hades-api", "--", "python", "-c", probe],
            check=False,
        ).strip()
        if seen.endswith("True True"):
            log(
                "api Pod, mounted app.pem (mode, process uid, file uid, gid, readable, "
                f"non-empty): {seen}"
            )
            break
        if time.monotonic() > deadline:
            raise SmokeError(f"the mounted app.pem never became readable in the api Pod: {seen!r}")
        time.sleep(5)

    step("6. pick a repository from the installation (#120)")
    picker = request("GET", f"{base_url}/v1/admin/github/installations", token=token)
    show(
        "installations",
        [
            {
                "account": i["account"],
                "id": i["id"],
                "repositories": [r["full_name"] for r in i["repositories"]],
            }
            for i in picker["installations"]
        ],
    )
    installation = picker["installations"][0]
    registered = request(
        "POST",
        f"{base_url}/v1/admin/github/repositories",
        token=token,
        body={
            "reason": "first-run kind proof",
            "installation_id": installation["id"],
            "repository": REPOSITORY,
            "attested_all_prs": True,
        },
    )
    show("registered", registered)
    if (
        registered["default_branch"] != "trunk"
        or registered["installation_id"] != installation["id"]
    ):
        raise SmokeError("the registration did not take GitHub's default branch and installation")
    # ADR 0019: the private repository is offered like any other; step 8 registers it.
    private = next(
        (r for r in installation["repositories"] if r["full_name"] == PRIVATE_REPOSITORY), None
    )
    if private is None or private.get("private") is not True or private.get("unsupported"):
        raise SmokeError(f"the picker does not offer {PRIVATE_REPOSITORY} as private: {private}")


def stub_json(path: str) -> Any:
    """A GET on the GitHub stand-in from inside its own Pod: the smoke's port forward
    reaches Crucible only."""
    code = (
        "import json, urllib.request; "
        f"print(urllib.request.urlopen('http://127.0.0.1:8080{path}', timeout=10).read().decode())"
    )
    out = kubectl(
        [
            "-n",
            STUB_NAMESPACE,
            "exec",
            "deployment/crucible-stubs",
            "-c",
            "stubs",
            "--",
            "python",
            "-c",
            code,
        ]
    )
    return json.loads(out)


def scoped_mints() -> list[dict[str, Any]]:
    return [m for m in stub_json("/_stub/minted") if m.get("repositories")]


def private_checkout(base_url: str, admin: str) -> None:
    step("8. a private repository, prepared with a read-only token the worker never sees (#157)")
    request("PUT", f"{base_url}/v1/routing/{KS.ROUTING}/1", token=admin, body=KS.routing_document())
    request(
        "PUT",
        f"{base_url}/v1/policies/{KS.POLICY}/1",
        token=admin,
        body=KS.policy_document(KS.shipped_policy(base_url, admin)),
    )
    KS.promote_worker_image(base_url, admin)
    # Only repository-scoped mints: the picker's own listing mints metadata-only tokens.
    before = len(scoped_mints())
    registered = request(
        "POST",
        f"{base_url}/v1/admin/github/repositories",
        token=admin,
        body={
            "reason": "first-run kind proof: a private repository (ADR 0019)",
            "installation_id": 77,
            "repository": PRIVATE_REPOSITORY,
            "policy_name": KS.POLICY,
            "attested_all_prs": True,
        },
    )
    show("registered", registered)
    if registered.get("private") is not True or registered.get("url") != PRIVATE_URL:
        raise SmokeError("the private repository did not register as private at its git URL")
    check = scoped_mints()[before:]
    show(
        "registration's token (scope only)",
        [{k: m[k] for k in ("repositories", "permissions", "revoked")} for m in check],
    )
    if [(m["repositories"], m["permissions"], m["revoked"]) for m in check] != [
        (["secret-plans"], {"contents": "read"}, True)
    ]:
        raise SmokeError("registration did not mint one read-only token and revoke it")

    operator = KS.mint_token(f"first-run-operator-{int(time.time())}", "operator")
    external_id = f"FIRST-RUN-PRIVATE-{int(time.time())}"
    contract = KS.task_contract(external_id)
    contract["title"] = "First-run kind proof: a private repository"
    contract["project"] = "secret-plans"
    contract["repository"].update({"name": "secret-plans", "base_ref": "main"})
    contract["execution_request"]["rationale"] = "first-run kind proof, private checkout"
    task = request("POST", f"{base_url}/v1/tasks", token=operator, body=contract)
    task_id = str(task["id"])
    request(
        "POST",
        f"{base_url}/v1/tasks/{task_id}/start",
        token=operator,
        body={"provider": "kubernetes", "policy_version": 1},
    )
    log(f"submitted and started task {task_id} on {PRIVATE_REPOSITORY}")

    # The repository's `e2e-behavior` is `hang`: the worker runs until it is cancelled,
    # which leaves time to look inside it.
    deadline = time.monotonic() + 600
    attempt_id = ""
    worker: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = request("GET", f"{base_url}/v1/tasks/{task_id}", token=operator)
        if view["state"] in KS.DEAD_END_STATES:
            trail = KS.task_events(base_url, operator, task_id)
            raise SmokeError(f"the private task reached {view['state']}:\n{trail}")
        attempt_id = str((view.get("latest_attempt") or {}).get("id") or "")
        if attempt_id:
            pods = json.loads(
                kubectl(
                    [
                        "-n",
                        KS.WORKERS_NAMESPACE,
                        "get",
                        "pods",
                        "-l",
                        f"crucible.attempt={attempt_id},crucible.role=worker",
                        "-o",
                        "json",
                    ]
                )
            )["items"]
            running = [p for p in pods if (p.get("status") or {}).get("phase") == "Running"]
            if running:
                worker = running[0]
                break
        time.sleep(3)
    else:
        raise SmokeError(
            "the private task's worker never ran:\n" + KS.task_events(base_url, operator, task_id)
        )
    log(f"attempt {attempt_id}: the worker Pod {worker['metadata']['name']} is running")

    events = KS.task_events(base_url, operator, task_id)
    git_log = kubectl(["-n", STUB_NAMESPACE, "logs", "deployment/crucible-stubs", "-c", "git"])
    served = [line for line in git_log.splitlines() if "secret-plans" in line]
    log("git stand-in, what it answered for the private repository:")
    for line in served:
        log(f"  {line}")
    if not any(" 401 " in f" {line} " for line in served):
        raise SmokeError("the git stand-in never refused an anonymous request")
    if not any("200 POST git-upload-pack" in line for line in served):
        raise SmokeError("the git stand-in never served the clone with a scoped token")

    minted = stub_json("/_stub/minted")
    prepared = scoped_mints()[before + 1 :]
    show(
        "prepare's token (scope only)",
        [{k: m[k] for k in ("repositories", "permissions", "revoked")} for m in prepared],
    )
    if not prepared or any(
        (m["repositories"], m["permissions"]) != (["secret-plans"], {"contents": "read"})
        for m in prepared
    ):
        raise SmokeError("prepare did not use a token scoped to the one repository, read-only")
    if not all(m["revoked"] for m in prepared):
        raise SmokeError("a prepare's token was not revoked when the step ended")

    secret_name = f"checkout-{attempt_id.lower()}"
    leftover = kubectl(
        [
            "-n",
            KS.WORKERS_NAMESPACE,
            "get",
            "secret",
            secret_name,
            "--ignore-not-found",
            "-o",
            "name",
        ]
    ).strip()
    if leftover:
        raise SmokeError(f"{secret_name} still exists while the worker runs")
    log(f"{secret_name} is gone while the worker runs")
    worker_spec = json.dumps(worker["spec"])
    if "checkout-" in worker_spec or "/run/crucible-token" in worker_spec:
        raise SmokeError("the worker Pod references the checkout token")
    log("the worker Pod spec names no checkout Secret and no token mount")

    tokens = [m["token"] for m in minted]
    probe = (
        'found=""; test -e /run/crucible-token && found="$found mount"; '
        'for t in "$@"; do '
        'grep -rqsF "$t" /crucible /home/worker /tmp 2>/dev/null && found="$found file"; '
        'tr "\\0" "\\n" < /proc/1/environ | grep -qF "$t" && found="$found env"; '
        'env | grep -qF "$t" && found="$found env"; '
        'done; echo "found:${found:- nothing}"'
    )
    seen = kubectl(
        [
            "-n",
            KS.WORKERS_NAMESPACE,
            "exec",
            worker["metadata"]["name"],
            "--",
            "sh",
            "-c",
            probe,
            "probe",
            *tokens,
        ],
        redact=True,
    ).strip()
    log(f"inside the worker, a search for every token the stand-in minted: {seen}")
    if seen != "found: nothing":
        raise SmokeError(f"the worker can read a checkout token: {seen}")
    readme = kubectl(
        [
            "-n",
            KS.WORKERS_NAMESPACE,
            "exec",
            worker["metadata"]["name"],
            "--",
            "cat",
            "/crucible/repo/README.md",
        ]
    ).strip()
    log(f"the worker's checkout of the private repository reads: {readme!r}")

    request(
        "POST",
        f"{base_url}/v1/tasks/{task_id}/cancel",
        token=operator,
        body={
            "reason": "first-run kind proof: the private checkout is proved",
            "verbatim": "cancel the private checkout proof task",
            "decided_by": "first-run-kind-proof",
        },
    )
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        state = request("GET", f"{base_url}/v1/tasks/{task_id}", token=operator)["state"]
        if state == "cancelled":
            break
        time.sleep(3)
    else:
        raise SmokeError(f"the private task did not cancel:\n{events}")
    log("the private task is cancelled")


def sign_in(base_url: str, jar: http.cookiejar.CookieJar) -> Any:
    """The first-run administrator from the Secret the migrate Job wrote (ADR 0016), as the
    operator would; never printed. The deploy smoke's helper also proves the Job's log
    does not carry it, and the Secret is gone once the token has signed in."""
    first_run = KS.first_run_token()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{base_url}/ui/sign-in", timeout=60) as response:
        page = response.read().decode()
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page)
    assert csrf is not None
    body = urllib.parse.urlencode({"csrf": csrf.group(1), "token": first_run, "next": "/ui"})
    post = urllib.request.Request(
        f"{base_url}/ui/sign-in",
        data=body.encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(post, timeout=120):
        pass
    KS.await_first_run_secret_gone()
    return opener


def page_text(opener: Any, url: str) -> str:
    with opener.open(url, timeout=180) as response:
        html = response.read().decode("utf-8", "replace")
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def smoke(stub_image: str, git_image: str, ca_dir: Path) -> None:
    if not os.environ.get("KUBECONFIG"):
        raise SmokeError("KUBECONFIG must name the disposable cluster's kubeconfig")
    key = "vk_" + secrets.token_urlsafe(24)
    deploy_stubs(
        stub_image,
        {
            "gateway_key": key,
            "models": MODELS,
            "installations": [],
            "manifest": {
                "app_id": APP_ID,
                "owner": "sentania",
                "install": {
                    "id": 77,
                    "account": "octo-lab",
                    "type": "Organization",
                    "repositories": [
                        {"full_name": REPOSITORY, "default_branch": "trunk"},
                        {"full_name": "octo-lab/gadgets", "default_branch": "main"},
                        {
                            "full_name": PRIVATE_REPOSITORY,
                            "private": True,
                            "default_branch": "main",
                            "url": PRIVATE_URL,
                            "git": {
                                "files": {
                                    "README.md": "The secret plans, readable with a token.\n",
                                    "e2e-behavior": "hang\n",
                                }
                            },
                        },
                    ],
                },
            },
        },
        git_image=git_image,
        tls=git_tls(ca_dir),
    )
    with KS.PortForward() as base_url:
        admin = KS.mint_token(f"first-run-{int(time.time())}", "admin")
        KS.await_supervisor(base_url, admin)

        step("1. Status on an empty deploy (#123)")
        before = readiness(base_url, admin)
        show("readiness", before)
        names = [h["name"] for h in before["harnesses"]]
        if "script-harness" in names or "script-harness" in json.dumps(before["steps"]):
            raise SmokeError("the readiness list names the script harness, a test fixture")
        codes = [s["code"] for s in hermes_of(before)["steps"]]
        for wanted in ("credential_missing", "endpoint_not_configured", "no_promoted_image"):
            if wanted not in codes:
                raise SmokeError(f"Hermes's steps {codes} do not name {wanted}")

        gateway(base_url, admin, key)
        egress_and_image(base_url, admin)
        # The operator signs in to the UI now: the App is created from the GitHub page.
        jar = http.cookiejar.CookieJar()
        opener = sign_in(base_url, jar)
        github(base_url, admin, jar)

        step("7. Status reaches ready for Hermes (#123)")
        deadline = time.monotonic() + 240
        while True:
            after = readiness(base_url, admin)
            if after["ready"] and "hermes" in after["ready_harnesses"]:
                break
            if time.monotonic() > deadline:
                show("readiness", after)
                raise SmokeError("Status never reached ready for Hermes")
            time.sleep(5)
        show("readiness", after)
        provider = KS.provider_checks(base_url, admin)
        checks = provider.get("checks") or {}
        show(
            "kubernetes provider, the workers' view of the gateway",
            {
                "health": provider.get("health"),
                **{
                    k: checks.get(k)
                    for k in ("namespace_ready", "dns_resolves", "local_endpoint_reachable")
                },
            },
        )
        if checks.get("local_endpoint_reachable") is not True:
            raise SmokeError("the readiness canary did not reach the gateway from the workers")

        status_page = page_text(opener, f"{base_url}/ui")
        match = re.search(r"Status ((?:Ready for a task|Crucible cannot).{0,120})", status_page)
        log(f"rendered /ui: {match.group(0) if match else status_page[:200]}")
        if "Ready for a task on hermes" not in status_page or " ready " not in status_page:
            raise SmokeError("the rendered Status page does not read ready for Hermes")
        gateway_page = page_text(opener, f"{base_url}/ui/gateway")
        for wanted in (f"{STUB_URL}/v1", "the last test passed"):
            if wanted not in gateway_page:
                raise SmokeError(f"the rendered Local gateway page lacks {wanted!r}")
        if key in gateway_page:
            raise SmokeError("the rendered Local gateway page shows the key")
        github_page = page_text(opener, f"{base_url}/ui/github")
        for wanted in (
            "connected",
            f"{STUB_URL}/apps/{APP_SLUG}/installations/new",
            REPOSITORY,
        ):
            if wanted not in github_page:
                raise SmokeError(f"the rendered GitHub page lacks {wanted!r}")
        log("rendered /ui, /ui/gateway and /ui/github read as expected")
        private_checkout(base_url, admin)
        log("\nfirst-run kind proof passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stub-image",
        default=os.environ.get("CRUCIBLE_DEPLOY_KIND_STUB_IMAGE"),
        help="an image with python and cryptography the cluster has (the service image)",
    )
    parser.add_argument(
        "--git-image",
        default=os.environ.get("CRUCIBLE_DEPLOY_KIND_GIT_IMAGE"),
        help="an image with git and python3 the cluster can pull (the combined worker image)",
    )
    parser.add_argument(
        "--ca-dir",
        default=os.environ.get("CRUCIBLE_DEPLOY_KIND_CA_DIR"),
        help="the run's CA (ca.crt, ca.key), which signs the git stand-in's certificate",
    )
    args = parser.parse_args(argv)
    if not args.stub_image or not args.git_image or not args.ca_dir:
        print(
            "set --stub-image, --git-image and --ca-dir (or CRUCIBLE_DEPLOY_KIND_STUB_IMAGE, "
            "CRUCIBLE_DEPLOY_KIND_GIT_IMAGE and CRUCIBLE_DEPLOY_KIND_CA_DIR)",
            file=sys.stderr,
        )
        return 2
    try:
        smoke(args.stub_image, args.git_image, Path(args.ca_dir))
    except SmokeError as exc:
        print(f"first-run kind proof failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
