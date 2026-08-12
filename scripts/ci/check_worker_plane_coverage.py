"""CI guard: every worker plane must have a process behind it.

``backend/workers/host.py`` defines the worker planes; ``docker-compose.yml``
is what actually runs them.  When the two drift, a whole subsystem goes
quiet with no error anywhere — the plane simply never starts, so nothing
logs, nothing fails, and the feature just reads zero forever.

That is not hypothetical.  The compose stack shipped three planes
(trading, news, discovery) while host.py defined eight, so the scanner
never ran.  The market opportunity tab sat at 0 for the entire life of
the deployment, and because the scanner is also what classifies markets
into sports/soccer/baseball categories, the sports tab did too.  Nothing
in the logs pointed at it: an absent process has no voice.

This guard makes that drift loud at build time instead of silent at
runtime.

Planes may be deliberately excluded — ``recording`` runs a broad WS book
stream and parquet encoding, which is a data-capture workload rather
than a functional gap.  List those in ``OPTIONAL_PLANES`` with a reason;
anything else missing fails the build.

Usage:
    python scripts/ci/check_worker_plane_coverage.py
Exit code 0 = pass; non-zero = a plane is defined but never deployed.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_PY = REPO_ROOT / "backend" / "workers" / "host.py"
COMPOSE_YML = REPO_ROOT / "docker-compose.yml"

# Planes intentionally not deployed, with the reason they are opt-in.
# Keep this list short and justified — it is the only escape hatch.
OPTIONAL_PLANES: dict[str, str] = {
    "all": "aggregate plane for single-process runs (launcher/dev), not a compose service",
    "recording": "microstructure capture: broad WS book stream + parquet encoding, disk-hungry and opt-in",
}


def defined_planes() -> set[str]:
    """Plane names from the ``_PLANE_CONFIGS`` dict literal in host.py."""
    tree = ast.parse(HOST_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # ``_PLANE_CONFIGS`` carries a type annotation, so it parses as an
        # AnnAssign rather than a plain Assign.  Accept either shape.
        if isinstance(node, ast.AnnAssign):
            names = [getattr(node.target, "id", None)]
        elif isinstance(node, ast.Assign):
            names = [getattr(t, "id", None) for t in node.targets]
        else:
            continue
        if "_PLANE_CONFIGS" not in names:
            continue
        if not isinstance(node.value, ast.Dict):
            break
        return {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    raise SystemExit(f"could not find a _PLANE_CONFIGS dict literal in {HOST_PY}")


def deployed_planes() -> set[str]:
    """Plane names from ``python -m workers.host <plane>`` compose commands.

    Parsed with a regex rather than a YAML load so the check has no
    third-party dependency and keeps working if the compose file grows
    anchors or extension fields.
    """
    text = COMPOSE_YML.read_text(encoding="utf-8")
    return set(re.findall(r'workers\.host"\s*,\s*"([a-z_]+)"', text))


def main() -> int:
    defined = defined_planes()
    deployed = deployed_planes()

    missing = sorted(defined - deployed - set(OPTIONAL_PLANES))
    unknown = sorted(deployed - defined)

    for plane in sorted(defined & deployed):
        print(f"  ok       {plane}")
    for plane, reason in sorted(OPTIONAL_PLANES.items()):
        if plane in defined and plane not in deployed:
            print(f"  opt-out  {plane} — {reason}")

    if unknown:
        print()
        for plane in unknown:
            print(f"  ERROR    compose runs '{plane}', which host.py does not define")

    if missing:
        print()
        print("Worker planes defined in host.py but never deployed:")
        for plane in missing:
            print(f"  MISSING  {plane}")
        print()
        print("A plane with no process behind it fails silently — the feature")
        print("reads zero forever and nothing logs an error.  Either add a")
        print("service to docker-compose.yml running")
        print("  command: [\"python\", \"-m\", \"workers.host\", \"<plane>\"]")
        print("or, if the plane is genuinely opt-in, add it to OPTIONAL_PLANES")
        print("in this script with the reason.")

    if missing or unknown:
        return 1

    print()
    print(f"All {len(defined - set(OPTIONAL_PLANES))} required worker planes are deployed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
