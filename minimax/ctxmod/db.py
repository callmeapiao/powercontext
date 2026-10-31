"""seekdb-backed store for engineering materials.

The store keeps three families of engineering material that an AI assistant
needs before it can change code:

``spec``
    Specification entries and design decisions, each with an explicit status
    (``active`` / ``superseded`` / ``draft``) and an optional ``supersedes``
    link to the entry it replaces.

``code``
    Source files and the symbols extracted from them.

``test``
    Test files and the individual test cases inside them.

Everything is stored in a real seekdb (OceanBase's AI-native search engine)
running in embedded mode through ``pylibseekdb``, so no database server is
required.  Retrieval runs as SQL against that database and the rows it
returns are the only source of the development context handed to the model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pylibseekdb as seekdb

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS spec (
        spec_id      VARCHAR(64)  NOT NULL,
        version      VARCHAR(32)  NOT NULL,
        title        VARCHAR(256) NOT NULL,
        kind         VARCHAR(32)  NOT NULL,
        status       VARCHAR(32)  NOT NULL,
        supersedes   VARCHAR(64),
        superseded_by VARCHAR(64),
        scope        VARCHAR(256),
        body         TEXT,
        source_path  VARCHAR(512),
        content_hash VARCHAR(64),
        imported_at  VARCHAR(32),
        PRIMARY KEY (spec_id, version)
    ) ORGANIZATION = HEAP
    """,
    """
    CREATE TABLE IF NOT EXISTS code_file (
        path         VARCHAR(512) NOT NULL,
        language     VARCHAR(32),
        content      TEXT,
        content_hash VARCHAR(64),
        PRIMARY KEY (path)
    ) ORGANIZATION = HEAP
    """,
    """
    CREATE TABLE IF NOT EXISTS code_symbol (
        path        VARCHAR(512) NOT NULL,
        symbol      VARCHAR(128) NOT NULL,
        kind        VARCHAR(32),
        signature   VARCHAR(256),
        body        TEXT,
        line_start  INT,
        PRIMARY KEY (path, symbol)
    ) ORGANIZATION = HEAP
    """,
    """
    CREATE TABLE IF NOT EXISTS test_case (
        path       VARCHAR(512) NOT NULL,
        test_name  VARCHAR(256) NOT NULL,
        kind       VARCHAR(32),
        body       TEXT,
        PRIMARY KEY (path, test_name)
    ) ORGANIZATION = HEAP
    """,
]


@dataclass
class MaterialStore:
    """Thin, explicit wrapper over the embedded seekdb runtime."""

    db_dir: Path
    database: str = "ctxmod"
    _instance: Any = field(default=None, init=False, repr=False)
    _conn: Any = field(default=None, init=False, repr=False)
    query_log: list[str] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------ setup
    def open(self) -> MaterialStore:
        """Start the embedded runtime and make sure the schema exists."""

        if self._instance is not None:
            return self
        self.db_dir = Path(self.db_dir).resolve()
        self.db_dir.parent.mkdir(parents=True, exist_ok=True)
        self._instance = seekdb.open(db_dir=str(self.db_dir))
        self._conn = self._connect_without_database()
        cursor = self._conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        cursor.close()
        self._conn.close()
        self._conn = self._instance.connect(database=self.database, autocommit=True)
        cursor = self._conn.cursor()
        for statement in SCHEMA:
            cursor.execute(statement)
        try:
            cursor.execute("ALTER TABLE spec ADD COLUMN superseded_by VARCHAR(64)")
        except Exception:  # noqa: BLE001 - column already present
            pass
        cursor.close()
        return self

    def _connect_without_database(self) -> Any:
        conn = self._instance.connect(autocommit=True)
        return conn

    def close(self) -> None:
        """Release the runtime handles."""

        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        if self._instance is not None:
            self._instance.close()
            self._instance = None

    def __enter__(self) -> MaterialStore:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------- exec
    def execute(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        """Run one statement and return the rows it produced.

        ``pylibseekdb`` cursors only accept a complete statement, so parameter
        placeholders are rendered into safe SQL literals before the call.
        """

        statement = _interpolate(sql, tuple(params))
        cursor = self._conn.cursor()
        try:
            cursor.execute(statement)
            try:
                rows = cursor.fetchall()
            except Exception:  # noqa: BLE001 - statements without a result set
                rows = []
        finally:
            cursor.close()
        self.query_log.append(_compact(statement))
        return rows

    def execute_many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        """Run one statement for every parameter tuple."""

        for row in rows:
            self.execute(sql, row)

    # ------------------------------------------------------------- spec import
    def upsert_spec(
        self,
        *,
        spec_id: str,
        version: str,
        title: str,
        kind: str,
        status: str,
        supersedes: str | None,
        scope: str | None,
        body: str,
        source_path: str,
        imported_at: str,
    ) -> None:
        """Insert or replace one specification entry."""

        self.execute(
            """
            REPLACE INTO spec
                (spec_id, version, title, kind, status, supersedes, scope, body,
                 source_path, content_hash, imported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                spec_id,
                version,
                title,
                kind,
                status,
                supersedes,
                scope,
                body,
                source_path,
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
                imported_at,
            ),
        )

    def mark_superseded(self, spec_id: str, version: str, superseded_by: str) -> None:
        """Flag an entry as replaced by a newer one."""

        self.execute(
            "UPDATE spec SET status = 'superseded', superseded_by = ?"
            " WHERE spec_id = ? AND version = ?",
            (superseded_by, spec_id, version),
        )

    def specs(self, status: str | None = None) -> list[dict]:
        """Return specification rows, optionally filtered by status."""

        if status:
            rows = self.execute(
                "SELECT spec_id, version, title, kind, status, supersedes, superseded_by, scope,"
                " body, source_path, content_hash FROM spec WHERE status = ? ORDER BY spec_id, version",
                (status,),
            )
        else:
            rows = self.execute(
                "SELECT spec_id, version, title, kind, status, supersedes, superseded_by, scope,"
                " body, source_path, content_hash FROM spec ORDER BY spec_id, version"
            )
        return [dict(zip(_SPEC_COLUMNS, row)) for row in rows]

    # ----------------------------------------------------------- code / tests
    def upsert_code_file(self, path: str, language: str, content: str) -> None:
        """Insert or replace one source file."""

        self.execute(
            "REPLACE INTO code_file (path, language, content, content_hash) VALUES (?,?,?,?)",
            (path, language, content, hashlib.sha256(content.encode("utf-8")).hexdigest()),
        )

    def upsert_symbol(
        self, path: str, symbol: str, kind: str, signature: str, body: str, line_start: int
    ) -> None:
        """Insert or replace one code symbol."""

        self.execute(
            "REPLACE INTO code_symbol (path, symbol, kind, signature, body, line_start)"
            " VALUES (?,?,?,?,?,?)",
            (path, symbol, kind, signature, body, line_start),
        )

    def upsert_test_case(self, path: str, test_name: str, kind: str, body: str) -> None:
        """Insert or replace one test case."""

        self.execute(
            "REPLACE INTO test_case (path, test_name, kind, body) VALUES (?,?,?,?)",
            (path, test_name, kind, body),
        )

    def code_files(self) -> list[dict]:
        """Return every imported source file."""

        rows = self.execute("SELECT path, language, content, content_hash FROM code_file ORDER BY path")
        return [dict(zip(_CODE_FILE_COLUMNS, row)) for row in rows]

    def symbols(self) -> list[dict]:
        """Return every imported symbol."""

        rows = self.execute(
            "SELECT path, symbol, kind, signature, body, line_start FROM code_symbol ORDER BY path, symbol"
        )
        return [dict(zip(_SYMBOL_COLUMNS, row)) for row in rows]

    def test_cases(self) -> list[dict]:
        """Return every imported test case."""

        rows = self.execute("SELECT path, test_name, kind, body FROM test_case ORDER BY path, test_name")
        return [dict(zip(_TEST_COLUMNS, row)) for row in rows]

    # -------------------------------------------------------------- retrieval
    def search_specs(self, keywords: list[str], limit: int = 8) -> list[dict]:
        """Rank specification entries against the task keywords using SQL scoring."""

        if not keywords:
            return []
        scored = self.execute(
            """
            SELECT spec_id, version, title, kind, status, supersedes, superseded_by, scope, body,
                   source_path,
                   (CASE
                      WHEN title LIKE ? THEN 100 ELSE 0 END
                    + CASE WHEN body LIKE ? THEN 60 ELSE 0 END
                    + CASE WHEN scope LIKE ? THEN 40 ELSE 0 END
                    + CASE WHEN spec_id LIKE ? THEN 50 ELSE 0 END
                   ) AS score
            FROM spec
            WHERE title LIKE ? OR body LIKE ? OR scope LIKE ? OR spec_id LIKE ?
            ORDER BY score DESC, spec_id ASC
            LIMIT ?
            """,
            (
                f"%{keywords[0]}%", f"%{keywords[0]}%", f"%{keywords[0]}%", f"%{keywords[0]}%",
                f"%{keywords[0]}%", f"%{keywords[0]}%", f"%{keywords[0]}%", f"%{keywords[0]}%",
                limit,
            ),
        )
        return [dict(zip(_SPEC_SEARCH_COLUMNS, row)) for row in scored]

    def search_symbols(self, keywords: list[str], limit: int = 8) -> list[dict]:
        """Rank code symbols against the task keywords using SQL scoring."""

        if not keywords:
            return []
        rows = self.execute(
            """
            SELECT path, symbol, kind, signature, body, line_start,
                   (CASE
                      WHEN symbol LIKE ? THEN 100 ELSE 0 END
                    + CASE WHEN signature LIKE ? THEN 60 ELSE 0 END
                    + CASE WHEN body LIKE ? THEN 30 ELSE 0 END
                   ) AS score
            FROM code_symbol
            WHERE symbol LIKE ? OR signature LIKE ? OR body LIKE ?
            ORDER BY score DESC, path ASC
            LIMIT ?
            """,
            (
                f"%{keywords[0]}%", f"%{keywords[0]}%", f"%{keywords[0]}%",
                f"%{keywords[0]}%", f"%{keywords[0]}%", f"%{keywords[0]}%",
                limit,
            ),
        )
        return [dict(zip(_SYMBOL_SEARCH_COLUMNS, row)) for row in rows]

    def search_tests(self, keywords: list[str], limit: int = 8) -> list[dict]:
        """Rank test cases against the task keywords using SQL scoring."""

        if not keywords:
            return []
        rows = self.execute(
            """
            SELECT path, test_name, kind, body,
                   (CASE
                      WHEN test_name LIKE ? THEN 100 ELSE 0 END
                    + CASE WHEN body LIKE ? THEN 40 ELSE 0 END
                   ) AS score
            FROM test_case
            WHERE test_name LIKE ? OR body LIKE ?
            ORDER BY score DESC, path ASC
            LIMIT ?
            """,
            (
                f"%{keywords[0]}%", f"%{keywords[0]}%",
                f"%{keywords[0]}%", f"%{keywords[0]}%",
                limit,
            ),
        )
        return [dict(zip(_TEST_SEARCH_COLUMNS, row)) for row in rows]

    # ----------------------------------------------------------------- report
    def stats(self) -> dict:
        """Return row counts per material family."""

        return {
            "spec": self.execute("SELECT COUNT(*) FROM spec")[0][0],
            "spec_active": self.execute("SELECT COUNT(*) FROM spec WHERE status='active'")[0][0],
            "spec_superseded": self.execute("SELECT COUNT(*) FROM spec WHERE status='superseded'")[0][0],
            "code_file": self.execute("SELECT COUNT(*) FROM code_file")[0][0],
            "code_symbol": self.execute("SELECT COUNT(*) FROM code_symbol")[0][0],
            "test_case": self.execute("SELECT COUNT(*) FROM test_case")[0][0],
        }

    def dump(self) -> dict:
        """Return a JSON-serialisable snapshot of the whole store."""

        return {
            "specs": self.specs(),
            "code_files": self.code_files(),
            "symbols": self.symbols(),
            "test_cases": self.test_cases(),
        }


_SPEC_COLUMNS = (
    "spec_id", "version", "title", "kind", "status", "supersedes", "superseded_by", "scope",
    "body", "source_path", "content_hash",
)
_SPEC_SEARCH_COLUMNS = _SPEC_COLUMNS + ("score",)
_CODE_FILE_COLUMNS = ("path", "language", "content", "content_hash")
_SYMBOL_COLUMNS = ("path", "symbol", "kind", "signature", "body", "line_start")
_SYMBOL_SEARCH_COLUMNS = _SYMBOL_COLUMNS + ("score",)
_TEST_COLUMNS = ("path", "test_name", "kind", "body")
_TEST_SEARCH_COLUMNS = _TEST_COLUMNS + ("score",)


def _compact(sql: str) -> str:
    """Collapse a statement into one readable line for the query log."""

    return " ".join(sql.split())


def _literal(value: Any) -> str:
    """Render one value as a safe SQL literal."""

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return repr(value)
    text = str(value)
    text = (
        text.replace("\\", "\\\\")
        .replace("'", "''")
        .replace("\x00", "\\0")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x1a", "\\Z")
    )
    return f"'{text}'"


def _interpolate(sql: str, params: tuple[Any, ...]) -> str:
    """Replace every ``?`` placeholder with a rendered literal."""

    if not params:
        return sql
    out: list[str] = []
    index = 0
    for char in sql:
        if char == "?" and index < len(params):
            out.append(_literal(params[index]))
            index += 1
        else:
            out.append(char)
    if index != len(params):
        raise ValueError(f"statement expects {index} parameters, got {len(params)}")
    return "".join(out)
