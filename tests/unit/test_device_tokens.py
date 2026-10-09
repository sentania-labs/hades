"""Device tokens (hades #576, U9): an administrator mints a named, long-lived token for a
device, lists it with its last use and never its secret, and revokes it; the token
authenticates /v1 like any bearer token and is exchanged once for a server-side UI
session (ADR 0030). Every token here is minted at runtime; none is written down."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext, Reader
from crucible.adapters.ui import session as ui
from crucible.application.admin import devices, tokens
from crucible.application.admin.audit import ADMIN_KINDS
from crucible.application.auth import DEVICE_PREFIX, _mint, authenticate, is_device, mint_token
from crucible.application.errors import ConflictError
from crucible.domain.entities import Device, Event, Principal, Role, UiSession
from crucible.domain.events import EventKind
from tests.fixtures import FakeClock

PHONE = "Mobile Safari on the operator phone"
DESKTOP = "Firefox on the desk"


class Store:
    """The rows every unit of work in one test shares."""

    def __init__(self) -> None:
        self.principals: dict[str, tuple[Principal, bytes, bytes]] = {}
        self.devices: dict[str, Device] = {}
        self.sessions: dict[str, UiSession] = {}
        self.events: list[Event] = []


class Principals:
    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, principal_id: str) -> Principal | None:
        row = self._store.principals.get(principal_id)
        return replace(row[0]) if row else None

    def get_by_name(self, name: str) -> Principal | None:
        return next(
            (replace(p) for p, _, _ in self._store.principals.values() if p.name == name), None
        )

    def add(self, principal: Principal, token_salt: bytes, token_hash: bytes) -> None:
        self._store.principals[principal.id] = (replace(principal), token_salt, token_hash)

    def credentials(self, principal_id: str) -> tuple[bytes, bytes] | None:
        row = self._store.principals.get(principal_id)
        if row is None or row[0].disabled_at is not None:
            return None
        return row[1], row[2]

    def rotate(self, principal_id: str, token_salt: bytes, token_hash: bytes) -> None:
        principal = self._store.principals[principal_id][0]
        self._store.principals[principal_id] = (principal, token_salt, token_hash)

    def rename(self, principal_id: str, name: str) -> bool:
        self._store.principals[principal_id][0].name = name
        return True

    def list_all(self) -> list[Principal]:
        return sorted((replace(p) for p, _, _ in self._store.principals.values()), key=_name)

    def disable(self, principal_id: str, at: datetime) -> bool:
        row = self._store.principals.get(principal_id)
        if row is None or row[0].disabled_at is not None:
            return False
        row[0].disabled_at = at
        return True


def _name(principal: Principal) -> str:
    return principal.name


class Devices:
    def __init__(self, store: Store) -> None:
        self._rows = store.devices

    def add(self, device: Device) -> None:
        self._rows[device.principal_id] = replace(device)

    def get(self, principal_id: str) -> Device | None:
        row = self._rows.get(principal_id)
        return replace(row) if row else None

    def get_by_name(self, name: str) -> Device | None:
        return next((replace(d) for d in self._rows.values() if d.name == name), None)

    def list_all(self) -> list[Device]:
        return [replace(d) for d in sorted(self._rows.values(), key=lambda d: d.name)]

    def record_use(self, principal_id: str, at: datetime, user_agent: str | None) -> None:
        self._rows[principal_id].last_used_at = at
        self._rows[principal_id].last_user_agent = user_agent

    def mark_exchanged(self, principal_id: str, at: datetime, user_agent: str | None) -> bool:
        row = self._rows[principal_id]
        if row.exchanged_at is not None or row.revoked_at is not None:
            return False
        row.exchanged_at = row.last_used_at = at
        row.last_user_agent = user_agent
        return True

    def revoke(self, principal_id: str, at: datetime, by: str) -> bool:
        row = self._rows[principal_id]
        if row.revoked_at is not None:
            return False
        row.revoked_at, row.revoked_by = at, by
        return True


class Sessions:
    def __init__(self, store: Store) -> None:
        self._rows = store.sessions

    def create(self, session: UiSession) -> None:
        self._rows[session.id] = session

    def get(self, session_id: str) -> UiSession | None:
        return self._rows.get(session_id)

    def delete(self, session_id: str) -> None:
        self._rows.pop(session_id, None)

    def delete_expired(self, now: datetime) -> int:
        return 0

    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        self._rows[session_id].last_seen_at = last_seen_at


class Events:
    def __init__(self, store: Store) -> None:
        self._rows = store.events

    def append(self, event: Event) -> Event:
        stored = replace(event, seq=len(self._rows) + 1)
        self._rows.append(stored)
        return stored


class Uow:
    def __init__(self, store: Store) -> None:
        self.principals = Principals(store)
        self.devices = Devices(store)
        self.ui_sessions = Sessions(store)
        self.events = Events(store)

    def __enter__(self) -> Uow:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def admin_token(store: Store, clock: FakeClock) -> str:
    return mint_token(Uow(store), clock, name="admin", role=Role.ADMIN).token  # type: ignore[arg-type]


@pytest.fixture
def client(store: Store, clock: FakeClock) -> Iterator[TestClient]:
    def factory() -> Uow:
        return Uow(store)

    ctx = AppContext(
        uow_factory=factory,  # type: ignore[arg-type]
        clock=clock,
        providers=[],
        database_url="",
        engine=None,  # type: ignore[arg-type]
        artifact_store=None,  # type: ignore[arg-type]
        admin=SimpleNamespace(clock=clock, uow_factory=factory),  # type: ignore[arg-type]
        ui_signing_key=b"unit-test-signing-key",
    )
    app = create_app(ctx)

    def whoami(principal: Reader) -> dict[str, str]:
        return {"name": principal.name, "role": principal.role.value}

    app.add_api_route("/v1/whoami", whoami, methods=["GET"])
    with TestClient(app, base_url="https://hades.test") as test_client:
        yield test_client


def bearer(token: str, agent: str = PHONE) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "User-Agent": agent}


def mint(client: TestClient, admin_token: str, **body: Any) -> dict[str, Any]:
    response = client.post("/v1/admin/devices", json=body, headers=bearer(admin_token, DESKTOP))
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def kinds(store: Store, kind: EventKind) -> list[Event]:
    return [event for event in store.events if event.kind == kind.value]


def test_mint_returns_the_token_once_and_the_list_never_shows_it(
    client: TestClient, admin_token: str, store: Store
) -> None:
    minted = mint(client, admin_token, name="ops phone", reason="the on-call phone")
    token = minted["token"]
    assert minted["name"] == "ops phone"
    assert minted["role"] == "operator"
    assert minted["principal"] == f"{DEVICE_PREFIX}ops phone"
    assert minted["state"] == "active"
    assert minted["last_used_at"] is None
    assert minted["created_local"].endswith(("CDT", "CST"))

    listed = client.get("/v1/admin/devices", headers=bearer(admin_token, DESKTOP))
    assert listed.status_code == 200
    assert token not in listed.text
    (item,) = listed.json()["items"]
    assert "token" not in item
    assert item["id"] == minted["id"]

    (event,) = kinds(store, EventKind.DEVICE_TOKEN_MINTED)
    assert event.principal == "admin"
    assert event.payload["after"] == {"id": minted["id"], "device": "ops phone", "role": "operator"}
    assert token not in str(event.payload)


def test_mint_names_a_role_and_refuses_what_it_cannot_mint(
    client: TestClient, admin_token: str, store: Store, clock: FakeClock
) -> None:
    observer = mint(client, admin_token, name="wall display", role="observer")
    assert observer["role"] == "observer"
    for body, status in (
        ({"name": "wall display"}, 409),
        ({"name": "tablet", "role": "superuser"}, 409),
        # An empty role is refused, not read as the operator default.
        ({"name": "tablet", "role": ""}, 409),
        ({"name": " padded"}, 409),
        ({"name": "x" * (devices.NAME_MAX + 1)}, 409),
        ({"role": "operator"}, 422),
    ):
        response = client.post("/v1/admin/devices", json=body, headers=bearer(admin_token))
        assert response.status_code == status, (body, response.text)
    operator = mint_token(Uow(store), clock, name="op", role=Role.OPERATOR).token  # type: ignore[arg-type]
    refused = client.post("/v1/admin/devices", json={"name": "mine"}, headers=bearer(operator))
    assert refused.status_code == 403
    # The device prefix belongs to devices: a plain token may not take it.
    with pytest.raises(ConflictError, match="reserved"):
        tokens.create(
            SimpleNamespace(clock=clock, uow_factory=lambda: Uow(store)),  # type: ignore[arg-type]
            Uow(store),  # type: ignore[arg-type]
            principal="admin",
            name=f"{DEVICE_PREFIX}sneaky",
            role="admin",
            reason=None,
        )


def test_the_token_authenticates_v1_and_records_its_use(
    client: TestClient, admin_token: str, store: Store, clock: FakeClock
) -> None:
    minted = mint(client, admin_token, name="ops phone")
    token = minted["token"]
    first = client.get("/v1/whoami", headers=bearer(token))
    assert first.json() == {"name": f"{DEVICE_PREFIX}ops phone", "role": "operator"}
    device = store.devices[minted["id"]]
    assert device.last_used_at == clock.now()
    assert device.last_user_agent == PHONE
    (used,) = kinds(store, EventKind.DEVICE_TOKEN_USED)
    assert used.principal == f"{DEVICE_PREFIX}ops phone"
    assert used.payload["via"] == "api"
    assert used.payload["first_use"] is True
    assert used.payload["user_agent"] == PHONE

    # Within a minute nothing is written; after it only the last-used time moves.
    first_use = clock.now()
    clock.advance(30)
    client.get("/v1/whoami", headers=bearer(token))
    assert store.devices[minted["id"]].last_used_at == first_use
    clock.advance(120)
    client.get("/v1/whoami", headers=bearer(token))
    assert store.devices[minted["id"]].last_used_at == clock.now()
    assert len(kinds(store, EventKind.DEVICE_TOKEN_USED)) == 1

    # A new user agent, or a use after an hour without one, is audited again.
    client.get("/v1/whoami", headers=bearer(token, "Hades iOS app"))
    assert store.devices[minted["id"]].last_user_agent == "Hades iOS app"
    clock.advance(2 * 60 * 60)
    client.get("/v1/whoami", headers=bearer(token, "Hades iOS app"))
    assert len(kinds(store, EventKind.DEVICE_TOKEN_USED)) == 3

    listed = client.get("/v1/admin/devices", headers=bearer(admin_token, DESKTOP)).json()
    (item,) = listed["items"]
    assert item["last_used_at"] is not None
    assert item["last_used_local"].endswith(("CDT", "CST"))
    assert item["last_user_agent"] == "Hades iOS app"


def test_a_device_token_is_exchanged_once_for_a_ui_session(
    client: TestClient, admin_token: str, store: Store
) -> None:
    minted = mint(client, admin_token, name="ops phone")
    token = minted["token"]
    response = client.post(
        "/ui/device-sign-in",
        data={"token": token, "next": "/ui/board"},
        headers={"User-Agent": PHONE},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/ui/board"
    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith(f"{ui.COOKIE}=") for cookie in cookies)
    assert all(token not in cookie for cookie in cookies)
    (session,) = store.sessions.values()
    assert session.principal_id == minted["id"]
    device = store.devices[minted["id"]]
    assert device.exchanged_at is not None
    assert device.last_user_agent == PHONE
    (used,) = kinds(store, EventKind.DEVICE_TOKEN_USED)
    assert used.payload["via"] == "ui_session"

    # Once: neither door opens a second session with the same token.
    again = client.post("/ui/device-sign-in", data={"token": token}, follow_redirects=False)
    assert again.status_code == 409
    assert "already been exchanged" in again.text
    preauth = ui._preauth_serializer(client.app.state.ctx).dumps({"csrf": "fixture-csrf"})  # type: ignore[attr-defined]
    client.cookies.set(ui.PREAUTH_COOKIE, preauth, path="/ui/sign-in")
    through_sign_in = client.post(
        "/ui/sign-in", data={"token": token, "csrf": "fixture-csrf"}, follow_redirects=False
    )
    assert through_sign_in.status_code == 409
    assert len(store.sessions) == 1
    # The API keeps accepting the token after the exchange.
    assert client.get("/v1/whoami", headers=bearer(token)).status_code == 200


def test_device_sign_in_refuses_another_site_and_a_token_that_is_not_a_device(
    client: TestClient, admin_token: str, store: Store
) -> None:
    minted = mint(client, admin_token, name="ops phone")
    cross_site = client.post(
        "/ui/device-sign-in",
        data={"token": minted["token"]},
        headers={"Origin": "https://elsewhere.example"},
        follow_redirects=False,
    )
    assert cross_site.status_code == 403
    assert store.devices[minted["id"]].exchanged_at is None
    not_a_device = client.post(
        "/ui/device-sign-in", data={"token": admin_token}, follow_redirects=False
    )
    assert not_a_device.status_code == 401
    unknown = client.post("/ui/device-sign-in", data={"token": "nothing"}, follow_redirects=False)
    assert unknown.status_code == 401
    assert store.sessions == {}
    same_site = client.post(
        "/ui/device-sign-in",
        headers={"Origin": "https://hades.test", "Authorization": f"Bearer {minted['token']}"},
        follow_redirects=False,
    )
    assert same_site.status_code == 303


def test_revoke_ends_the_token_and_its_session(
    client: TestClient, admin_token: str, store: Store, clock: FakeClock
) -> None:
    minted = mint(client, admin_token, name="lost phone")
    token = minted["token"]
    signed_in = client.post("/ui/device-sign-in", data={"token": token}, follow_redirects=False)
    assert signed_in.status_code == 303
    (session_id,) = store.sessions

    no_reason = client.post(
        f"/v1/admin/devices/{minted['id']}/revoke", json={}, headers=bearer(admin_token)
    )
    assert no_reason.status_code == 422
    clock.advance(60)
    revoked = client.post(
        f"/v1/admin/devices/{minted['id']}/revoke",
        json={"reason": "the phone was lost"},
        headers=bearer(admin_token),
    )
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["revoked"] is True and body["state"] == "revoked"
    assert body["revoked_by"] == "admin"
    assert body["revoked_local"].endswith(("CDT", "CST"))

    assert client.get("/v1/whoami", headers=bearer(token)).status_code == 401
    assert authenticate(Uow(store), token) is None  # type: ignore[arg-type]
    request = SimpleNamespace(
        cookies={ui.COOKIE: ui._serializer(client.app.state.ctx).dumps(session_id)}  # type: ignore[attr-defined]
    )
    assert ui._session(request, client.app.state.ctx, Uow(store)) is None  # type: ignore[arg-type,attr-defined]
    twice = client.post(
        f"/v1/admin/devices/{minted['id']}/revoke",
        json={"reason": "again"},
        headers=bearer(admin_token),
    )
    assert twice.status_code == 409
    unknown = client.post(
        "/v1/admin/devices/01K0000000000000000000000Z/revoke",
        json={"reason": "no such device"},
        headers=bearer(admin_token),
    )
    assert unknown.status_code == 409

    (event,) = kinds(store, EventKind.DEVICE_TOKEN_REVOKED)
    assert event.principal == "admin"
    assert event.payload["reason"] == "the phone was lost"
    assert event.payload["after"]["enabled"] is False
    (item,) = client.get("/v1/admin/devices", headers=bearer(admin_token)).json()["items"]
    assert item["state"] == "revoked"


def test_the_device_events_are_audit_events_and_a_device_principal_keeps_its_name(
    client: TestClient, admin_token: str, store: Store, clock: FakeClock
) -> None:
    assert {
        EventKind.DEVICE_TOKEN_MINTED.value,
        EventKind.DEVICE_TOKEN_USED.value,
        EventKind.DEVICE_TOKEN_REVOKED.value,
    } <= ADMIN_KINDS
    minted = mint(client, admin_token, name="ops phone")
    with pytest.raises(ConflictError, match="keeps its name"):
        tokens.rename(
            SimpleNamespace(clock=clock, uow_factory=lambda: Uow(store)),  # type: ignore[arg-type]
            Uow(store),  # type: ignore[arg-type]
            principal="admin",
            principal_id=minted["id"],
            name="renamed",
            reason="tidy",
        )


def test_a_secret_shaped_user_agent_is_withheld() -> None:
    assert devices._agent(None) is None
    assert devices._agent("  ") is None
    assert devices._agent(PHONE) == PHONE
    assert devices._agent("x" * 600) == "x" * devices.AGENT_MAX
    shaped = "ghp_" + "A1b2C3d4" * 5
    assert devices._agent(f"agent {shaped}") == devices.WITHHELD


def test_a_principal_named_like_a_device_before_devices_stays_an_ordinary_one(
    client: TestClient, admin_token: str, store: Store, clock: FakeClock
) -> None:
    """A principal named `device:...` before devices existed has no device row: it signs
    in as before, keeps its token rotation, and may be renamed out of the prefix."""
    uow: Any = Uow(store)
    legacy = _mint(uow, clock, name=f"{DEVICE_PREFIX}old kiosk", role=Role.OPERATOR, rotate=False)
    assert not is_device(uow, legacy.principal)

    preauth = ui._preauth_serializer(client.app.state.ctx).dumps({"csrf": "fixture-csrf"})  # type: ignore[attr-defined]
    for _ in range(2):
        client.cookies.set(ui.PREAUTH_COOKIE, preauth, path="/ui/sign-in")
        signed_in = client.post(
            "/ui/sign-in",
            data={"token": legacy.token, "csrf": "fixture-csrf"},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303, signed_in.text
    assert kinds(store, EventKind.DEVICE_TOKEN_USED) == []
    # It is not a device, so the device door stays shut to it.
    not_a_device = client.post(
        "/ui/device-sign-in", data={"token": legacy.token}, follow_redirects=False
    )
    assert not_a_device.status_code == 401

    rotated = mint_token(
        uow, clock, name=f"{DEVICE_PREFIX}old kiosk", role=Role.OPERATOR, rotate=True
    )
    assert rotated.principal.id == legacy.principal.id
    with pytest.raises(ValueError, match="reserved"):
        mint_token(uow, clock, name=f"{DEVICE_PREFIX}new kiosk", role=Role.OPERATOR, rotate=True)

    renamed = tokens.rename(
        SimpleNamespace(clock=clock, uow_factory=lambda: Uow(store)),  # type: ignore[arg-type]
        uow,
        principal="admin",
        principal_id=legacy.principal.id,
        name="old kiosk",
        reason="out of the device namespace",
    )
    assert renamed["name"] == "old kiosk"
