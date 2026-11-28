"""架构单向依赖断言测试：强制执行 docs/architecture/module-boundaries.md 中的分层边界。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"

SERVICE_ENTRY_FILES = {
    "app/service_main.py",
    "app/api/service_routes.py",
    "app/api/study_routes.py",
}

DESKTOP_ENTRY_FILES = {
    "app/main.py",
    "app/api/routes.py",
    "app/api/batch_routes.py",
    "app/api/qa_routes.py",
    "app/api/library_routes.py",
    "app/api/settings_routes.py",
    "app/api/stats_routes.py",
}

PACKAGE_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "app/services/": (
        "app.courses",
        "app.study",
        "app.worker",
        "app.generation",
        "app.infrastructure",
        "app.api",
        "app.main",
        "app.service_main",
    ),
    "app/infrastructure/": (
        "app.api",
        "app.courses",
        "app.study",
        "app.worker",
        "app.generation",
        "app.main",
        "app.service_main",
    ),
    "app/generation/": (
        "app.api",
        "app.courses",
        "app.study",
        "app.worker",
        "app.main",
        "app.service_main",
    ),
    "app/courses/": (
        "app.api",
        "app.study",
        "app.worker",
        "app.main",
        "app.service_main",
    ),
    "app/study/": (
        "app.api",
        "app.worker",
        "app.main",
        "app.service_main",
    ),
    "app/worker/": (
        "app.api",
        "app.courses",
        "app.study",
        "app.main",
        "app.service_main",
    ),
}

SERVICE_ENTRY_FORBIDDEN: tuple[str, ...] = (
    "app.main",
    "app.api.routes",
    "app.api.batch_routes",
    "app.api.qa_routes",
    "app.api.library_routes",
    "app.api.settings_routes",
    "app.api.stats_routes",
)

DESKTOP_ENTRY_FORBIDDEN: tuple[str, ...] = (
    "app.service_main",
    "app.courses",
    "app.study",
    "app.worker",
    "app.generation",
    "app.infrastructure",
    "app.api.service_routes",
    "app.api.study_routes",
)


def extract_internal_imports(source: str) -> set[str]:
    """解析源码中所有指向 app.* 的导入目标（含 from pkg import submodule 展开）。"""
    tree = ast.parse(source)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app" or node.module.startswith("app."):
                targets.add(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        targets.add(f"{node.module}.{alias.name}")
    return targets


def check_forbidden(rel_posix: str, source: str) -> list[str]:
    forbidden: list[str] = []
    for pkg_prefix, rules in PACKAGE_FORBIDDEN.items():
        if rel_posix.startswith(pkg_prefix):
            forbidden.extend(rules)
    if rel_posix in SERVICE_ENTRY_FILES:
        forbidden.extend(SERVICE_ENTRY_FORBIDDEN)
    if rel_posix in DESKTOP_ENTRY_FILES:
        forbidden.extend(DESKTOP_ENTRY_FORBIDDEN)
    if not forbidden:
        return []

    violations: list[str] = []
    for target in sorted(extract_internal_imports(source)):
        for rule in forbidden:
            if target == rule or target.startswith(f"{rule}."):
                violations.append(f"{rel_posix} 禁止导入 {target}（命中规则：{rule}）")
                break
    return violations


def test_app_module_boundaries() -> None:
    violations: list[str] = []
    for py_file in sorted(APP_ROOT.rglob("*.py")):
        rel_posix = py_file.relative_to(ROOT).as_posix()
        source = py_file.read_text(encoding="utf-8-sig")
        violations.extend(check_forbidden(rel_posix, source))
    assert not violations, "发现违反模块单向依赖边界的导入：\n" + "\n".join(violations)


def test_boundary_detector_catches_reverse_imports() -> None:
    assert check_forbidden("app/services/kb.py", "from app.courses import service\n")
    assert check_forbidden("app/api/service_routes.py", "from app.api import routes\n")
    assert check_forbidden("app/api/routes.py", "import app.infrastructure.database\n")
    assert not check_forbidden("app/courses/service.py", "from app.services import reducer\n")
