"""In-place edits preserve data and reject active or unauthorized mutations."""

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sleight.core import errors
from sleight.core.types import InstanceStatus
from sleight.deploy.api.app import create_app
from sleight.providers.cloakbrowser import CLEAR
from sleight.runtime.config import Templates
from sleight.runtime.native import NativeSupervisor
from sleight.service.execution import Controller, Supervisor
from sleight.service.identity import Identity
from sleight.service.profile_editor import ProfileEditor


def setup():
    identity = Identity()
    identity.ensure_admin("editor-fixture")
    user = identity.lookup("editor-fixture")
    row = Controller().create({"name": "Original", "binary": sys.executable, "kind": "native", "ephemeral": False}, user, "original")
    return user, row, ProfileEditor(Supervisor())


def test_native_edit_preserves_profile_cookie_data_and_frees_old_name(service_db):
    user, row, editor = setup()
    native = NativeSupervisor().get(row["source_id"])
    path = Path(native["profile_dir"]) / "Cookies"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"saved-login-fixture")
    edited = editor.update("profile:" + row["id"], {"name": "Renamed", "notes": "saved", "headless": False}, user)
    source = NativeSupervisor().get(row["source_id"])
    assert edited["id"] == row["id"] and edited["source_id"] == row["source_id"]
    assert edited["name"] == source["name"] == "Renamed"
    assert edited["notes"] == "saved" and source["headless"] is False
    assert source["profile_dir"] == native["profile_dir"] and path.read_bytes() == b"saved-login-fixture"
    replacement = Controller().create({"name": "Original", "binary": sys.executable, "ephemeral": False}, user, "replacement")
    assert replacement["id"] != row["id"]


def test_edit_rejects_duplicate_names_ownership_and_active_sessions(service_db):
    user, row, editor = setup()
    Controller().create({"name": "Other", "binary": sys.executable}, user, "other")
    ref = "profile:" + row["id"]
    with pytest.raises(errors.Busy):
        editor.update(ref, {"name": " other "}, user)
    identity = Identity()
    other = identity.save_user({"name": "other user"})
    principal = identity.lookup(identity.issue(other["id"])["token"])
    with pytest.raises(errors.NotFound):
        editor.configuration(ref, principal)
    with pytest.raises(errors.NotFound):
        editor.update(ref, {"notes": "foreign"}, principal)
    with service_db.state("service-sessions") as state:
        state["records"]["busy"] = {"profile": row["id"], "state": "starting"}
    with pytest.raises(errors.Busy):
        editor.update(ref, {"notes": "busy"}, user)
    assert service_db.get("profiles", row["id"])["name"] == "Original"


def test_validation_failure_restores_metadata_and_locked_fields_are_rejected(service_db):
    user, row, editor = setup()
    ref = "profile:" + row["id"]
    for changes in ({"name": "Invalid", "fingerprint_seed": 25}, {"environment": "other"}, {"ephemeral": True}, {"extension_paths": "invalid"}):
        with pytest.raises(ValueError):
            editor.update(ref, changes, user)
    assert service_db.get("profiles", row["id"])["name"] == "Original"
    assert NativeSupervisor().get(row["source_id"])["name"] == "Original"


def test_edit_template_applies_proxy_and_preserves_profile_directory(service_db):
    user, row, editor = setup()
    template = Templates().save({"name": "local proxy", "proxy": "http://127.0.0.1:8080", "extension_paths": []})
    old = NativeSupervisor().get(row["source_id"])
    editor.update("profile:" + row["id"], {"template_id": template["id"]}, user)
    source = NativeSupervisor().get(row["source_id"])
    assert source["proxy"] == "http://127.0.0.1:8080" and source["profile_dir"] == old["profile_dir"]
    editor.update("profile:" + row["id"], {"template_id": ""}, user)
    assert NativeSupervisor().get(row["source_id"])["proxy"] == ""


def test_cloak_edit_preserves_custom_args_and_masks_secrets(service_db, monkeypatch):
    identity = Identity()
    identity.ensure_admin("editor-fixture")
    user = identity.lookup("editor-fixture")
    row = {"id": "cloak", "source_id": "manager-id", "owner": "admin", "environment": "manager",
           "kind": "cloak", "name": "Cloak", "ephemeral": False, "state": "idle"}
    service_db.put("profiles", row["id"], row)
    source = {"name": "manager-name", "fingerprint_seed": 77, "proxy": "socks5://account:secret@127.0.0.1:1080",
              "launch_args": ["--lang=en", "--load-extension=/data/old"], "extension_paths": ["/data/old"]}
    class Manager:
        def get_profile(self, pid):
            assert pid == "manager-id"
            return dict(source)
        def capabilities(self):
            return {"generation": "modern"}
        def status(self, pid):
            return InstanceStatus.STOPPED
        def update_profile(self, pid, **changes):
            assert pid == "manager-id"
            source.update({k: "" if v is CLEAR else v for k, v in changes.items()})
    @contextmanager
    def manager_for(environment):
        assert environment == "manager"
        yield Manager()
    monkeypatch.setattr("sleight.service.profile_editor.manager_for", manager_for)
    editor = ProfileEditor(Supervisor())
    config = editor.configuration("profile:cloak", user)
    assert "secret" not in str(config) and "account" not in str(config)
    edited = editor.update("profile:cloak", {"name": "New alias", "fingerprint_seed": 123, "extension_paths": ["/data/new"]}, user)
    assert edited["source_id"] == "manager-id" and source["fingerprint_seed"] == 123
    assert source["launch_args"] == ["--lang=en"] and source["extension_paths"] == ["/data/new"]
    assert source["proxy"].startswith("socks5://account:secret@")
    operator = identity.save_user({"name": "fixed profile user"})
    principal = identity.lookup(identity.issue(operator["id"])["token"])
    service_db.put("profiles", row["id"], {**edited, "owner": operator["id"], "external": True})
    config = editor.configuration("profile:cloak", principal)
    assert config["metadata_only"] and "extension_paths" not in config
    editor.update("profile:cloak", {"name": "Own alias", "notes": "own note"}, principal)
    with pytest.raises(PermissionError):
        editor.update("profile:cloak", {"fingerprint_seed": 456}, principal)
    assert source["name"] == "manager-name" and source["fingerprint_seed"] == 123


def test_editor_api_and_name_check_exclude_current_instance(service_db):
    _user, row, _editor = setup()
    headers = {"X-Sleight-Token": "editor-fixture"}
    ref = "profile:" + row["id"]
    with TestClient(create_app(token="editor-fixture", runtime=False)) as client:
        response = client.get("/api/v1/fleet/configuration", params={"ref": ref}, headers=headers)
        assert response.status_code == 200 and response.json()["name"] == "Original"
        response = client.get("/api/v1/fleet/name", params={"name": "Original", "exclude": ref}, headers=headers)
        assert response.json()["available"]
        assert not client.get("/api/v1/fleet/name", params={"name": "Original"}, headers=headers).json()["available"]
        response = client.post("/api/v1/fleet/edit", json={"ref": ref, "changes": {"notes": "updated"}}, headers=headers)
        assert response.status_code == 200 and response.json()["notes"] == "updated"
