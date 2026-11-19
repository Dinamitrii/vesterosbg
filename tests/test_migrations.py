"""TDD: test the id_reg / admin_rights migration (0004).

RED -> write a failing test first; do not write production code before it.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

MIGRATION = Path(__file__).parent.parent / "migrations" / "0004_id_reg_and_admin_rights.sql"


def _run_migration(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(MIGRATION.read_text())
    conn.commit()
    conn.close()


def _tables(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# RED — the migration file is missing; tables should be created
# ---------------------------------------------------------------------------

def test_migration_file_exists():
    assert MIGRATION.is_file(), f"migration file missing: {MIGRATION}"


def test_id_reg_table_created():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        tables = _tables(str(db))
        assert "id_reg" in tables, f"id_reg missing from {tables}"


def test_admin_rights_table_created():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        tables = _tables(str(db))
        assert "admin_rights" in tables, f"admin_rights missing from {tables}"


def test_id_reg_has_expected_columns():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(id_reg)").fetchall()}
        conn.close()
        for expected in ["id", "owner_id", "reg_type", "reg_value", "is_active", "created_at"]:
            assert expected in cols, f"id_reg missing column {expected}; got {cols}"


def test_admin_rights_has_expected_columns():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(admin_rights)").fetchall()}
        conn.close()
        for expected in ["id", "user_id", "right_name", "right_value", "created_at"]:
            assert expected in cols, f"admin_rights missing column {expected}; got {cols}"


def test_id_reg_primary_key_id():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='id_reg'"
        ).fetchone()
        conn.close()
        assert row is not None, "id_reg table not found"
        assert "PRIMARY KEY" in row[0], f"id_reg PRIMARY KEY missing: {row[0]}"


def _get_table_sql(db_path: str, table: str) -> str | None:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def test_admin_rights_primary_key_id():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        sql = _get_table_sql(str(db), "admin_rights")
        assert sql is not None, "admin_rights table not found"
        assert "PRIMARY KEY" in sql, f"admin_rights PRIMARY KEY missing: {sql}"


def test_migration_is_idempotent():
    """Running the migration twice must not create duplicate tables."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        _run_migration(str(db))  # run again
        tables = _tables(str(db))
        assert tables == {"admin_rights", "id_reg"}, f"duplicate tables? {tables}"


def test_indexes_created():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        conn = sqlite3.connect(db)
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        for idx in ["idx_id_reg_owner_active", "idx_id_reg_type_value"]:
            assert idx in indexes, f"missing index {idx} in {indexes}"
        for idx in ["idx_admin_rights_user", "idx_admin_rights_value"]:
            assert idx in indexes, f"missing index {idx} in {indexes}"


def test_id_reg_not_null_columns():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        _run_migration(str(db))
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "PRAGMA table_info(id_reg)"
        ).fetchall()
        conn.close()
        columns = {r[1]: r for r in rows}
        for col in ["owner_id", "reg_type", "reg_value", "is_active", "created_at"]:
            assert col in columns, f"id_reg missing column {col}"
            assert columns[col][3] == 1, f"id_reg column {col} not NOT NULL"
