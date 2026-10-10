"""Resolve an existing Compose with Docker, then adopt its actual bind mount."""

import json

from .errors import DeployError
from .spec import DeploySpec


def import_spec(runner, directory: str, *, filename="docker-compose.yaml", service="manager") -> DeploySpec:
    spec = DeploySpec(dir=directory, compose_filename=filename, service_name=service)
    spec.validate()
    result = runner.run(["docker", "compose", "-f", spec.compose_path, "config", "--format", "json"],
                        cwd=directory, timeout=60)
    if not result.ok:
        raise DeployError("Compose could not be resolved; check filename and .env on the target")
    raw = json.loads(result.out)
    row = raw.get("services", {}).get(service)
    if not row:
        raise DeployError(f"No service {service!r} in Compose")
    mount = next((v for v in row.get("volumes", []) if v.get("target") == "/data"), None)
    volume_name = ""
    if mount and mount.get("type") == "volume" and mount.get("volume", {}).get("subpath") == "data":
        volume_name = raw.get("volumes", {}).get(mount["source"], {}).get("name", mount["source"])
    if not mount or (mount.get("type") != "bind" and not volume_name):
        raise DeployError("Import requires a dedicated /data bind mount; named-volume migration is manual")
    port = next((p for p in row.get("ports", []) if int(p.get("target", 0)) == 8080), None)
    if not port:
        raise DeployError("Manager needs a published 8080 port for CDP and monitoring")
    bind_ip = port.get("host_ip") or "0.0.0.0"
    plugins = next((v for v in row.get("volumes", []) if v.get("target") == "/data/extensions"), None)
    if plugins and plugins.get("type") != "bind":
        raise DeployError("Extension management requires a /data/extensions bind mount")
    return spec.replace(name=raw.get("name", spec.name), image=row["image"],
                        resource_key=raw.get("name") or row.get("container_name") or spec.resource_key,
                        container_name=row.get("container_name", spec.container_name),
                        data_path=mount["source"] if not volume_name else "", data_volume=volume_name,
                        extensions_path=plugins["source"] if plugins else "", bind_ip=bind_ip,
                        port=int(port["published"]), expose=bind_ip not in ("127.0.0.1", "::1"))
