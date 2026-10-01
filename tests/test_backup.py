"""Database backups: consistent copy, retention, CLI."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from fcast.cli import app
from fcast.config import get_settings
from fcast.db.backup import backup_database, list_backups, prune

T0 = datetime(2026, 10, 1, 1, 30, tzinfo=UTC)


def make_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("create table t (x integer)")
        connection.executemany("insert into t values (?)", [(i,) for i in range(100)])


def test_backup_is_a_complete_copy(tmp_path: Path) -> None:
    db = tmp_path / "fcast.db"
    make_db(db)
    target = backup_database(db, tmp_path / "backups", T0, keep=3)
    assert target.name == "fcast-2026-10-01_013000.db"
    with sqlite3.connect(target) as copy:
        assert copy.execute("select count(*) from t").fetchone() == (100,)
    assert not list((tmp_path / "backups").glob("*.part"))


def test_only_the_newest_backups_are_kept(tmp_path: Path) -> None:
    db = tmp_path / "fcast.db"
    make_db(db)
    backups = tmp_path / "backups"
    for day in range(5):
        backup_database(db, backups, T0 + timedelta(days=day), keep=3)
    (backups / "notes.txt").write_text("not a backup")
    kept = list_backups(backups)
    assert [b.created_at.day for b in kept] == [5, 4, 3]
    assert (backups / "notes.txt").exists()  # foreign files are left alone
    assert prune(backups, 1) == [kept[1].path, kept[2].path]


def test_db_backup_command(db_path: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    result = runner.invoke(app, ["db", "backup"])
    assert result.exit_code == 0, result.output
    assert "1 backups kept" in result.output
    assert len(list_backups(get_settings().backup_path)) == 1
    assert get_settings().backup_path == db_path.parent / "backups"
