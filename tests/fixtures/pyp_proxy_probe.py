"""Standalone probe using pyp's real route and only in-memory test users."""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Annotated

sys.path.insert(0, str(Path.cwd()))

from app.models.user_model import User
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.websockets import WebSocketDisconnect

url, token, job = sys.argv[1:]
settings = SimpleNamespace(SLEIGHT_UPSTREAM=url, SLEIGHT_TOKEN=token, SLEIGHT_COOKIE_TTL=900,
                           SECRET_KEY="isolated-pyp-proxy-test-key")
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
users = [User(email=f"proxy-{i}@example.org", full_name=f"Admin {i}", hashed_password="fixture", is_superuser=True) for i in range(2)]
with Session(engine) as session:
    for user in users:
        session.add(user)
    session.commit()
    for user in users:
        session.refresh(user)

def get_db():
    with Session(engine) as session:
        yield session

def admin(request: Request):
    return users[int(request.headers.get("x-test-user", "0"))]

for name, values in (
    ("app.core.config", {"settings": settings}), ("app.core.security", {"ALGORITHM": "HS256"}),
    ("app.core.db", {"engine": engine}),
    ("app.api.deps", {"SessionDep": Annotated[Session, Depends(get_db)], "get_current_active_superuser": admin}),
):
    module = ModuleType(name)
    module.__dict__.update(values)
    sys.modules[name] = module

from app.api.routes import sleight  # noqa: E402

app = FastAPI()
app.include_router(sleight.router, prefix="/api/v1")
app.include_router(sleight.proxy_router)
with TestClient(app) as client:
    assert client.get("/sleight/").status_code == 401
    response = client.post("/api/v1/sleight/bootstrap")
    assert response.status_code == 200, response.text
    cookie = client.cookies.get(sleight.COOKIE)
    assert token not in cookie
    first = client.get("/sleight/api/auth/me").json()["user"]["id"]
    assert first != "admin"
    assert client.get("/sleight/", headers={"Authorization": "Bearer fake-pyp-jwt", "X-Sleight-Token": "forged"}).status_code == 200
    import re
    for asset in re.findall(r'(?:src|href)="\./(assets/[^\"]+)"', client.get("/sleight/").text):
        assert client.get("/sleight/" + asset).status_code == 200
    assert client.post("/sleight/api/auth/login", json={"token": "fake"}).status_code == 403
    assert client.post("/sleight/api/v1/profiles", json={}, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    events = client.get(f"/sleight/api/jobs/{job}/events")
    assert events.status_code == 200 and "event: done" in events.text, events.text
    profile = client.post("/sleight/api/v1/profiles", json={}, headers={"Idempotency-Key": "pyp-create"}).json()
    opened = client.post("/sleight/api/v1/sessions", json={"profile": profile["id"]}).json()
    assert opened["owner"] == first
    with client.websocket_connect(f"/sleight/api/v1/sessions/{opened['id']}/events") as stream:
        assert stream.receive_json()["state"] == "running"
        with Session(engine) as session:
            actual = session.get(User, users[0].id)
            actual.is_superuser = False
            session.add(actual)
            session.commit()
        try:
            while True:
                stream.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 4401, exc.code
    with Session(engine) as session:
        actual = session.get(User, users[0].id)
        actual.is_superuser = True
        session.add(actual)
        session.commit()
    settings.SLEIGHT_COOKIE_TTL = 2
    assert client.post("/api/v1/sleight/bootstrap").status_code == 200
    with client.websocket_connect(f"/sleight/api/v1/sessions/{opened['id']}/events") as stream:
        assert stream.receive_json()["state"] == "running"
        try:
            while True:
                stream.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 4401, exc.code
    settings.SLEIGHT_COOKIE_TTL = 900
    assert client.post("/api/v1/sleight/bootstrap").status_code == 200
    assert client.delete(f"/sleight/api/v1/sessions/{opened['id']}").status_code == 200
    assert client.post("/api/v1/sleight/bootstrap", headers={"X-Test-User": "1"}).status_code == 200
    assert client.get("/sleight/api/auth/me").json()["user"]["id"] != first
    with Session(engine) as session:
        actual = session.get(User, users[1].id)
        actual.is_superuser = False
        session.add(actual)
        session.commit()
    assert client.get("/sleight/api/auth/me").status_code == 403
print("pyp proxy: identity isolation, assets, SSE, WS, origin, revocation and live WS expiry passed")
