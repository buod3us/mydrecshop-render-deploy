"""Create an authenticated encrypted SQLite snapshot for a new Render disk.

The encryption key is stored under ``data/`` (which is git-ignored) and is never
printed. Copy that key into Render's ``DATABASE_BOOTSTRAP_KEY`` secret.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from mydrecshop.db import Database


def _online_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(source, timeout=10)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.execute("PRAGMA busy_timeout = 10000")
        source_connection.backup(target_connection, pages=256, sleep=0.05)
    finally:
        target_connection.close()
        source_connection.close()


async def _migrate_and_validate(path: Path) -> None:
    database = Database(path)
    try:
        await database.initialize()
        await database.cleanup_expired_orders()
    finally:
        await database.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if integrity != ("ok",) or foreign_key_errors:
        raise RuntimeError("release database failed SQLite integrity checks")


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        key = path.read_bytes().strip()
        Fernet(key)
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key + b"\n")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return key


async def prepare(source: Path, output: Path, key_path: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    key = _load_or_create_key(key_path)

    with tempfile.TemporaryDirectory(prefix="mydrecshop-release-") as temporary_dir:
        snapshot = Path(temporary_dir) / "shop.db"
        await asyncio.to_thread(_online_backup, source, snapshot)
        await _migrate_and_validate(snapshot)
        plaintext = snapshot.read_bytes()

    encrypted = Fernet(key).encrypt(plaintext)
    temporary_output = output.with_suffix(output.suffix + ".tmp")
    temporary_output.write_bytes(encrypted)
    os.replace(temporary_output, output)
    digest = hashlib.sha256(encrypted).hexdigest()
    print(f"Encrypted snapshot ready: {output} ({len(encrypted)} bytes, sha256={digest})")
    print(f"Bootstrap key stored locally: {key_path} (value intentionally hidden)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/shop.db"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deploy/bootstrap/shop.db.fernet"),
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path("data/deploy-bootstrap.key"),
    )
    arguments = parser.parse_args()
    asyncio.run(
        prepare(
            arguments.source.resolve(),
            arguments.output.resolve(),
            arguments.key_file.resolve(),
        )
    )


if __name__ == "__main__":
    main()
