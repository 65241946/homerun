from __future__ import annotations

import ast
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _configured_worker_planes() -> set[str]:
    host_path = PROJECT_ROOT / "backend" / "workers" / "host.py"
    module = ast.parse(host_path.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id != "_PLANE_CONFIGS":
            continue
        if not isinstance(node.value, ast.Dict):
            break
        return {
            str(key.value)
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value != "all"
        }
    raise AssertionError("Could not find _PLANE_CONFIGS in workers/host.py")


def _compose_services() -> set[str]:
    compose_path = PROJECT_ROOT / "docker-compose.yml"
    in_services = False
    services: set[str] = set()
    for line in compose_path.read_text(encoding="utf-8").splitlines():
        if line == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        match = re.fullmatch(r"  ([a-z0-9-]+):", line)
        if match:
            services.add(match.group(1))
    return services


def test_compose_launches_every_dedicated_worker_plane() -> None:
    expected_services = {f"worker-{plane}" for plane in _configured_worker_planes()}

    assert expected_services <= _compose_services()


def test_compose_passes_settlement_runtime_mode_through_shared_backend_environment() -> None:
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert (
        "HOMERUN_SETTLEMENT_RUNTIME_MODE: "
        "${HOMERUN_SETTLEMENT_RUNTIME_MODE:-observe}"
    ) in compose_text
    assert "HOMERUN_SETTLEMENT_RUNTIME_MODE=observe" in env_text
