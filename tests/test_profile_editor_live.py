"""Opt-in real Chrome persistence check on a disposable local profile."""

import os
import time

import pytest

from sleight import connect
from sleight.core import errors
from sleight.runtime.native import NativeSupervisor, discover_browsers
from sleight.service.execution import Controller, Supervisor
from sleight.service.identity import Identity
from sleight.service.profile_editor import ProfileEditor

pytestmark = pytest.mark.skipif(os.environ.get("SLEIGHT_EDITOR_LIVE") != "1", reason="Dedicated local editor integration")


def test_real_native_cookie_survives_edit_and_restart(service_db):
    browsers = discover_browsers()
    if not browsers:
        pytest.skip("No installed browser")
    identity = Identity()
    identity.ensure_admin("editor-live-fixture")
    user = identity.lookup("editor-live-fixture")
    row = Controller().create({"name": "cookie-edit-fixture", "binary": browsers[0]["binary"], "headless": True, "ephemeral": False}, user, "cookie-edit")
    native, editor = NativeSupervisor(), ProfileEditor(Supervisor())
    ref = "profile:" + row["id"]
    try:
        running = native.start(row["source_id"])
        with connect(f"http://127.0.0.1:{running['port']}") as browser:
            browser.call("Network.setCookies", {"cookies": [{"name": "fixture_login", "value": "preserved", "url": "http://127.0.0.1", "expires": time.time() + 86400}]})
            assert any(c["name"] == "fixture_login" for c in browser.cookies(["http://127.0.0.1"]))
            with pytest.raises(errors.Busy):
                editor.update(ref, {"notes": "running"}, user)
            browser.transport.call("Browser.close")
        for _ in range(100):
            if native.get(row["source_id"])["status"] == "stopped":
                break
            time.sleep(.05)
        native.stop(row["source_id"])
        original = native.get(row["source_id"])
        editor.update(ref, {"name": "cookie-edit-renamed", "notes": "new", "headless": True}, user)
        edited = native.get(row["source_id"])
        assert edited["id"] == original["id"] and edited["profile_dir"] == original["profile_dir"]
        running = native.start(row["source_id"])
        with connect(f"http://127.0.0.1:{running['port']}") as browser:
            assert any(c["name"] == "fixture_login" and c["value"] == "preserved" for c in browser.cookies(["http://127.0.0.1"]))
    finally:
        native.stop(row["source_id"])
        native.delete(row["source_id"], native.get(row["source_id"])["name"])
