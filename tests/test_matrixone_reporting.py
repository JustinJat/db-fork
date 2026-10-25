"""Exercise real path selection with a small deterministic server stub."""

import json
from unittest.mock import Mock

import pymysql
import pytest

from dblib import matrixone as mo
from dblib.db_api import DBToolSuite, Ref
from dblib.result_collector import ResultCollector


@pytest.fixture
def suite(monkeypatch, tmp_path):
    monkeypatch.setattr(mo, "MO_NATIVE_MERGE_AS_IS", False)
    monkeypatch.setattr(mo, "MO_NATIVE_RESET", False)
    s = object.__new__(mo.MatrixOneToolSuite)
    DBToolSuite.__init__(s, Mock(), ResultCollector(output_dir=str(tmp_path)))
    s._warned = set()
    s._database_branch_supported = {"diff": None, "merge": None}
    s._database_restore_supported = {"reset": None, "revert": None}
    s._merge_summary_supported = None
    s._on = Mock(return_value="ours")
    s._state = Mock(return_value=mo._State("theirs"))
    s._relation = Mock(return_value=("theirs", "ours", {"prev_db": "ours", "prev_snap": "base"}))
    catalog = {t: {"columns": [("id", "int", "NO", None), ("v", "int", "YES", None)],
                   "pk": ["id"]} for t in ("a", "b")}
    s._catalogs = Mock(return_value=[catalog, catalog])
    s._readable = Mock(return_value=True)
    s._summary = Mock(return_value=((0, 0, 1), (0, 0, 0)))
    s._head = Mock(return_value={"id": "c", "kind": "commit", "snap": "s", "db": "ours"})
    s._copy_log = Mock()
    s._find_commit = Mock(return_value={"id": "c", "kind": "commit", "snap": "s", "db": "ours"})
    s._add_commit = Mock(return_value="merged")
    return s


def server(mode):
    def execute(sql, args=None):
        if sql.startswith("DATA BRANCH MERGE DATABASE"):
            if mode == "database":
                return [(t, 1, 1, 0, 0, 1, 0, 0, 0, 0) for t in ("a", "b")]
            raise pymysql.ProgrammingError(1064, 'syntax error at line 1 near "DATABASE"')
        if sql.startswith("DATA BRANCH MERGE") and sql.endswith("OUTPUT SUMMARY"):
            if mode == "table":
                return [(0, 0, 1, 0, 0, 0, 0)]
            raise pymysql.ProgrammingError(1064, 'syntax error at line 1 near "OUTPUT"')
    return Mock(side_effect=execute)


@pytest.mark.parametrize("mode,path,summary", [
    ("database", "database_merge_summary", "native"),
    ("table", "table_merge_summary", "native"),
    ("legacy", "legacy_table_merge", "legacy"),
])
def test_merge_result_and_recorded_path_follow_negotiation(suite, mode, path, summary, capsys):
    suite._execute = server(mode)
    for _ in range(2):
        result = suite.merge("main", "feature")
        assert result.ok, result.error
        assert result.value["merge_summary"] == summary
        assert result.execution_paths == [path]
        assert len(result.fallback_reasons) == {"database": 0, "table": 1, "legacy": 2}[mode]
        assert all("server rejected syntax" in reason for reason in result.fallback_reasons)
    output = capsys.readouterr().out
    assert output.count("Warning:") == {"database": 0, "table": 1, "legacy": 2}[mode]
    assert suite.result_collector.support_summary()["execution_path_counts"]["MERGE"][path]["ok"] == 2


@pytest.mark.parametrize("error", [
    pymysql.OperationalError(2013, "lost connection"),
    pymysql.IntegrityError(1062, "duplicate key"),
])
def test_failed_native_path_is_recorded_without_fallback(suite, error, capsys):
    def execute(sql, args=None):
        if sql.startswith("DATA BRANCH MERGE"):
            raise error

    suite._execute = Mock(side_effect=execute)
    result = suite.merge("main", "feature")
    assert result.failed
    assert result.execution_paths == ["database_merge_summary"]
    assert result.fallback_reasons == []
    suite._summary.assert_not_called()
    assert "Warning:" not in capsys.readouterr().out
    counts = suite.result_collector.support_summary()["execution_path_counts"]
    assert counts["MERGE"]["database_merge_summary"]["failed"] == 1


def test_custom_resolver_is_reported_as_legacy_without_native_probe(suite):
    suite._execute = server("database")
    result = suite.merge("main", "feature", on_conflict=lambda db, conflicts: None)
    assert result.ok
    assert result.value["merge_summary"] == "legacy"
    assert result.execution_paths == ["legacy_table_merge"]
    assert result.fallback_reasons == ["merge: custom resolver requires conflict rows"]
    assert not any("OUTPUT SUMMARY" in call.args[0] for call in suite._execute.call_args_list)


def test_invoked_custom_resolver_is_recorded(suite, monkeypatch):
    monkeypatch.setattr(mo, "MO_COUNT_CONFLICTS", True)
    suite._execute = server("database")
    suite._summary.return_value = ((0, 0, 1), (0, 0, 1))
    suite._delta = Mock(return_value={})
    suite._conflict_keys = Mock(return_value=[(1,)])
    suite._conflict_dicts = Mock(return_value=[{"id": 1}])
    suite._conflict_session = Mock()
    resolver = Mock()
    result = suite.merge("main", "feature", on_conflict=resolver)
    assert result.ok, result.error
    assert result.value["merge_summary"] == "legacy"
    assert result.execution_paths == ["legacy_table_merge", "custom_resolver"]
    resolver.assert_called_once()


def test_read_only_schema_rejection_records_sql_diff(suite, monkeypatch):
    monkeypatch.setattr(mo, "MO_NATIVE_DIFF_ONLY", False)
    suite._execute = Mock(side_effect=pymysql.OperationalError(1105, "schema compatibility check failed"))
    suite._summary = Mock(side_effect=pymysql.OperationalError(1105, "schema compatibility check failed"))
    suite._tables = Mock(return_value=["a"])
    suite._sql_summary = Mock(return_value=(0, 0, 1))
    suite._schema_diff_impl = Mock(return_value={})
    result = suite.diff("main", "feature")
    assert result.ok, result.error
    assert result.execution_paths == ["sql_diff"]
    assert result.fallback_reasons == ["database_diff_summary: incompatible schema",
                                       "table_diff_summary: incompatible schema"]


def test_snapshot_readability_is_checked_once_per_source_table(suite):
    suite._state.return_value = mo._State("theirs", "past")
    suite._execute = server("database")
    assert suite.merge("main", "feature@commit").ok
    assert suite._readable.call_count == 2
    assert {c.args[1] for c in suite._readable.call_args_list} == {"a", "b"}


def test_empty_merge_does_not_claim_a_native_request(suite):
    suite._catalogs.return_value = [{}, {}]
    suite._execute = server("database")
    result = suite.merge("main", "feature")
    assert result.ok
    assert result.value["merge_summary"] == "none"
    assert result.execution_paths == ["no_data_tables"]
    assert not any("DATA BRANCH MERGE" in call.args[0] for call in suite._execute.call_args_list)


@pytest.mark.parametrize("supported,path", [(True, "database_reset"), (False, "legacy_reset")])
def test_reset_reports_native_or_disabled_path(suite, supported, path):
    schema = {"a": {"cols": {"id": "int"}, "pk": ["id"]}}
    suite._find_commit.return_value = {"db": "ours", "snap": "s", "seq": 1, "schema_json": json.dumps(schema)}
    suite._fingerprint = Mock(return_value=schema)
    suite._database_restore_supported["reset"] = supported
    suite._execute = Mock()
    suite._undo_since = Mock()
    result = suite.reset("main", "commit")
    assert result.ok, result.error
    assert result.execution_paths == [path]
    assert result.fallback_reasons == ([] if supported else ["database_reset: disabled"])


def test_profiler_captures_server_identity_and_closes_connection(monkeypatch):
    from scripts.profile_matrixone_operations import server_identity
    conn = Mock()
    cursor = Mock()
    conn.cursor.return_value = Mock(__enter__=Mock(return_value=cursor), __exit__=Mock(return_value=False))
    cursor.fetchone.return_value = ("4.2.6", "7149815")
    monkeypatch.setattr(mo, "connect", Mock(return_value=conn))
    assert server_identity() == {"version": "4.2.6", "git_version": "7149815"}
    cursor.execute.assert_called_once_with("SELECT version(), git_version()")
    conn.close.assert_called_once()
