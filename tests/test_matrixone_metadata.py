"""Metadata correctness and round-trip bounds without a running MO server."""

from unittest.mock import Mock

import pymysql
import pytest

from dblib import matrixone as mo
from dblib.db_api import Ref


def suite():
    s = object.__new__(mo.MatrixOneToolSuite)
    s._merge_summary_supported = False
    return s


def test_catalog_preserves_pk_order_and_column_defaults():
    s = suite()
    s._execute = Mock(return_value=[
        ("t", "a", "int", "NO", None, 2),
        ("t", "b", "int", "NO", None, 1),
        ("t", "v", "varchar(8)", "YES", "'hello'", None),
        ("t", "__mo_cpkey_col", "varchar", "NO", None, 1),
        ("heap", "v", "int", "YES", None, None),
    ])
    catalog = s._catalog("MixedCase")
    assert catalog["t"]["pk"] == ["b", "a"]
    assert catalog["t"]["columns"] == [
        ("a", "int", "NO", None), ("b", "int", "NO", None),
        ("v", "varchar(8)", "YES", "'hello'"),
    ]
    assert catalog["heap"]["pk"] == []
    assert s._execute.call_count == 1
    assert s._execute.call_args.args[1] == ("MixedCase", r"\_bb\_%")


def test_schema_conflict_is_detected_before_any_ddl():
    s = suite()
    s._execute = Mock()
    s._readable = Mock(return_value=True)
    ours = {
        "a": {"columns": [("id", "int", "NO", None)], "pk": ["id"]},
        "z": {"columns": [("id", "int", "NO", None)], "pk": ["id"]},
    }
    theirs = {
        "a": {"columns": [("id", "int", "NO", None), ("v", "int", "YES", None)], "pk": ["id"]},
        "z": {"columns": [("id", "int", "NO", None)], "pk": []},
    }
    with pytest.raises(RuntimeError, match="primary keys differ"):
        s._reconcile_schema("ours", mo._State("theirs"), ours, theirs)
    s._execute.assert_not_called()
    assert len(ours["a"]["columns"]) == 1


@pytest.mark.parametrize("table_count", [1, 12])
@pytest.mark.parametrize("native_summary", [False, True])
def test_merge_catalog_queries_are_constant_and_refreshed_between_calls(monkeypatch, table_count, native_summary):
    monkeypatch.setattr(mo, "MO_NATIVE_MERGE_AS_IS", False)
    monkeypatch.setattr(mo, "MO_COUNT_CONFLICTS", True)
    s = suite()
    s._merge_summary_supported = native_summary
    queries = []
    version = [0]

    def execute(sql, args=None):
        queries.append((sql, args))
        if "information_schema.columns c" in sql:
            cols = [("id", "int", "NO", None, 1), ("v", "int", "YES", None, None)]
            # A new source column appears between two merge calls. The
            # second operation must discover it without recreating the suite.
            if args[0] == "theirs" and version[0]:
                cols.append(("extra", "int", "YES", "7", None))
            return [(f"t{i}", *col) for i in range(table_count) for col in cols]
        if sql.startswith("SHOW CREATE TABLE"):
            return [("t", "CREATE TABLE `t` (`id` int, `v` int, `extra` int DEFAULT 7)")]
        if sql.endswith("OUTPUT SUMMARY"):
            return [(1, 0, 0, 1, 0, 0, 1)]
        if sql.startswith(("SHOW", "SELECT")):
            raise AssertionError(f"unexpected per-table metadata query: {sql}")
        return None

    s._execute = execute
    s._on = Mock(return_value="ours")
    s._state = Mock(return_value=mo._State("theirs"))
    s._relation = Mock(return_value=("theirs", "ours", {"prev_db": "ours", "prev_snap": "base"}))
    s._readable = Mock(return_value=True)
    s._summary = Mock(return_value=((1, 0, 0), (1, 0, 0)))
    s._delta = lambda head, base, pk: {(1,): ("UPDATE", {"id": 1, "v": 2 if "theirs" in head else 3})}
    s._head = Mock(return_value={"id": "c", "kind": "commit", "snap": "s", "db": "ours"})
    s._copy_log = Mock()
    s._add_commit = Mock(return_value="merged")

    first = s._merge_impl(Ref("ours"), Ref("theirs"), "merge")
    assert first["conflicts"] == table_count
    assert first["schema_changes"]["added_columns"] == []
    version[0] = 1
    second = s._merge_impl(Ref("ours"), Ref("theirs"), "merge again")
    assert second["schema_changes"]["added_columns"] == [f"t{i}.extra" for i in sorted(range(table_count), key=lambda i: f"t{i}")]
    catalog_queries = [sql for sql, _ in queries if "information_schema.columns c" in sql]
    assert len(catalog_queries) == 4  # exactly two per operation, at either scale
    assert sum(sql.startswith("ALTER TABLE") for sql, _ in queries) == table_count
    assert sum(sql.startswith("DATA BRANCH MERGE") for sql, _ in queries) == 2 * table_count
    if native_summary:
        s._summary.assert_not_called()
        s._readable.assert_not_called()
    else:
        assert s._summary.call_count == 2 * table_count
        # Only historical bases need visibility probes, once per table
        # per operation; the two current head catalogs are sufficient.
        assert s._readable.call_count == 2 * table_count


def test_commit_index_migration_handles_concurrent_initialization():
    s = suite()
    s._execute = Mock(side_effect=[[], [(1,)]])
    s._execute_ddl = Mock(side_effect=pymysql.MySQLError(1061, "duplicate index"))
    s._ensure_commit_index("db")
    assert s._execute_ddl.call_count == 1


def test_commit_index_migration_does_not_hide_failure():
    s = suite()
    s._execute = Mock(return_value=[])
    s._execute_ddl = Mock(side_effect=pymysql.MySQLError(1105, "catalog unavailable"))
    with pytest.raises(pymysql.MySQLError, match="catalog unavailable"):
        s._ensure_commit_index("db")


def test_ddl_defaults_preserve_literals_expressions_and_quoted_identifiers():
    ddl = r"""CREATE TABLE `t` (
      `id` int NOT NULL,
      `a` varchar(50) DEFAULT 'NULL' COMMENT 'DEFAULT ignored',
      `b``x` varchar(50) DEFAULT 'it''s, (fine) \\ DEFAULT',
      `c` timestamp DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      `d` int DEFAULT (-7 + (3 * 2)),
      `e` varchar(20) DEFAULT '',
      PRIMARY KEY (`id`)
    )"""
    assert mo._ddl_defaults(ddl) == {
        "a": "'NULL'", "b`x": r"'it''s, (fine) \\ DEFAULT'",
        "c": "CURRENT_TIMESTAMP(6)", "d": "(-7 + (3 * 2))", "e": "''",
    }


def test_snapshot_schema_checks_visibility_even_when_current_catalog_has_table():
    s = suite()
    s._readable = Mock(return_value=False)
    s._execute = Mock()
    past = mo._State("source", "old_snapshot")
    current_catalog = {"new_table": {"pk": ["id"], "columns": [("id", "int", "NO", None)]}}
    assert s._reconcile_schema("destination", past, {}, current_catalog) == {
        "added_tables": [], "added_columns": [],
    }
    s._readable.assert_called_once_with(past, "new_table")
    s._execute.assert_not_called()
