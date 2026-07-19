"""Authenticated one-time database bootstrap for a new persistent disk."""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import PROJECT_ROOT

BOOTSTRAP_KEY_ENV = "DATABASE_BOOTSTRAP_KEY"
BOOTSTRAP_ENABLED_ENV = "DATABASE_BOOTSTRAP_ENABLED"
DEFAULT_ENCRYPTED_SNAPSHOT = PROJECT_ROOT / "deploy" / "bootstrap" / "shop.db.fernet"
MAX_ENCRYPTED_SNAPSHOT_BYTES = 512 * 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"


class DatabaseBootstrapError(RuntimeError):
    """Raised when an encrypted deployment snapshot cannot be restored safely."""


def bootstrap_database_from_encrypted_snapshot(
    database_path: str | Path,
    *,
    encrypted_snapshot: str | Path = DEFAULT_ENCRYPTED_SNAPSHOT,
    key: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Restore a missing SQLite file once, without ever overwriting live data."""

    destination = Path(database_path)
    if destination.exists():
        return False

    bootstrap_enabled = enabled
    if bootstrap_enabled is None:
        bootstrap_enabled = os.getenv(BOOTSTRAP_ENABLED_ENV, "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    snapshot = Path(encrypted_snapshot)
    if not snapshot.is_file():
        if bootstrap_enabled:
            raise DatabaseBootstrapError(
                "persistent database is missing and encrypted bootstrap snapshot is missing"
            )
        return False
    if not bootstrap_enabled:
        raise DatabaseBootstrapError(
            "persistent database is missing and encrypted bootstrap is disabled"
        )
    if snapshot.stat().st_size > MAX_ENCRYPTED_SNAPSHOT_BYTES:
        raise DatabaseBootstrapError("encrypted database snapshot is unexpectedly large")

    marker = destination.with_name(f".{destination.name}.bootstrap-complete")
    if marker.exists():
        raise DatabaseBootstrapError(
            "database bootstrap was already completed; refusing to restore a stale snapshot"
        )

    secret = (key if key is not None else os.getenv(BOOTSTRAP_KEY_ENV, "")).strip()
    if not secret:
        raise DatabaseBootstrapError(
            f"{BOOTSTRAP_KEY_ENV} is required to initialize the new persistent disk"
        )
    try:
        decrypted = Fernet(secret.encode("ascii")).decrypt(snapshot.read_bytes())
    except (InvalidToken, UnicodeEncodeError, ValueError) as exc:
        raise DatabaseBootstrapError("encrypted database snapshot authentication failed") from exc
    if not decrypted.startswith(SQLITE_HEADER):
        raise DatabaseBootstrapError("decrypted snapshot is not a SQLite database")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(decrypted)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        _validate_sqlite_snapshot(temporary)
        if destination.exists():
            return False
        os.replace(temporary, destination)
        marker.write_text(
            hashlib.sha256(snapshot.read_bytes()).hexdigest() + "\n",
            encoding="ascii",
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return True


def _validate_sqlite_snapshot(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseBootstrapError("decrypted SQLite snapshot cannot be opened") from exc
    if integrity != ("ok",) or foreign_key_errors:
        raise DatabaseBootstrapError("decrypted SQLite snapshot failed integrity checks")
