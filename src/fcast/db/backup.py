"""Database backups with SQLite's online backup API (safe while the app is writing)."""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "fcast-"
SUFFIX = ".db"


@dataclass(frozen=True)
class BackupFile:
    path: Path
    created_at: datetime  # from the file name (UTC)
    size: int


def _name(now: datetime) -> str:
    return f"{PREFIX}{now.astimezone(UTC):%Y-%m-%d_%H%M%S}{SUFFIX}"


def list_backups(backup_dir: Path) -> list[BackupFile]:
    """Backups in the directory, newest first."""
    if not backup_dir.is_dir():
        return []
    found = []
    for path in backup_dir.glob(f"{PREFIX}*{SUFFIX}"):
        try:
            created = datetime.strptime(path.name, f"{PREFIX}%Y-%m-%d_%H%M%S{SUFFIX}")
        except ValueError:
            continue  # not ours
        found.append(BackupFile(path, created.replace(tzinfo=UTC), path.stat().st_size))
    return sorted(found, key=lambda b: b.created_at, reverse=True)


def prune(backup_dir: Path, keep: int) -> list[Path]:
    """Delete all but the `keep` newest backups; returns the deleted files."""
    removed = []
    for old in list_backups(backup_dir)[keep:]:
        old.path.unlink()
        removed.append(old.path)
    return removed


def backup_database(db_path: Path, backup_dir: Path, now: datetime, keep: int) -> Path:
    """Copy the database into `backup_dir` and keep the `keep` newest copies."""
    if not db_path.is_file():
        raise FileNotFoundError(f"database {db_path} not found")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / _name(now)
    partial = target.with_name(target.name + ".part")
    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    partial.replace(target)  # only complete backups get the final name
    removed = prune(backup_dir, keep)
    logger.info("database backup %s (%d old backups removed)", target.name, len(removed))
    return target
