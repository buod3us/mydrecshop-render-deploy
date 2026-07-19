"""Fail when local deployment secrets appear in release files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_NAMES = {
    "BOT_TOKEN",
    "ADMIN_IDS",
    "BINANCE_ID",
    "DATABASE_BOOTSTRAP_KEY",
    "BACKUP_ENCRYPTION_KEY",
}
TELEGRAM_TOKEN_PATTERN = re.compile(rb"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def main() -> None:
    tracked_output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    candidates = [
        ROOT / path.decode("utf-8")
        for path in tracked_output.split(b"\0")
        if path and (ROOT / path.decode("utf-8")).is_file()
    ]

    local_values = dotenv_values(ROOT / ".env") if (ROOT / ".env").is_file() else {}
    secrets = {
        name: str(value).encode("utf-8")
        for name, value in local_values.items()
        if name in SENSITIVE_NAMES and value and len(str(value)) >= 6
    }
    findings: list[str] = []
    for path in candidates:
        content = path.read_bytes()
        if TELEGRAM_TOKEN_PATTERN.search(content):
            findings.append(f"{path.relative_to(ROOT)}: Telegram token pattern")
        for name, secret in secrets.items():
            if secret in content:
                findings.append(f"{path.relative_to(ROOT)}: local {name} value")

    if findings:
        raise SystemExit("Release secret scan failed:\n- " + "\n- ".join(findings))
    print(f"Release secret scan passed for {len(candidates)} files.")


if __name__ == "__main__":
    main()
