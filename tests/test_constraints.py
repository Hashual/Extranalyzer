"""constraints.txt fige toutes les dépendances de l'image Docker, et le Dockerfile s'en sert."""
import importlib.metadata as md
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
# Environnement de l'image (python:3.12-alpine sur le Pi) pour évaluer les marqueurs des dépendances.
IMAGE_ENV = {"python_version": "3.12", "python_full_version": "3.12.0", "sys_platform": "linux",
             "platform_system": "Linux", "platform_machine": "aarch64", "os_name": "posix",
             "implementation_name": "cpython", "platform_python_implementation": "CPython", "extra": ""}


def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, sep, version = line.partition("==")
            assert sep and version.strip(), f"version exacte attendue : {line!r}"
            pins[canonicalize_name(name)] = version.strip()
    return pins


def _runtime_requirements() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return [Requirement(r) for r in project["dependencies"]]


def test_every_runtime_dependency_and_build_tool_is_pinned():
    pins = _pins()
    names = {canonicalize_name(r.name) for r in _runtime_requirements()} | {"setuptools", "wheel"}
    assert names <= set(pins), f"non figés : {sorted(names - set(pins))}"


def test_transitive_dependencies_are_pinned():
    """Fermeture des dépendances (d'après les métadonnées installées) toute présente dans constraints.txt."""
    pins, seen = _pins(), set()
    todo = _runtime_requirements()
    while todo:
        req = todo.pop()
        name = canonicalize_name(req.name)
        if name in seen or (req.marker is not None and not req.marker.evaluate(IMAGE_ENV)):
            continue
        seen.add(name)
        todo += [Requirement(r) for r in md.requires(req.name) or []]
    assert seen <= set(pins), f"dépendances transitives non figées : {sorted(seen - set(pins))}"


def test_dockerfile_builds_with_the_constraints():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY constraints.txt" in dockerfile
    pip_steps = [line for line in dockerfile.splitlines()
                 if "pip install" in line and "setuptools" in line or "pip wheel" in line and "--no-deps" not in line]
    assert pip_steps and all("-c constraints.txt" in line for line in pip_steps), pip_steps
    assert "!constraints.txt" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
