from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from mydrecshop.bootstrap import (
    BOOTSTRAP_ENABLED_ENV,
    DatabaseBootstrapError,
    bootstrap_database_from_encrypted_snapshot,
)


def _sqlite_bytes(path: Path) -> bytes:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('preserved')")
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()


def test_encrypted_snapshot_initializes_missing_database_once(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    plaintext = _sqlite_bytes(source)
    key = Fernet.generate_key()
    encrypted = tmp_path / "shop.db.fernet"
    encrypted.write_bytes(Fernet(key).encrypt(plaintext))
    destination = tmp_path / "disk" / "shop.db"

    assert bootstrap_database_from_encrypted_snapshot(
        destination,
        encrypted_snapshot=encrypted,
        key=key.decode("ascii"),
        enabled=True,
    )
    connection = sqlite3.connect(destination)
    try:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("preserved",)
    finally:
        connection.close()

    destination.write_bytes(b"do-not-overwrite")
    assert not bootstrap_database_from_encrypted_snapshot(
        destination,
        encrypted_snapshot=encrypted,
        key=key.decode("ascii"),
        enabled=True,
    )
    assert destination.read_bytes() == b"do-not-overwrite"


def test_snapshot_requires_valid_secret_and_sqlite_payload(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    encrypted = tmp_path / "shop.db.fernet"
    encrypted.write_bytes(Fernet(key).encrypt(b"not a database"))
    destination = tmp_path / "shop.db"

    with pytest.raises(DatabaseBootstrapError, match="required"):
        bootstrap_database_from_encrypted_snapshot(
            destination,
            encrypted_snapshot=encrypted,
            key="",
            enabled=True,
        )
    with pytest.raises(DatabaseBootstrapError, match="authentication"):
        bootstrap_database_from_encrypted_snapshot(
            destination,
            encrypted_snapshot=encrypted,
            key=Fernet.generate_key().decode("ascii"),
            enabled=True,
        )
    with pytest.raises(DatabaseBootstrapError, match="not a SQLite"):
        bootstrap_database_from_encrypted_snapshot(
            destination,
            encrypted_snapshot=encrypted,
            key=key.decode("ascii"),
            enabled=True,
        )
    assert not destination.exists()


def test_existing_bootstrap_marker_blocks_stale_restore(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    key = Fernet.generate_key()
    encrypted = tmp_path / "shop.db.fernet"
    encrypted.write_bytes(Fernet(key).encrypt(_sqlite_bytes(source)))
    destination = tmp_path / "shop.db"
    destination.with_name(".shop.db.bootstrap-complete").write_text("old\n", encoding="ascii")

    with pytest.raises(DatabaseBootstrapError, match="already completed"):
        bootstrap_database_from_encrypted_snapshot(
            destination,
            encrypted_snapshot=encrypted,
            key=key.decode("ascii"),
            enabled=True,
        )


def test_missing_snapshot_is_allowed_when_bootstrap_is_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "shop.db"
    missing_snapshot = tmp_path / "missing.db.fernet"
    monkeypatch.delenv(BOOTSTRAP_ENABLED_ENV, raising=False)

    assert not bootstrap_database_from_encrypted_snapshot(
        destination,
        encrypted_snapshot=missing_snapshot,
    )
    assert not bootstrap_database_from_encrypted_snapshot(
        destination,
        encrypted_snapshot=missing_snapshot,
        enabled=False,
    )
    assert not destination.exists()


@pytest.mark.parametrize("enabled_value", ["1", "true", "YES", " on "])
def test_missing_snapshot_fails_closed_when_bootstrap_is_enabled_by_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled_value: str,
) -> None:
    destination = tmp_path / "shop.db"
    monkeypatch.setenv(BOOTSTRAP_ENABLED_ENV, enabled_value)

    with pytest.raises(DatabaseBootstrapError, match="snapshot is missing"):
        bootstrap_database_from_encrypted_snapshot(
            destination,
            encrypted_snapshot=tmp_path / "missing.db.fernet",
        )
    assert not destination.exists()


def test_explicit_enabled_overrides_disabled_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BOOTSTRAP_ENABLED_ENV, "false")

    with pytest.raises(DatabaseBootstrapError, match="snapshot is missing"):
        bootstrap_database_from_encrypted_snapshot(
            tmp_path / "shop.db",
            encrypted_snapshot=tmp_path / "missing.db.fernet",
            enabled=True,
        )


def test_existing_database_wins_without_snapshot_key_or_marker(tmp_path: Path) -> None:
    destination = tmp_path / "shop.db"
    original = _sqlite_bytes(destination)
    destination.with_name(".shop.db.bootstrap-complete").write_text("old\n", encoding="ascii")

    assert not bootstrap_database_from_encrypted_snapshot(
        destination,
        encrypted_snapshot=tmp_path / "missing.db.fernet",
        key="not-a-fernet-key",
        enabled=True,
    )
    assert destination.read_bytes() == original
