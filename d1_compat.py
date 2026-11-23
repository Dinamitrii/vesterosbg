"""D1 compatibility layer.

The production Worker stores data in Cloudflare D1 (binding ``DB``), while local
development keeps using a real ``sqlite3`` file.  Flask views are synchronous and
call ``conn.execute(...)`` / ``conn.executescript(...)`` with the sqlite3 DB-API.
D1 exposes an async JS API, but the Cloudflare WSGI adapter runs the WSGI app
synchronously inside the async ``fetch`` handler using Pyodide's ``run_sync``
(JSPI stack switching).  That makes it possible to call the D1 binding from the
synchronous Flask code.

This module implements a sqlite3-like ``D1Connection`` / ``D1Cursor`` / ``D1Row``
on top of the raw JS ``env.DB`` binding.  It is only used inside the Worker
runtime; locally ``app.db()`` returns a plain ``sqlite3`` connection.
"""

from __future__ import annotations

import sqlite3


def _returns_rows(sql: str) -> bool:
    """True when a statement produces a result set (SELECT/PRAGMA/RETURNING)."""
    head = sql.lstrip().upper()
    return head.startswith(("SELECT", "PRAGMA", "WITH")) or " RETURNING " in head


def _to_py(value):
    """Convert a Pyodide JsProxy to native Python objects (best effort)."""
    if value is None:
        return None
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        try:
            return to_py()
        except Exception:  # noqa: BLE001 - fall back to the raw proxy
            return value
    return value


def _map_error(exc: Exception) -> Exception:
    """Translate a D1/JS error into the sqlite3 exception the app expects."""
    message = str(exc)
    low = message.lower()
    if "unique" in low or "constraint" in low or "primary key" in low:
        return sqlite3.IntegrityError(message)
    return sqlite3.OperationalError(message)


class D1Row(dict):
    """A row that supports both ``row["col"]`` and positional ``row[0]`` access."""

    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._columns = list(columns)

    def __getitem__(self, key):
        if isinstance(key, int):
            return dict.__getitem__(self, self._columns[key])
        return dict.__getitem__(self, key)


class D1Cursor:
    """Minimal DB-API cursor over an already-materialized result set."""

    def __init__(self, rows, rowcount=0, lastrowid=None):
        self._rows = rows
        self._pos = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None

    def fetchmany(self, size=1):
        rows = self._rows[self._pos:self._pos + size]
        self._pos += len(rows)
        return rows

    def __iter__(self):
        return iter(self._rows[self._pos:])


class D1Connection:
    """sqlite3-like connection backed by a Cloudflare D1 database binding."""

    def __init__(self, raw_env):
        db = getattr(raw_env, "DB", None)
        if db is None:
            raise RuntimeError("D1 binding 'DB' is not configured on the Worker env")
        self._db = db

    def _run(self, sql, params=()):
        from pyodide.ffi import run_sync
        from workers.rpc import python_to_rpc, python_from_rpc

        stmt = self._db.prepare(sql)
        if params:
            js_params = [python_to_rpc(param) for param in params]
            stmt = stmt.bind(*js_params)
        try:
            pending = stmt.all() if _returns_rows(sql) else stmt.run()
            result = python_from_rpc(run_sync(pending))
        except Exception as exc:  # noqa: BLE001 - normalize to sqlite3 errors
            raise _map_error(exc) from exc

        if not isinstance(result, dict):
            result = {}
        raw_rows = result.get("results") or []
        meta = result.get("meta") or {}
        columns = list(raw_rows[0].keys()) if raw_rows else []
        rows = [D1Row(columns, [row.get(col) for col in columns]) for row in raw_rows]
        last_row_id = meta.get("last_row_id")
        if last_row_id is None:
            last_row_id = meta.get("lastRowId")
        changes = meta.get("changes", 0) or 0
        return D1Cursor(rows, changes, last_row_id)

    def execute(self, sql, params=()):
        return self._run(sql, params)

    def executemany(self, sql, seq_params):
        last = None
        for params in seq_params:
            last = self._run(sql, params)
        return last if last is not None else D1Cursor([])

    def executescript(self, script):
        # D1 cannot run multiple statements in one prepare(); split on ';' and
        # execute each one.  The app's schema script has no ';' inside literals.
        statements = [part.strip() for part in script.split(";") if part.strip()]
        last = None
        for statement in statements:
            last = self._run(statement)
        return last if last is not None else D1Cursor([])

    def commit(self):  # D1 auto-commits each statement
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        return False


def get_raw_env(worker_env):
    """Unwrap the ``workers`` env wrapper to the raw JS env object.

    The per-request env stored by the WSGI adapter (``environ["workers.env"]``)
    is preferred because it is the authoritative env (includes secret bindings).
    Otherwise fall back to the global ``workers`` env (its raw object is on
    ``_env``).
    """
    try:
        from flask import request as _request

        wrapped = _request.environ.get("workers.env")
        if wrapped is not None:
            return getattr(wrapped, "_env", wrapped)
    except Exception:  # noqa: BLE001 - no request context / no flask
        pass
    if worker_env is not None:
        raw = getattr(worker_env, "_env", None)
        if raw is not None:
            return raw
    return None