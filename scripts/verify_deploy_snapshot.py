"""Restore and verify the real encrypted deployment snapshot without exposing its key."""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
import tempfile
from pathlib import Path

from mydrecshop.bootstrap import bootstrap_database_from_encrypted_snapshot
from mydrecshop.db import CURRENT_SCHEMA_VERSION


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("deploy/bootstrap/shop.db.fernet"),
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path("data/deploy-bootstrap.key"),
    )
    arguments = parser.parse_args()
    key = arguments.key_file.read_text(encoding="ascii").strip()

    with tempfile.TemporaryDirectory(prefix="mydrecshop-bootstrap-check-") as directory:
        restored_path = Path(directory) / "shop.db"
        restored = bootstrap_database_from_encrypted_snapshot(
            restored_path,
            encrypted_snapshot=arguments.snapshot,
            key=key,
            enabled=True,
        )
        if not restored:
            raise RuntimeError("snapshot was not restored")
        connection = sqlite3.connect(restored_path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            counts = {
                "products": connection.execute(
                    "SELECT COUNT(*) FROM products WHERE deleted_at IS NULL"
                ).fetchone()[0],
                "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "orders": connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                "inventory": connection.execute(
                    "SELECT COUNT(*) FROM product_inventory"
                ).fetchone()[0],
            }
        finally:
            connection.close()

    if integrity != ("ok",) or foreign_key_errors:
        raise RuntimeError("restored snapshot failed SQLite integrity checks")
    if version != CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"snapshot schema is {version}, expected {CURRENT_SCHEMA_VERSION}"
        )
    with contextlib.suppress(BrokenPipeError):
        print(
            "Snapshot verified: "
            f"schema={version}, products={counts['products']}, users={counts['users']}, "
            f"orders={counts['orders']}, inventory={counts['inventory']}"
        )


if __name__ == "__main__":
    main()
