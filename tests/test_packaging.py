"""The installed package must contain every module the source tree has (an editable install hides
a missing subpackage: a clean install of 0.1.0 crashed at import)."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_python_package_in_the_tree_is_declared_for_installation():
    declared = set(tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"])
    on_disk = {".".join(p.parent.relative_to(ROOT).parts) for p in (ROOT / "aiops").rglob("__init__.py")}
    assert on_disk == declared, f"undeclared: {on_disk - declared}, missing on disk: {declared - on_disk}"
