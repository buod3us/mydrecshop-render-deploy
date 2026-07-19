from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_outgoing_bot_messages_never_enable_content_protection() -> None:
    protected_calls: list[str] = []
    for path in (PROJECT_ROOT / "src" / "mydrecshop").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "protect_content"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    protected_calls.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert protected_calls == []
