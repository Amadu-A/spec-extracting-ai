# tests/test_architecture.py
"""AST-проверки направления зависимостей и отсутствие небезопасного frontend HTML."""

import ast
from pathlib import Path


def test_dependency_direction():
    """Domain/application не знают HTTP/SQL/брокер, presentation не создаёт адаптеры."""
    forbidden = {
        "domain": (
            "fastapi",
            "sqlalchemy",
            "httpx",
            "aio_pika",
            "app.infrastructure",
            "app.application",
        ),
        "application": (
            "fastapi",
            "sqlalchemy",
            "httpx",
            "aio_pika",
            "app.infrastructure",
            "app.presentation",
        ),
        "presentation": ("sqlalchemy", "app.infrastructure"),
    }
    for layer, prefixes in forbidden.items():
        for path in (Path("app") / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                )
                assert not any(module.startswith(prefixes) for module in modules), str(path)


def test_source_documentation_and_safe_dom():
    """Русские docstrings и BEM/module структура обязательны для поддерживаемого кода."""
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf8"))
        assert ast.get_docstring(tree), str(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assert ast.get_docstring(node), f"{path}:{node.lineno}"
    for path in Path("app/web/static/js").rglob("*.js"):
        assert "innerHTML" not in path.read_text(encoding="utf8"), str(path)
    for path in Path("app/web/templates").rglob("*.html"):
        assert "onclick=" not in path.read_text(encoding="utf8"), str(path)
