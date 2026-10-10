"""Local Docker v0.0.10 schema upgrade plus complete data rollback."""
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

from sleight.deploy import Deployer, DeploySpec, LocalRunner
from sleight.deploy.ops import ExtensionOps
from sleight.deploy.spec import DEFAULT_IMAGE
from sleight.providers import ProfileSpec

pytestmark = [pytest.mark.deploy, pytest.mark.skipif(os.environ.get("SLEIGHT_LOCAL_DOCKER_TEST") != "1", reason="Dedicated local Docker required")]


def test_old_manager_upgrade_and_cookie_data_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path / "home"))
    name = "sleight06-upgrade-" + uuid.uuid4().hex[:8]
    spec = DeploySpec(name=name, container_name=name, dir=str(tmp_path / "deployment").replace("\\", "/"),
                      image="cloakhq/cloakbrowser-manager:v0.0.10", data_volume=name + "-store", port=19067,
                      mem_limit="2000m", shm_size="512m", max_running=1, resource_key=name)
    dep = Deployer(spec, LocalRunner())
    if os.environ.get("SLEIGHT_TEST_CACHED_IMAGES") == "1":
        monkeypatch.setattr(dep, "_pull", lambda: None)
    try:
        dep.apply(pull=False)
        token = dep.existing_token()
        body = yaml.safe_load(Path(spec.compose_path).read_text(encoding="utf-8"))
        body["services"]["manager"]["environment"]["SLEIGHT_CUSTOM_PROBE"] = "keep-me"
        body["services"]["manager"]["networks"] = ["custom"]
        body["networks"] = {"custom": {"driver": "bridge"}}
        Path(spec.compose_path).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
        dep.apply(pull=False)
        ext = ExtensionOps(dep).push(Path(__file__).parent / "fixtures/extension", name="integration-probe")
        with dep.connect() as mgr:
            assert mgr.capabilities()["generation"] == "legacy"
            profile = mgr.create_profile(ProfileSpec.windows("cookie-preservation", fingerprint_seed=1234,
                                          extension_paths=(ext.container_path,)), launch=True)
            with mgr.lease(instance_id=profile.id) as handle, handle.session() as session:
                assert session.call("Network.setCookie", {"name": "migration_probe", "value": "before",
                                    "url": "https://sleight-local-test.invalid/", "expires": time.time() + 86400})["success"]
            mgr.stop(profile.id)
        dep.upgrade(DEFAULT_IMAGE)

        def verify(generation, expected):
            with dep.connect() as mgr:
                assert mgr.capabilities()["generation"] == generation
                assert profile.id in {p["id"] for p in mgr.list_profiles()}
                mgr.ensure_ready(profile.id)
                with mgr.lease(instance_id=profile.id) as handle, handle.session() as session:
                    cookies = session.cookies(["https://sleight-local-test.invalid/"])
                    assert any(c["name"] == "migration_probe" and c["value"] == expected for c in cookies), cookies
                    assert session.call("Network.setCookie", {"name": "migration_probe", "value": "after",
                                        "url": "https://sleight-local-test.invalid/", "expires": time.time() + 86400})["success"]
                mgr.stop(profile.id)
            reports = ExtensionOps(dep).verify(launch=True, settle=1)
            assert any(r.id == profile.id and r.ok and r.evidence == "chromium-inventory" for r in reports), reports

        verify("modern", "before")
        dep.rollback()
        verify("legacy", "before")
        assert dep.existing_token() == token
        restored = yaml.safe_load(Path(spec.compose_path).read_text(encoding="utf-8"))
        assert restored["services"]["manager"]["environment"]["SLEIGHT_CUSTOM_PROBE"] == "keep-me"
        assert restored["services"]["manager"]["networks"] == ["custom"]
        assert restored["networks"]["custom"]["driver"] == "bridge"
    finally:
        dep.destroy()
        subprocess.run(["docker", "volume", "rm", spec.data_volume], check=True, capture_output=True)
