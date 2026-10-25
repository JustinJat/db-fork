"""Database operations keep round trips bounded and never replay failed writes."""

from unittest.mock import Mock

import pymysql
import pytest

from dblib import matrixone as mo
from dblib.db_api import Ref


def suite():
    s = object.__new__(mo.MatrixOneToolSuite)
    s._warned = set()
    s._database_branch_supported = {"diff": None, "merge": None}
    s._database_restore_supported = {"reset": None, "revert": None}
    return s


def test_database_merge_returns_all_tables_in_one_statement():
    s = suite()
    s._execute = Mock(return_value=[("a", 1, 1, 0, 0, 2, 0, 0, 1, 1),
                                    ("b", 1, 1, 1, 0, 0, 0, 0, 0, 0)])
    result = s._database_summary("merge", mo._State("s"), mo._State("d"),
                                 tables=["a", "b"], policy="SKIP")
    assert result["a"] == (True, True, (0, 0, 2, 0, 0, 1, 1))
    assert s._database_branch_supported["merge"] is True
    s._execute.assert_called_once_with(
        "DATA BRANCH MERGE DATABASE `s` INTO `d` TABLES (`a`, `b`) WHEN CONFLICT SKIP OUTPUT SUMMARY")


def test_database_diff_handles_snapshot_only_tables_and_orientation():
    s = suite()
    s._state = Mock(side_effect=[mo._State("a", "old"), mo._State("b")])
    s._execute = Mock(return_value=[("common", 1, 1, 3, 4, 5, 6, 7, 8, 0),
                                    ("removed", 0, 1, 0, 0, 0, 2, 0, 0, 0)])
    result = s._diff_impl(Ref("a"), Ref("b"))
    assert result["rows_added"] == 10
    assert result["rows_deleted"] == 12
    assert result["rows_modified"] == 13
    assert result["tables"][1] == {"table_name": "removed", "rows_added": 0,
                                    "rows_deleted": 2, "rows_modified": 0}
    s._execute.assert_called_once_with(
        "DATA BRANCH DIFF DATABASE `b` AGAINST `a`{snapshot='old'} "
        "IGNORE TABLES (`_bb_commits`) OUTPUT SUMMARY")


def test_database_parser_rejection_is_cached_per_operation():
    s = suite()
    s._execute = Mock(side_effect=pymysql.ProgrammingError(
        1064, 'syntax error at line 1 column 25 near "DATABASE s INTO d";'))
    for _ in range(2):
        assert s._database_summary("merge", mo._State("s"), mo._State("d")) is None
    assert s._execute.call_count == 1
    assert s._database_branch_supported == {"diff": None, "merge": False}


@pytest.mark.parametrize("error", [
    pymysql.OperationalError(2013, "Lost connection"),
    pymysql.OperationalError(1105, "conflict on later table"),
    pymysql.ProgrammingError(1064, 'syntax error at line 1 near "bad"'),
])
def test_database_execution_errors_are_not_replayed(error):
    s = suite()
    s._execute = Mock(side_effect=error)
    with pytest.raises(pymysql.MySQLError):
        s._database_summary("merge", mo._State("s"), mo._State("d"))
    assert s._execute.call_count == 1
    assert s._database_branch_supported["merge"] is None


@pytest.mark.parametrize("rows", [None, [], [("a", 1)],
                                  [("a", 1, 1, -1, 0, 0, 0, 0, 0, 0)],
                                  [("a", 1, 1, 0, 0, 0, 0, 0, 0, 0)] * 2])
def test_database_incomplete_results_do_not_trigger_replay(rows):
    s = suite()
    s._execute = Mock(return_value=rows)
    with pytest.raises(RuntimeError, match="not retried"):
        s._database_summary("merge", mo._State("s"), mo._State("d"), tables=["a"])
    assert s._execute.call_count == 1


def test_server_revert_sends_only_snapshot_handles_and_tables():
    s = suite()
    s._execute = Mock()
    assert s._database_restore("revert", "d", mo._State("s", "after"),
                               before=mo._State("s", "before"), tables=["a", "b"])
    s._execute.assert_called_once_with(
        "DATA BRANCH REVERT DATABASE `d` FROM `s`{snapshot='after'} "
        "AGAINST `s`{snapshot='before'} TABLES (`a`, `b`)")
    assert s._database_restore_supported["revert"] is True


@pytest.mark.parametrize("operation", ["reset", "revert"])
def test_server_restore_parser_fallback_only_once(operation):
    s = suite()
    s._execute = Mock(side_effect=pymysql.ProgrammingError(
        1064, f'syntax error at line 1 near "{operation.upper()} DATABASE"'))
    for _ in range(2):
        assert not s._database_restore(operation, "d", mo._State("s", "snapshot"))
    assert s._execute.call_count == 1
    assert s._database_restore_supported[operation] is False


@pytest.mark.parametrize("error", [
    pymysql.OperationalError(2013, "Lost connection"),
    pymysql.OperationalError(1062, "Duplicate key in later table"),
    pymysql.ProgrammingError(1064, 'syntax error at line 1 near "bad"'),
])
def test_server_restore_execution_errors_never_replay(error):
    s = suite()
    s._execute = Mock(side_effect=error)
    with pytest.raises(pymysql.MySQLError):
        s._database_restore("reset", "d", mo._State("s", "snapshot"))
    assert s._execute.call_count == 1
    assert s._database_restore_supported["reset"] is None


@pytest.mark.parametrize("source", ["ours", "parent"])
def test_server_reset_preserves_inherited_log_boundaries(monkeypatch, source):
    import json
    monkeypatch.setattr(mo, "MO_NATIVE_RESET", False)
    s = suite()
    fingerprint = {"t": {"cols": {"id": "int"}, "pk": ["id"]}}
    s._on = Mock(return_value="ours")
    s._find_commit = Mock(return_value={"db": source, "snap": "old", "seq": 42,
                                        "schema_json": json.dumps(fingerprint)})
    s._fingerprint = Mock(return_value=fingerprint)
    s._execute = Mock()
    s._reconcile_to_fingerprint = Mock(side_effect=AssertionError("unchanged schema"))
    s._undo_since = Mock(side_effect=AssertionError("native reset must not replay diff rows"))
    s._reset_impl(Ref("ours"), "commit")
    sqls = [call.args[0] for call in s._execute.call_args_list]
    assert sum(sql.startswith("DATA BRANCH RESET DATABASE") for sql in sqls) == 1
    assert ("kind NOT IN" in sqls[-1]) == (source == "parent")
    assert s._execute.call_args.args[1] == (42,)


def test_unknown_server_keeps_schema_reconciliation_in_legacy_reset(monkeypatch):
    import json
    monkeypatch.setattr(mo, "MO_NATIVE_RESET", False)
    s = suite()
    target = {"t": {"cols": {"id": "int"}, "pk": ["id"]}}
    current = {"t": {"cols": {"id": "int", "new": "int"}, "pk": ["id"]}}
    s._on = Mock(return_value="d")
    s._find_commit = Mock(return_value={"db": "d", "snap": "old", "seq": 42,
                                        "schema_json": json.dumps(target)})
    s._fingerprint = Mock(return_value=current)
    s._execute = Mock()
    s._reconcile_to_fingerprint = Mock(side_effect=AssertionError("must leave original schema for fallback"))
    s._database_restore = Mock(side_effect=AssertionError("support is not yet known"))
    s._undo_since = Mock()
    s._reset_impl(Ref("d"), "commit")
    assert s._undo_since.call_count == 1
    assert s._undo_since.call_args.args[2] == target
