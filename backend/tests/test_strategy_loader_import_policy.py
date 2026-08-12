import ast
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.strategy_loader import _check_imports


def test_strategy_import_policy_allows_builtin_ml_capability_module():
    tree = ast.parse("from services.ml import MLCapability")

    assert _check_imports(tree) == []


def test_strategy_import_policy_still_blocks_operating_system_module():
    tree = ast.parse("import os")

    violations = _check_imports(tree)

    assert any("Blocked import: 'os'" in violation for violation in violations)
