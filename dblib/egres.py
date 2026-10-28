"""egres backend (microbenchmark only).

egres (github.com/manishrjain/egres) speaks the Postgres wire protocol and
has one branching primitive, ``CREATE DATABASE <new> TEMPLATE <src>``: a
single manifest update that gives the new database its own LSM trees over
the source's SSTables (no data is copied and there is no copy-on-write).
Branches are therefore whole databases, as on MatrixOne and SeekDB:

    main branch      -> <db_name>
    branch "X"       -> <db_name>__X
    log row <id>     -> <db_name>_c<id>    (the branch as of that row)

egres has no commits, diff or merge, and its SQL is small (one table per
SELECT, ``column = value`` predicates only, no ORDER BY, joins, aggregates
other than count(*), ON CONFLICT, ALTER TABLE or catalog views; DDL runs
outside transactions; a schema-qualified name is read in the session's own
database, so one session cannot reach another database). Everything but
branch and delete is built here from that primitive, plain DML and
client-side comparison of whole tables, one connection per database:

    branch     CREATE DATABASE <branch> TEMPLATE <parent head or commit
               snapshot>, then CREATE DATABASE <db>_c<id> TEMPLATE <branch>
               as the fork point (the parent moves on), recorded as a
               "fork" row in the branch's _bb_commits
    commit     a _bb_commits row, then CREATE DATABASE <db>_c<id> TEMPLATE
               <branch> (the snapshot carries the row)
    log        SELECT FROM _bb_commits, sorted here
    diff       every table read on both sides (SELECT *), compared by
               primary key (as multisets without one)
    merge      three-way, per table: base = the fork (or last rebase)
               snapshot of whichever side descends from the other, ours =
               the target, theirs = the source, all read with SELECT *;
               theirs' changes applied to ours in one transaction (DELETE /
               INSERT / UPDATE by key); conflicts kept as "ours" or taken
               as "theirs" (a callable policy is treated as "ours"); the
               source's commits are copied into the target's log and a
               merge row + snapshot are recorded
    rebase     the upstream head is cloned (the branch's new base) and its
               changes since the branch's old base are merged into the
               branch three-way (git's sides: on conflict "ours" keeps the
               upstream, "theirs" the branch); a rebase row records the new
               base; lineage is not rewritten
    revert     inverse of (the row before the commit on that branch ->
               the commit), both read from their snapshots, applied to the
               branch, recorded as a commit
    reset      DROP DATABASE <branch>; CREATE DATABASE <branch> TEMPLATE
               <commit snapshot> (the snapshot's log ends at that commit)
    delete     DROP DATABASE <branch> (its snapshots are dropped with the
               run)

The table catalog (columns, types, primary key) is read once per run from
main's ``_bb_schema`` table, which the loader fills from the dump's CREATE
TABLE statements; egres has no information_schema and the
microbenchmark's branch verbs do not change the schema. The dump must use
what egres accepts: single-column inline PRIMARY KEY, no NOT NULL /
DEFAULT / REFERENCES, no numeric (db_setup/tpcc_schema_egres.sql).

Every database the run creates is appended to a registry file
(``$EGRES_STATE_DIR/<db_name>.dbs``) since the server cannot list its
databases; setup and cleanup drop what it names.

Configuration (environment): EGRES_HOST (127.0.0.1), EGRES_PORT (3210),
EGRES_USER (bench; the server has no authentication), EGRES_STATE_DIR
(/tmp/egres_bb), EGRES_DATA_DIR (the server's data directory, for storage
measurements; 0 without it).
"""

import collections
import os
import re
import threading
import time
from typing import Optional

import psycopg2
import psycopg2.extensions

from dblib.db_api import DBToolSuite, Ref
import dblib.result_collector as rc

EGRES_HOST = os.environ.get("EGRES_HOST", "127.0.0.1")
EGRES_PORT = int(os.environ.get("EGRES_PORT", "3210"))
EGRES_USER = os.environ.get("EGRES_USER", "bench")
EGRES_STATE_DIR = os.environ.get("EGRES_STATE_DIR", "/tmp/egres_bb")
EGRES_DATA_DIR = os.environ.get("EGRES_DATA_DIR", "")
DEFAULT_DB = "egres"

MAIN_BRANCH = "main"
_BRANCH_SEP = "__"
COMMITS_TABLE = "_bb_commits"
SCHEMA_TABLE = "_bb_schema"
META_PREFIX = "_bb_"
_LOG_COLS = ("id", "seq", "kind", "message", "branch", "parent", "base", "ts")
_COMMITS_DDL = (f"CREATE TABLE {COMMITS_TABLE} (id text PRIMARY KEY, seq bigint, kind text, "
                "message text, branch text, parent text, base text, ts bigint)")
_SCHEMA_DDL = f"CREATE TABLE {SCHEMA_TABLE} (name text PRIMARY KEY, ddl text)"


# ----------------------------------------------------------------------------
# Connections, names, registry
# ----------------------------------------------------------------------------


class _Connection(psycopg2.extensions.connection):
    """A psycopg2 connection that carries the table catalog, which
    util.db_helpers reads instead of information_schema."""

    catalog = None


def connect(db: str = DEFAULT_DB, catalog=None):
    conn = psycopg2.connect(host=EGRES_HOST, port=EGRES_PORT, dbname=db, user=EGRES_USER,
                            sslmode="disable", connect_timeout=30, connection_factory=_Connection)
    conn.autocommit = True
    conn.catalog = catalog
    return conn


def _x(conn, sql: str, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description is not None else None


def _safe(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not s:
        raise ValueError(f"invalid branch name {name!r}")
    return s


def branch_db(db_name: str, branch: str) -> str:
    return db_name if branch == MAIN_BRANCH else f"{db_name}{_BRANCH_SEP}{_safe(branch)}"


def snap_db(db_name: str, row_id: str) -> str:
    return f"{db_name}_c{row_id}"


def base_db(db_name: str, row_id: str) -> str:
    return f"{db_name}_b{row_id}"


_REG_LOCK = threading.Lock()


def _registry(db_name: str) -> str:
    return os.path.join(EGRES_STATE_DIR, f"{db_name}.dbs")


def _register(db_name: str, db: str) -> None:
    with _REG_LOCK:
        os.makedirs(EGRES_STATE_DIR, exist_ok=True)
        with open(_registry(db_name), "a") as f:
            f.write(db + "\n")


def _drop(conn, db: str) -> bool:
    try:
        _x(conn, f"DROP DATABASE {db}")
        return True
    except psycopg2.Error as e:
        if "does not exist" in str(e):
            return False
        raise


# ----------------------------------------------------------------------------
# Catalog
# ----------------------------------------------------------------------------

_UDT = {"smallint": "int2", "int2": "int2", "integer": "int4", "int": "int4", "int4": "int4",
        "bigint": "int8", "int8": "int8", "double precision": "float8", "float8": "float8",
        "float": "float8", "real": "float4", "float4": "float4", "boolean": "bool",
        "bool": "bool", "text": "text", "varchar": "varchar", "character varying": "varchar",
        "char": "bpchar", "character": "bpchar", "bpchar": "bpchar", "bytea": "bytea",
        "date": "date", "timestamp": "timestamp", "timestamptz": "timestamptz"}


def _split_top(body: str) -> list:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


class _Table:
    def __init__(self, name: str, columns: list, pk: Optional[str], ddl: str):
        self.name = name
        self.columns = columns    # [(name, udt, char_len)]
        self.pk = pk
        self.ddl = ddl
        self.names = [c[0] for c in columns]

    @classmethod
    def parse(cls, ddl: str) -> "_Table":
        m = re.match(r"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*)\)\s*;?\s*$", ddl,
                     re.IGNORECASE | re.DOTALL)
        if not m:
            raise ValueError(f"cannot parse CREATE TABLE: {ddl[:80]}")
        name, body = m.group(1).lower(), m.group(2)
        cols, pk = [], None
        for part in _split_top(body):
            if re.match(r"(primary|unique|foreign|constraint|check)\b", part, re.IGNORECASE):
                raise ValueError(f"egres takes no table-level constraint: {part}")
            cm = re.match(r"(\w+)\s+([a-zA-Z ]+?)\s*(?:\((\d+)\))?\s*(primary\s+key|unique)?\s*$", part,
                          re.IGNORECASE)
            if not cm:
                raise ValueError(f"cannot parse column definition: {part}")
            cname, ctype = cm.group(1).lower(), " ".join(cm.group(2).lower().split())
            udt = _UDT.get(ctype)
            if udt is None:
                raise ValueError(f"type {ctype!r} of {name}.{cname} is not mapped for egres")
            cols.append((cname, udt, int(cm.group(3)) if cm.group(3) else None))
            if cm.group(4) and cm.group(4).lower().startswith("primary"):
                pk = cname
        return cls(name, cols, pk, ddl)

    def key(self, row: tuple):
        return row[self.names.index(self.pk)] if self.pk else None


class _Catalog:
    """The interface util.db_helpers expects (tables, columns, pk_columns)."""

    def __init__(self, tables: dict):
        self._tables = tables

    def tables(self) -> list:
        return sorted(self._tables)

    def table(self, name: str) -> _Table:
        t = self._tables.get(name.lower())
        if t is None:
            raise ValueError(f"table {name} not in the egres catalog")
        return t

    def columns(self, table: str) -> list:
        return list(self.table(table).names)

    def pk_columns(self, table: str) -> list:
        t = self.table(table)
        return [t.pk] if t.pk else []


_CATALOGS: dict = {}
_CAT_LOCK = threading.Lock()


def _catalog(db_name: str) -> _Catalog:
    with _CAT_LOCK:
        if db_name not in _CATALOGS:
            conn = connect(db_name)
            try:
                rows = _x(conn, f"SELECT name, ddl FROM {SCHEMA_TABLE}") or []
            finally:
                conn.close()
            _CATALOGS[db_name] = _Catalog({n: _Table.parse(d) for n, d in rows})
        return _CATALOGS[db_name]


# ----------------------------------------------------------------------------
# Setup and cleanup (BackendManager)
# ----------------------------------------------------------------------------


def _statements(path: str):
    buf = []
    with open(path) as f:
        for line in f:
            if not buf and (not line.strip() or line.lstrip().startswith("--")):
                continue
            buf.append(line)
            if line.rstrip().endswith(";"):
                yield "".join(buf).strip().rstrip(";").strip()
                buf = []
    if "".join(buf).strip():
        yield "".join(buf).strip().rstrip(";").strip()


def drop_database(db_name: str) -> None:
    """Drop db_name and every database the registry names."""
    admin = connect()
    dropped = 0
    try:
        names = []
        if os.path.exists(_registry(db_name)):
            with open(_registry(db_name)) as f:
                names = [n.strip() for n in f if n.strip()]
        for db in reversed(list(dict.fromkeys(names + [db_name]))):
            dropped += _drop(admin, db)
        with _REG_LOCK:
            if os.path.exists(_registry(db_name)):
                os.remove(_registry(db_name))
        with _CAT_LOCK:
            _CATALOGS.pop(db_name, None)
        print(f"egres: dropped {dropped} databases of '{db_name}'")
    finally:
        admin.close()


def setup_database(db_name: str, sql_path: str = None) -> None:
    """(Re)create db_name and load sql_path: CREATE TABLE statements are run
    and recorded in _bb_schema; DROP TABLE lines are skipped (the database
    is new); every other statement runs as is."""
    drop_database(db_name)
    admin = connect()
    try:
        _x(admin, f"CREATE DATABASE {db_name}")
    finally:
        admin.close()
    conn = connect(db_name)
    try:
        _x(conn, _SCHEMA_DDL)
        _x(conn, _COMMITS_DDL)
        n = 0
        for stmt in _statements(sql_path) if sql_path else []:
            head = stmt.split(None, 2)[:2]
            if [h.upper() for h in head] == ["DROP", "TABLE"]:
                continue
            if [h.upper() for h in head] == ["CREATE", "TABLE"]:
                t = _Table.parse(stmt)
                _x(conn, stmt)
                _x(conn, f"INSERT INTO {SCHEMA_TABLE} (name, ddl) VALUES (%s, %s)", (t.name, stmt))
                n += 1
            else:
                _x(conn, stmt)
        print(f"egres: database {db_name} created, {n} tables loaded")
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# The suite
# ----------------------------------------------------------------------------


class EgresToolSuite(DBToolSuite):
    BACKEND_NAME = "egres"
    STORAGE_SCOPE = "server"
    SUPPORTS_COMMIT_REFS = True
    SCHEMA_DIFF_HALF = "simulated"
    SCHEMA_SKIP_TABLE_PREFIXES = (META_PREFIX,)
    IMPLEMENTATION = {
        "branch": "native",
        "commit": "simulated",
        "diff": "simulated",
        "log": "simulated",
        "merge": "simulated",
        "rebase": "simulated",
        "revert": "simulated",
        "reset": "simulated",
        "delete": "native",
        "commit_refs": "simulated",
    }
    IMPLEMENTATION_NOTES = {
        "branch": "CREATE DATABASE <branch> TEMPLATE <parent>, plus a TEMPLATE clone of the new branch as its "
                  "fork point (merge base) and a fork row in its _bb_commits",
        "commit": "a _bb_commits row, then CREATE DATABASE <db>_c<id> TEMPLATE <branch>",
        "diff": "whole tables read on both sides (SELECT *) and compared client-side by primary key",
        "log": "SELECT FROM _bb_commits, sorted client-side",
        "merge": "client-side three-way per table against the fork/rebase snapshot; DELETE/INSERT/UPDATE by "
                 "key in one transaction; callable policies treated as 'ours'",
        "rebase": "TEMPLATE clone of the upstream head as the new base; its changes since the old base merged "
                  "into the branch three-way",
        "revert": "inverse of (previous row's snapshot -> commit snapshot), applied by key, recorded as a commit",
        "reset": "DROP DATABASE <branch>; CREATE DATABASE <branch> TEMPLATE <commit snapshot>",
        "delete": "DROP DATABASE <branch>",
        "commit_refs": "branch@commit reads the commit's snapshot database",
    }

    @classmethod
    def get_default_connection_uri(cls) -> str:
        return cls.get_initial_connection_uri(DEFAULT_DB)

    @classmethod
    def get_initial_connection_uri(cls, db_name: str) -> str:
        return f"postgresql://{EGRES_USER}@{EGRES_HOST}:{EGRES_PORT}/{db_name}?sslmode=disable"

    @classmethod
    def init_for_bench(cls, collector: rc.ResultCollector, db_name: str,
                       default_branch_name: str = MAIN_BRANCH, measure_storage: bool = False):
        return cls(None, collector, db_name, default_branch_name, measure_storage)

    def __init__(self, connection, collector: rc.ResultCollector, db_name: str,
                 default_branch_name: str = MAIN_BRANCH, measure_storage: bool = False):
        super().__init__(connection, collector, measure_storage)
        self.db_name = db_name
        self.default_branch = default_branch_name
        self.catalog = _catalog(db_name)
        self._admin_conn = connect(DEFAULT_DB)
        self._conn_db = None
        if connection is None:
            self._connect_impl(Ref(default_branch_name))
            self._current_ref = Ref(default_branch_name)

    # -- names and connections -----------------------------------------------

    def _db(self, branch: str) -> str:
        return branch_db(self.db_name, branch)

    def _read_db(self, ref: Ref) -> str:
        """Database holding ``ref``: the branch, or the commit's snapshot."""
        return snap_db(self.db_name, ref.commit) if ref.commit else self._db(ref.branch)

    def _admin(self, sql: str) -> None:
        self._check_cancel()
        _x(self._admin_conn, sql)

    def _clone(self, src: str, dst: str) -> None:
        self._admin(f"CREATE DATABASE {dst} TEMPLATE {src}")
        _register(self.db_name, dst)

    def _x(self, conn, sql: str, args=None):
        self._check_cancel()
        return _x(conn, sql, args)

    def _close_conn(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self._conn_db = None

    def _connect_impl(self, ref: Ref) -> None:
        db = self._read_db(ref)
        self._close_conn()
        self.conn = connect(db, self.catalog)
        self._conn_db = db

    def _on(self, ref: Ref) -> str:
        """Point self.conn at the branch head; returns its database."""
        head = Ref(ref.branch)
        if self.conn is None or self._current_ref != head or getattr(self.conn, "closed", 0):
            self._connect_impl(head)
            self._current_ref = head
        return self._conn_db

    def _side(self, db: str):
        """self.conn when it is on ``db``, else a new connection (closed by
        _release)."""
        if self.conn is not None and self._conn_db == db and not self.conn.closed:
            return self.conn
        return connect(db, self.catalog)

    def _release(self, conn) -> None:
        if conn is not None and conn is not self.conn:
            conn.close()

    def close_connection(self) -> None:
        self._close_conn()
        try:
            self._admin_conn.close()
        except Exception:
            pass
        self._current_ref = None

    def list_branches(self) -> list:
        raise self._unsupported("list_branches", "egres cannot list its databases")

    def get_current_connection(self):
        return self.conn

    def _get_table_columns(self, table_name: str) -> list:
        t = self.catalog.table(table_name)
        return [(name, udt, "YES", char_len, None, None) for name, udt, char_len in t.columns]

    def _storage_bytes(self) -> int:
        if not EGRES_DATA_DIR:
            return 0
        total = 0
        for root, _, files in os.walk(EGRES_DATA_DIR):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    # -- bookkeeping ----------------------------------------------------------

    def _log(self, conn) -> list:
        rows = self._x(conn, f"SELECT {', '.join(_LOG_COLS)} FROM {COMMITS_TABLE}") or []
        return sorted((dict(zip(_LOG_COLS, r)) for r in rows), key=lambda r: r["seq"])

    def _add_row(self, conn, kind: str, message: str, branch: str, parent: str = None,
                 base: str = None, row_id: str = None, seq: int = None, ts: int = None) -> str:
        seq = seq or time.time_ns()
        row_id = row_id or f"{seq:x}"
        self._x(conn, f"INSERT INTO {COMMITS_TABLE} ({', '.join(_LOG_COLS)}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (row_id, seq, kind, message or "", branch, parent, base, ts or int(time.time() * 1000)))
        return row_id

    def _commit_row(self, conn, db: str, branch: str, kind: str, message: str, **kw) -> str:
        """A log row and the snapshot that holds the branch as of it."""
        row_id = self._add_row(conn, kind, message, branch, **kw)
        self._clone(db, snap_db(self.db_name, row_id))
        return row_id

    def _find(self, log: list, row_id: str) -> dict:
        for r in log:
            if r["id"] == row_id:
                return r
        raise ValueError(f"commit {row_id} not found")

    # -- table reads and comparison -------------------------------------------

    def _rows(self, conn, table: str) -> list:
        return self._x(conn, f"SELECT {', '.join(self.catalog.table(table).names)} FROM {table}") or []

    def _keyed(self, t: _Table, rows: list) -> dict:
        i = t.names.index(t.pk)
        return {r[i]: tuple(r) for r in rows}

    def _compare(self, t: _Table, rows_a: list, rows_b: list) -> tuple:
        """(added, deleted, modified) from a to b."""
        if t.pk:
            a, b = self._keyed(t, rows_a), self._keyed(t, rows_b)
            added = sum(1 for k in b if k not in a)
            deleted = sum(1 for k in a if k not in b)
            modified = sum(1 for k in a if k in b and a[k] != b[k])
            return added, deleted, modified
        ca, cb = collections.Counter(map(tuple, rows_a)), collections.Counter(map(tuple, rows_b))
        return sum((cb - ca).values()), sum((ca - cb).values()), 0

    # -- verbs ----------------------------------------------------------------

    def _branch_impl(self, name: str, from_ref: Ref) -> None:
        child = self._db(name)
        self._clone(self._read_db(from_ref), child)
        seq = time.time_ns()
        row_id = f"{seq:x}"
        fork = snap_db(self.db_name, row_id)
        self._clone(child, fork)
        conn = connect(child, self.catalog)
        try:
            self._add_row(conn, "fork", f"fork from {from_ref}", child, parent=from_ref.branch, base=fork,
                          row_id=row_id, seq=seq)
        finally:
            conn.close()

    def _commit_impl(self, ref: Ref, message: str) -> str:
        db = self._on(ref)
        return self._commit_row(self.conn, db, db, "commit", message)

    def _log_impl(self, ref: Ref, limit: int) -> list:
        conn = self._side(self._db(ref.branch))
        try:
            log = self._log(conn)
        finally:
            self._release(conn)
        if ref.commit:
            top = self._find(log, ref.commit)["seq"]
            log = [r for r in log if r["seq"] <= top]
        out = [{"commit_hash": r["id"], "kind": r["kind"], "committer": "egres", "email": "",
                "date": r["ts"], "message": r["message"]} for r in reversed(log)]
        return out[:limit] if limit else out

    def _diff_impl(self, ref_a: Ref, ref_b: Ref) -> dict:
        da, dbb = self._read_db(ref_a), self._read_db(ref_b)
        ca = self._side(da)
        cb = self._side(dbb) if dbb != da else ca
        try:
            out = []
            for name in self.catalog.tables():
                t = self.catalog.table(name)
                added, deleted, modified = self._compare(t, self._rows(ca, name), self._rows(cb, name))
                if added or deleted or modified:
                    out.append({"table_name": name, "rows_added": added, "rows_deleted": deleted,
                                "rows_modified": modified})
            return {"tables": out, "rows_added": sum(x["rows_added"] for x in out),
                    "rows_deleted": sum(x["rows_deleted"] for x in out),
                    "rows_modified": sum(x["rows_modified"] for x in out)}
        finally:
            if cb is not ca:
                self._release(cb)
            self._release(ca)

    def _schema_at(self, ref: Ref) -> dict:
        """{table: {column: type}} of ``ref`` from its _bb_schema, read
        through an untimed exec() as the base class does."""
        res = self.exec(lambda db: db.sql(f"SELECT name, ddl FROM {SCHEMA_TABLE}"), refs=[ref], timed=False,
                        raise_on_error=True)[0]
        out = {}
        for name, ddl in res.value or []:
            t = _Table.parse(ddl)
            out[t.name] = {c: u for c, u, _ in t.columns}
        return out

    @staticmethod
    def _base_row(log: list, parent: str) -> Optional[dict]:
        """The latest fork/rebase row of ``log`` whose parent is ``parent``."""
        rows = [r for r in log if r["kind"] in ("fork", "rebase") and r["parent"] == parent]
        return rows[-1] if rows else None

    def _apply(self, ours, t: _Table, deletes: list, inserts: list, updates: list) -> None:
        if deletes:
            for i in range(0, len(deletes), 200):
                chunk = deletes[i:i + 200]
                self._x(ours, f"DELETE FROM {t.name} WHERE " + " OR ".join(f"{t.pk} = %s" for _ in chunk), chunk)
        if inserts:
            for i in range(0, len(inserts), 200):
                chunk = inserts[i:i + 200]
                ph = "(" + ", ".join(["%s"] * len(t.names)) + ")"
                self._x(ours, f"INSERT INTO {t.name} ({', '.join(t.names)}) VALUES " + ", ".join([ph] * len(chunk)),
                        [v for row in chunk for v in row])
        rest = [c for c in t.names if c != t.pk]
        for row in updates:
            if not rest:
                continue
            vals = dict(zip(t.names, row))
            self._x(ours, f"UPDATE {t.name} SET " + ", ".join(f"{c} = %s" for c in rest) + f" WHERE {t.pk} = %s",
                    [vals[c] for c in rest] + [vals[t.pk]])

    def _three_way(self, ours, theirs, base, keep_ours: bool) -> dict:
        """Apply theirs' changes since base (None: no common ancestor, every
        row theirs has that ours lacks is new) onto ours."""
        info = {"conflicts": 0, "conflict_tables": [], "ours_changes": 0, "theirs_changes": 0,
                "merged_tables": [], "skipped_tables": {}, "two_way": base is None}
        plans = []
        for name in self.catalog.tables():
            t = self.catalog.table(name)
            T, O = self._rows(theirs, name), self._rows(ours, name)
            B = self._rows(base, name) if base is not None else None
            if not t.pk:
                new = collections.Counter(map(tuple, T)) - collections.Counter(map(tuple, B if B is not None else O))
                if new:
                    plans.append((t, [], list(new.elements()), []))
                    info["theirs_changes"] += sum(new.values())
                    info["merged_tables"].append(name)
                    info["skipped_tables"][name] = "no primary key: new rows only"
                continue
            tk, ok = self._keyed(t, T), self._keyed(t, O)
            bk = self._keyed(t, B) if B is not None else None
            deletes, inserts, updates, conflicts = [], [], [], 0
            if bk is None:
                changes = {k: v for k, v in tk.items() if k not in ok}
            else:
                changes = {k: tk.get(k) for k in set(tk) | set(bk) if tk.get(k) != bk.get(k)}
                info["ours_changes"] += sum(1 for k in set(ok) | set(bk) if ok.get(k) != bk.get(k))
            for k, tv in changes.items():
                ov, bv = ok.get(k), (bk.get(k) if bk is not None else None)
                if ov == tv:
                    continue
                if bk is not None and ov != bv:
                    conflicts += 1
                    if keep_ours:
                        continue
                if tv is None:
                    if ov is not None:
                        deletes.append(k)
                elif ov is None:
                    inserts.append(tv)
                else:
                    updates.append(tv)
            info["theirs_changes"] += len(changes)
            if conflicts:
                info["conflicts"] += conflicts
                info["conflict_tables"].append(name)
            if deletes or inserts or updates:
                plans.append((t, deletes, inserts, updates))
                info["merged_tables"].append(name)
        if plans:
            self._x(ours, "BEGIN")
            try:
                for t, d, i, u in plans:
                    self._apply(ours, t, d, i, u)
                self._x(ours, "COMMIT")
            except BaseException:
                try:
                    self._x(ours, "ROLLBACK")
                except Exception:
                    pass
                raise
        return info

    def _copy_log(self, ours, ours_log: list, their_log: list, max_seq: int = None) -> None:
        have = {r["id"] for r in ours_log}
        rows = [r for r in their_log if r["kind"] in ("commit", "merge") and r["id"] not in have
                and (max_seq is None or r["seq"] <= max_seq)]
        if rows:
            ph = "(" + ", ".join(["%s"] * len(_LOG_COLS)) + ")"
            self._x(ours, f"INSERT INTO {COMMITS_TABLE} ({', '.join(_LOG_COLS)}) VALUES " + ", ".join([ph] * len(rows)),
                    [r[c] for r in rows for c in _LOG_COLS])

    def _merge_impl(self, into: Ref, source: Ref, message: str, on_conflict="ours") -> dict:
        ours_db = self._on(into)
        theirs_db = self._read_db(source)
        if theirs_db == ours_db:
            raise ValueError(f"Cannot merge branch '{source.branch}' into itself")
        ours = self.conn
        theirs = connect(theirs_db, self.catalog)
        base = None
        try:
            our_log, their_log = self._log(ours), self._log(theirs)
            row = self._base_row(their_log, into.branch) or self._base_row(our_log, source.branch)
            if row is not None:
                base = connect(row["base"], self.catalog)
            keep_ours = callable(on_conflict) or on_conflict == "ours"
            info = self._three_way(ours, theirs, base, keep_ours)
            if info["conflicts"]:
                info["resolved"] = "ours" if callable(on_conflict) else on_conflict
            top = self._find(their_log, source.commit)["seq"] if source.commit else None
            self._copy_log(ours, our_log, their_log, max_seq=top)
            info["hash"] = self._commit_row(ours, ours_db, ours_db, "merge",
                                            message or f"Merge {source} into {into.branch}")
            info["fast_forward"] = info["ours_changes"] == 0 or info["theirs_changes"] == 0
            return info
        finally:
            theirs.close()
            if base is not None:
                base.close()

    def _rebase_impl(self, ref: Ref, onto: Ref, on_conflict="ours") -> dict:
        ours_db = self._on(ref)
        up_db = self._read_db(onto)
        if up_db == ours_db:
            raise ValueError(f"Cannot rebase branch '{ref.branch}' onto itself")
        ours = self.conn
        seq = time.time_ns()
        row_id = f"{seq:x}"
        new_base = base_db(self.db_name, row_id)
        # The upstream at one point: its clone is read as "theirs" and
        # becomes the branch's new base.
        self._clone(up_db, new_base)
        theirs = connect(new_base, self.catalog)
        base = None
        try:
            our_log, up_log = self._log(ours), self._log(theirs)
            row = self._base_row(our_log, onto.branch)
            if row is not None:
                base = connect(row["base"], self.catalog)
            # git: "ours" is the upstream, "theirs" the branch's own commits.
            info = self._three_way(ours, theirs, base, keep_ours=(on_conflict == "theirs"))
            if info["conflicts"]:
                info["resolved"] = "ours" if callable(on_conflict) else on_conflict
            self._copy_log(ours, our_log, up_log)
            self._commit_row(ours, ours_db, ours_db, "rebase", f"rebase onto {onto}", parent=onto.branch,
                             base=new_base, row_id=row_id, seq=seq)
            info["up_to_date"] = info["theirs_changes"] == 0
            return info
        finally:
            theirs.close()
            if base is not None:
                base.close()

    def _revert_impl(self, ref: Ref, commit: str) -> None:
        db = self._on(ref)
        ours = self.conn
        log = self._log(ours)
        c = self._find(log, commit)
        before = [r for r in log if r["seq"] < c["seq"] and r["branch"] == c["branch"]]
        if not before:
            raise ValueError(f"commit {commit} has no predecessor to revert to")
        ca = connect(snap_db(self.db_name, c["id"]), self.catalog)
        cb = connect(snap_db(self.db_name, before[-1]["id"]), self.catalog)
        try:
            plans = []
            for name in self.catalog.tables():
                t = self.catalog.table(name)
                if not t.pk:
                    continue
                A, Bf = self._keyed(t, self._rows(ca, name)), self._keyed(t, self._rows(cb, name))
                added = [k for k in A if k not in Bf]
                restore = [Bf[k] for k in Bf if A.get(k) != Bf[k]]
                if not added and not restore:
                    continue
                O = self._keyed(t, self._rows(ours, name))
                deletes = [k for k in added if k in O]
                inserts = [r for r in restore if t.key(r) not in O]
                updates = [r for r in restore if t.key(r) in O and O[t.key(r)] != r]
                if deletes or inserts or updates:
                    plans.append((t, deletes, inserts, updates))
            if not plans:
                return
            self._x(ours, "BEGIN")
            try:
                for t, d, i, u in plans:
                    self._apply(ours, t, d, i, u)
                self._x(ours, "COMMIT")
            except BaseException:
                try:
                    self._x(ours, "ROLLBACK")
                except Exception:
                    pass
                raise
            self._commit_row(ours, db, db, "commit", f"revert {commit}")
        finally:
            ca.close()
            cb.close()

    def _reset_impl(self, ref: Ref, to: str) -> None:
        db = self._on(ref)
        self._find(self._log(self.conn), to)
        self._close_conn()
        self._current_ref = None
        self._admin(f"DROP DATABASE {db}")
        self._admin(f"CREATE DATABASE {db} TEMPLATE {snap_db(self.db_name, to)}")
        self._on(ref)

    def _delete_impl(self, ref: Ref) -> None:
        db = self._db(ref.branch)
        if db == self.db_name:
            raise ValueError("cannot delete the main branch")
        if self._conn_db == db:
            self._close_conn()
            self._current_ref = None
        self._admin(f"DROP DATABASE {db}")
