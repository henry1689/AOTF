"""M0-A1a 包级测试。"""

from __future__ import annotations

import pathlib
import tomllib

import aotf

# 项目根以本测试文件的绝对位置定位，不依赖偶然 cwd。
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _load_pyproject() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_import_has_no_side_effect_and_exposes_version() -> None:
    assert aotf.__version__ == "0.1.0.dev0"
    assert aotf.__all__ == ["__version__"]


def test_package_version_matches_pyproject() -> None:
    pyproject = _load_pyproject()
    assert pyproject["project"]["version"] == aotf.__version__


def test_pyproject_runtime_dependencies_allowlisted() -> None:
    # M0-D2 owner 决策：唯一运行时依赖 = claude-agent-sdk（零依赖铁律的唯一例外）
    pyproject = _load_pyproject()
    deps = pyproject["project"].get("dependencies", [])
    parsed = {d.split(">=")[0].split("<")[0].strip() for d in deps}
    assert parsed <= {"claude-agent-sdk"}


def test_python_range_is_pre_mvp_supported_range() -> None:
    pyproject = _load_pyproject()
    assert pyproject["project"]["requires-python"] == ">=3.11,<3.14"


def test_runtime_ignores_do_not_hide_source_packages() -> None:
    lines = (PROJECT_ROOT / ".gitignore").read_text(
        encoding="utf-8").splitlines()
    assert "/artifacts/" in lines
    assert "/scratch/" in lines
    assert "/worktrees/" in lines
    assert "artifacts/" not in lines


def test_readme_uses_executable_powershell_test_command() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Set-Location 'D:\\tools\\aotf'" in readme
    assert "$env:PYTHONDONTWRITEBYTECODE = '1'" in readme
    assert "$env:PYTHONPATH = 'src'" in readme
    assert (
        r"C:\Users\henry\AppData\Local\Programs\Python\Python313\python.exe"
        in readme
    )
    assert (
        "-B -m pytest -p no:cacheprovider -q tests/unit/test_package.py tests/unit/test_errors.py"
        in readme
    )
    assert "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src" not in readme
