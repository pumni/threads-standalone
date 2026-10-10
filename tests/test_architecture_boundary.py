from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from threads_platform.standalone.__main__ import _build_parser

_ROOT = Path(__file__).parents[1]
_FORBIDDEN_IMPORT_PREFIXES = (
    "alembic",
    "asyncpg",
    "fastapi",
    "infrastructure.worker_agent",
    "sqlalchemy",
    "threads_platform.control_plane",
    "threads_platform.workers",
)


def test_package_source_has_no_distributed_or_database_imports() -> None:
    source_root = _ROOT / "src" / "threads_platform"
    imports: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.append(node.module)

    forbidden = [
        module
        for module in imports
        if any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in _FORBIDDEN_IMPORT_PREFIXES
        )
    ]
    assert forbidden == []


def test_runtime_dependencies_have_no_old_repo_or_distributed_stack() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = [item.lower() for item in project["dependencies"]]
    forbidden = (
        "pumni/thread",
        "sqlalchemy",
        "asyncpg",
        "alembic",
        "fastapi",
        "uvicorn",
        "postgres",
        "worker",
    )
    assert not [
        dependency for dependency in dependencies if any(term in dependency for term in forbidden)
    ]


@pytest.mark.parametrize("command", ["publish", "reply", "quote", "browser", "worker"])
def test_cli_does_not_expose_mutation_or_distributed_commands(command: str) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["api", command, "synthetic_account"])
