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
    "infrastructure.persistence",
    "sqlalchemy",
    "threads_platform.control_plane",
    "threads_platform.domain.workers",
    "threads_platform.workers",
    "workers",
    "uvicorn",
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

    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_names = {item["name"].lower() for item in lock["package"]}
    forbidden_locked = {
        "alembic",
        "asyncpg",
        "celery",
        "confluent-kafka",
        "fastapi",
        "kafka-python",
        "sqlalchemy",
        "psycopg2",
        "psycopg2-binary",
        "uvicorn",
        "psycopg",
        "redis",
    }
    assert locked_names.isdisjoint(forbidden_locked)
    assert all("git" not in item.get("source", {}) for item in lock["package"])
    assert all(
        "pumni/thread" not in str(item.get("source", {})).casefold() for item in lock["package"]
    )


def test_playwright_import_is_confined_to_its_lazy_engine_adapter() -> None:
    source_root = _ROOT / "src" / "threads_platform"
    imports: list[Path] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.Import):
                if any(
                    alias.name == "playwright" or alias.name.startswith("playwright.")
                    for alias in node.names
                ):
                    module = "playwright"
            elif isinstance(node, ast.ImportFrom):
                module = node.module
            if module == "playwright" or (module is not None and module.startswith("playwright.")):
                imports.append(path.relative_to(source_root))

    assert set(imports) == {Path("browser/playwright_engine.py")}


@pytest.mark.parametrize("command", ["publish", "reply", "quote", "browser", "worker"])
def test_api_cli_does_not_expose_mutation_or_distributed_commands(command: str) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["api", command, "synthetic_account"])
