from __future__ import annotations

import importlib.metadata
import os
import re
import sys
import tomllib
import zipfile
from pathlib import Path

import threads_platform

_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_REQUIREMENTS = {"httpx2", "playwright", "pydantic", "pydantic-settings"}
_FORBIDDEN_PACKAGES = {
    "alembic",
    "asyncpg",
    "celery",
    "fastapi",
    "kafka-python",
    "psycopg",
    "psycopg2",
    "redis",
    "sqlalchemy",
    "uvicorn",
}


def _requirement_name(requirement: str) -> str:
    match = re.match(r"([A-Za-z0-9_.-]+)", requirement)
    assert match is not None
    return re.sub(r"[-_.]+", "-", match.group(1)).casefold()


def test_installed_distribution_and_optional_wheel_are_source_free() -> None:
    distribution = importlib.metadata.distribution("threads-standalone")
    requirements = distribution.requires or []
    requirement_names = {_requirement_name(item) for item in requirements}
    assert requirement_names == _RUNTIME_REQUIREMENTS
    assert requirement_names.isdisjoint(_FORBIDDEN_PACKAGES)

    entry_points = {
        entry_point.name: entry_point.value
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts"
    }
    assert entry_points == {"threads-local": "threads_platform.standalone.__main__:main"}

    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.14,<3.15"
    assert lock["requires-python"] == "==3.14.*"

    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") != "1":
        return

    assert "PYTHONPATH" not in os.environ
    imported_path = Path(threads_platform.__file__).resolve()
    assert "site-packages" in str(imported_path).casefold()
    checkout_paths = {
        _ROOT.resolve(),
        (_ROOT / "src").resolve(),
    }
    active_paths = {Path(item or os.getcwd()).resolve() for item in sys.path if item}
    assert checkout_paths.isdisjoint(active_paths)

    wheel_value = os.environ.get("THREADS_STANDALONE_WHEEL_PATH")
    assert wheel_value is not None
    wheel_path = Path(wheel_value).resolve(strict=True)
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        roots = {name.split("/", maxsplit=1)[0] for name in names}
        assert roots == {"threads_platform", "threads_standalone-0.1.0.dist-info"}
        assert all(
            not name.casefold().endswith((".json", ".cookie", ".db", ".sqlite", ".log"))
            for name in names
        )
        assert not any(
            term in name.casefold()
            for name in names
            for term in ("pumni/thread", "worker", "control_plane", "evidence")
        )
        metadata_path = "threads_standalone-0.1.0.dist-info/METADATA"
        metadata = wheel.read(metadata_path).decode("utf-8")
        assert "pumni/thread" not in metadata.casefold()
        assert "git+" not in metadata.casefold()
        metadata_requirements = {
            _requirement_name(line.removeprefix("Requires-Dist: "))
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist: ")
        }
        assert metadata_requirements == _RUNTIME_REQUIREMENTS
