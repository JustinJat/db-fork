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
