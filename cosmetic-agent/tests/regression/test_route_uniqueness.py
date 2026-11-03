"""Regression checks for duplicate FastAPI route registrations."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
API_SERVER = Path(__file__).parents[2] / "api" / "api_server.py"


def test_api_server_has_no_duplicate_literal_routes() -> None:
    tree = ast.parse(API_SERVER.read_text(encoding="utf-8"))
    routes: dict[tuple[str, str], list[str]] = defaultdict(list)

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "app"
                and decorator.func.attr in HTTP_METHODS
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                continue
            key = (decorator.func.attr.upper(), decorator.args[0].value)
            routes[key].append(f"{node.name}:{node.lineno}")

    duplicates = {key: handlers for key, handlers in routes.items() if len(handlers) > 1}
    assert duplicates == {}, f"duplicate FastAPI routes: {duplicates}"
