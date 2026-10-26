"""Merge timing logs must distinguish unavailable data and avoid double counting."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.profile_matrixone_operations import SQLTrace, summarize_merge_timings, summarize_restore_work


@pytest.mark.parametrize("options", [{"history_rows": -1}, {"log_copy": "unknown"}])
def test_invalid_history_options_do_not_create_databases(options):
    from scripts.profile_matrixone_operations import run_once

    backend = Mock()
    with pytest.raises(ValueError):
        run_once(1, 10, 5, "database", backend=backend, **options)
    backend.setup_database.assert_not_called()


def test_history_sequence_guard_checks_both_logs_before_inserting():
    from scripts.profile_matrixone_operations import seed_merge_history

    suite = Mock()
    suite._execute.side_effect = [[(3, 1000)], [(4, 10)]]
    with pytest.raises(ValueError, match="overlap"):
        seed_merge_history(suite, "target", "source", "base", 10)
    suite.conn.cursor.assert_not_called()


def events(request_id="r", tables=2, status="ok"):
    table_events = [{"msg": "DataBranch-Merge-Table-Timing", "schema_version": 1,
                     "request_id": request_id, "source": f"src.t{i}", "destination": f"dst.t{i}",
                     "status": "ok", "total_ms": 3,
                     "phases_ms": {"diff": 1, "apply": 2}} for i in range(tables)]
    return table_events + [{"msg": "DataBranch-Merge-Request-Timing", "schema_version": 1,
                           "request_id": request_id, "table_count": tables, "status": status,
                           "total_ms": tables * 3 + 4,
                           "phases_ms": {"diff": tables, "apply": tables * 2, "txn_finish": 4}}]


def restore_event(operation="reset"):
    return {"msg": "DataBranch-Restore-Work", "schema_version": 1, "request_id": "r",
            "operation": operation, "status": "ok", "staging": "temporary", "table_count": 1,
            "sql_statements": 3, "sql_counts": {"stage_create": 2, "delete": 0, "update": 0,
                                               "insert": 0, "stage_drop": 1},
            "empty_stages": 2, "unknown_stages": 0, "materialized_rows": 0, "total_ms": 6,
            "phases_ms": {"stage_create": 3, "delete": 0, "update": 0, "insert": 0,
                          "stage_drop": 2, "other": 1}}


@pytest.mark.parametrize("operation", ["reset", "revert"])
@pytest.mark.parametrize("status", ["ok", "error", "panic"])
def test_restore_work_keeps_status_and_counts_once(operation, status):
    event = restore_event(operation)
    event.update(status=status, empty_stages=1, unknown_stages=1)
    second = dict(event, request_id="r2")
    result = summarize_restore_work([event, second, *events()], 2, operation)
    assert result["complete"] and result["issues"] == []
    assert result["sql_statements"] == 6 and result["total_ms"] == 12
    assert sum(result["sql_counts"].values()) == 6
    assert sum(result["phases_ms"].values()) == 12
    assert result["records"][0]["status"] == status
    assert result["records"][0]["staging"] == "temporary"


@pytest.mark.parametrize("change", [
    lambda r: r.append(deepcopy(r[0])),
    lambda r: r[0].pop("request_id"),
    lambda r: r[0].update(operation="merge"),
    lambda r: r[0].update(schema_version=2),
    lambda r: r[0].update(sql_statements=4),
    lambda r: r[0].update(table_count=True),
    lambda r: r[0].update(unknown_stages=1),
    lambda r: r[0].update(materialized_rows=-1),
    lambda r: r[0].update(total_ms=float("nan")),
    lambda r: r[0].update(total_ms=7),
    lambda r: r[0]["sql_counts"].pop("update"),
    lambda r: r[0]["phases_ms"].update(txn_finish=0),
])
def test_invalid_restore_records_do_not_publish_totals(change):
    records = [restore_event()]
    change(records)
    result = summarize_restore_work(records, 1, "reset")
    assert result["available"] and not result["complete"] and result["issues"]
    assert result["sql_statements"] is None and result["total_ms"] is None


def test_old_restore_server_does_not_report_zero_work():
    result = summarize_restore_work([], 1, "reset")
    assert not result["available"] and not result["complete"]
    assert result["sql_statements"] is None


def test_restore_work_without_staging_label_keeps_counts_without_guessing_path():
    record = restore_event()
    record.pop("staging")
    result = summarize_restore_work([record], 1, "reset")
    assert result["complete"] and result["sql_statements"] == 3
    assert "staging" not in result["records"][0]


def restore_probe_event():
    record = restore_event()
    record.update(schema_version=2, inline_delete_tables=1, fallback_delete_tables=0)
    record["sql_counts"].update(stage_create=1, delete_probe=1)
    record["phases_ms"].update(stage_create=2, delete_probe=1)
    return record


@pytest.mark.parametrize("fallback", [False, True])
def test_restore_probe_paths_keep_counts_and_timings(fallback):
    record = restore_probe_event()
    if fallback:
        record.update(inline_delete_tables=0, fallback_delete_tables=1, sql_statements=4)
        record["sql_counts"]["stage_create"] = 2
    result = summarize_restore_work([record], 1, "reset")
    assert result["complete"] and not result["issues"]
    assert result["sql_counts"]["delete_probe"] == 1
    assert result["phases_ms"]["delete_probe"] == 1
    assert result["sql_statements"] == (4 if fallback else 3)


@pytest.mark.parametrize("change", [
    lambda r: r.update(schema_version=5),
    lambda r: r.pop("inline_delete_tables"),
    lambda r: r.update(fallback_delete_tables=-1),
    lambda r: r.update(inline_delete_tables=2),
    lambda r: r.update(inline_delete_tables=0),
    lambda r: r.update(unknown_stages=1),
    lambda r: r["phases_ms"].pop("delete_probe"),
])
def test_invalid_restore_probe_record_does_not_publish_totals(change):
    record = restore_probe_event()
    change(record)
    result = summarize_restore_work([record], 1, "reset")
    assert not result["complete"] and result["issues"]
    assert result["sql_statements"] is None


def test_failed_restore_probe_can_have_no_completed_path():
    record = restore_probe_event()
    record.update(status="error", inline_delete_tables=0, empty_stages=1)
    result = summarize_restore_work([record], 1, "reset")
    assert result["complete"] and result["sql_counts"]["delete_probe"] == 1


def restore_subquery_event():
    record = restore_event()
    record.update(schema_version=3, subquery_delete_tables=1, staged_delete_tables=0, empty_stages=1)
    record["sql_counts"].update(stage_create=1, delete=1)
    record["phases_ms"].update(stage_create=2, delete=1)
    return record


@pytest.mark.parametrize("path", ["subquery", "staged", "partial"])
def test_restore_subquery_and_staged_paths(path):
    record = restore_subquery_event()
    if path == "staged":
        record.update(subquery_delete_tables=0, staged_delete_tables=1, empty_stages=2)
        record["sql_counts"].update(stage_create=2, delete=0)
    elif path == "partial":
        record.update(subquery_delete_tables=0, status="error", sql_statements=2)
        record["sql_counts"]["delete"] = 0
    result = summarize_restore_work([record], 1, "reset")
    assert result["complete"] and not result["issues"]
    assert "delete_probe" not in result["sql_counts"]
    assert result["records"][0]["subquery_delete_tables"] == int(path == "subquery")


@pytest.mark.parametrize("change", [
    lambda r: r.pop("subquery_delete_tables"),
    lambda r: r.update(subquery_delete_tables=2),
    lambda r: r.update(staged_delete_tables=-1),
    lambda r: r.update(staged_delete_tables=1),
    lambda r: r.update(empty_stages=2),
    lambda r: r["sql_counts"].update(delete_probe=1),
    lambda r: r["phases_ms"].update(delete_probe=0),
])
def test_invalid_restore_subquery_records_do_not_publish_totals(change):
    record = restore_subquery_event()
    change(record)
    result = summarize_restore_work([record], 1, "reset")
    assert not result["complete"] and result["issues"]
    assert result["sql_statements"] is None


def restore_no_stage_event():
    record = restore_subquery_event()
    record.update(schema_version=4, staging="none", subquery_row_tables=1, staged_row_tables=0, empty_stages=0)
    record["sql_counts"].update(stage_create=0, stage_drop=0, update=1, insert=1)
    record["phases_ms"].update(stage_create=0, stage_drop=0, update=2, insert=2)
    return record


@pytest.mark.parametrize("path", ["subquery", "staged", "mixed", "partial", "stage_failure", "keyless"])
def test_restore_row_paths_keep_materialization_and_dml_separate(path):
    record = restore_no_stage_event()
    if path in ("staged", "mixed", "stage_failure"):
        record.update(staging="temporary", subquery_row_tables=0, staged_row_tables=1,
                      subquery_delete_tables=0, staged_delete_tables=1)
        record["sql_counts"].update(stage_create=2, stage_drop=1)
        if path == "mixed":
            record.update(table_count=2, subquery_row_tables=1, subquery_delete_tables=1)
            record["sql_counts"].update(delete=2, update=2, insert=2)
        elif path == "stage_failure":
            record.update(status="error", staged_row_tables=0, staged_delete_tables=0)
            record["sql_counts"].update(stage_create=1, stage_drop=0, delete=0, update=0, insert=0)
    elif path == "partial":
        record.update(status="error", subquery_row_tables=0)
        record["sql_counts"].update(update=0, insert=0)
    elif path == "keyless":
        record.update(subquery_delete_tables=0)
        record["sql_counts"]["update"] = 0
    record["sql_statements"] = sum(record["sql_counts"].values())
    result = summarize_restore_work([record], 1, "reset")
    assert result["complete"] and not result["issues"]
    assert result["records"][0]["staging"] == record["staging"]
    assert result["sql_statements"] == record["sql_statements"]


@pytest.mark.parametrize("change", [
    lambda r: r.pop("subquery_row_tables"),
    lambda r: r.pop("staging"),
    lambda r: r.update(staging="public"),
    lambda r: r.update(staging="temporary"),
    lambda r: r.update(subquery_row_tables=True),
    lambda r: r.update(subquery_row_tables=2),
    lambda r: r.update(staged_row_tables=-1),
    lambda r: r.update(staged_row_tables=1),
    lambda r: r.update(materialized_rows=1),
    lambda r: r.update(empty_stages=1),
    lambda r: r["sql_counts"].update(stage_drop=1),
    lambda r: r["sql_counts"].update(update=0, insert=0),
])
def test_invalid_restore_row_paths_do_not_publish_totals(change):
    record = restore_no_stage_event()
    change(record)
    # Keep totals consistent so failures exercise path validation.
    record["sql_statements"] = sum(record["sql_counts"].values())
    result = summarize_restore_work([record], 1, "reset")
    assert not result["complete"] and result["issues"]
    assert result["sql_statements"] is None


@pytest.mark.parametrize("operation", ["reset", "revert"])
def test_trace_attributes_restore_work_to_current_native_request(tmp_path, operation):
    log = tmp_path / "mo.log"
    endpoints = 3 if operation == "revert" else 2
    metadata = {"msg": "DataBranch-Restore-Metadata", "schema_version": 1,
                "operation": operation, "database_opens": endpoints, "snapshot_resolutions": endpoints - 1,
                "relation_opens": endpoints, "catalog_ms": 5}
    log.write_text(json.dumps(restore_event(operation)) + "\n" + json.dumps(metadata) + "\n")
    trace = SQLTrace(SimpleNamespace(), log)

    def restore():
        trace.records.append({"sql": f"DATA BRANCH {operation.upper()} DATABASE db FROM snapshot_db",
                              "error": None, "received_bytes": 11, "returned_rows": 0})
        with log.open("a") as out:
            out.write(json.dumps(restore_event(operation)) + "\n")
            out.write(json.dumps(metadata) + "\n")

    _, result = trace.measure(operation, restore)
    assert result["statements"] == 1
    assert result["server_restore_work"]["sql_statements"] == 3
    assert result["server_restore_work"]["complete"]
    assert result["server_restore_metadata"] == [metadata]
    assert result["server_restore_work"]["total_ms"] == 6  # catalog time is separate
    _, result = trace.measure(operation, lambda: None)
    assert result["server_restore_metadata"] == []
    _, result = SQLTrace(SimpleNamespace()).measure(operation, lambda: None)
    assert "server_restore_work" not in result


@pytest.mark.parametrize("options", [{"tables": 0}, {"changed": 50}, {"operation": "merge"}, {"mutation": "wrong"}])
def test_invalid_restore_profile_does_not_connect(monkeypatch, options):
    from scripts import profile_matrixone_restore as profile

    connect = Mock()
    monkeypatch.setattr(profile.mo, "connect", connect)
    args = dict(tables=1, rows=100, changed=10, operation="reset", mutation="noop", server_log=None)
    args.update(options)
    with pytest.raises(ValueError):
        profile.run_once(**args)
    connect.assert_not_called()


@pytest.mark.parametrize("counts", [[10], [1] * 10])
def test_database_and_table_requests_count_each_phase_once(counts):
    records = [r for i, count in enumerate(counts) for r in events(str(i), count)]
    result = summarize_merge_timings(records, len(counts))
    assert result["available"] and result["complete"]
    assert result["issues"] == []
    assert len(result["tables"]) == 10
    assert len(result["requests"]) == len(counts)
    assert result["phases_ms"] == {"diff": 10, "apply": 20, "txn_finish": 4 * len(counts)}
    assert sum(result["phases_ms"].values()) == result["total_ms"]


def test_old_server_timings_are_unavailable_not_zero():
    result = summarize_merge_timings([{"msg": "DataBranch-HashDiff-Done"}], 1)
    assert not result["available"] and not result["complete"]
    assert result["phases_ms"] is None and result["total_ms"] is None
    assert result["issues"]


@pytest.mark.parametrize("status", ["ok", "error", "panic"])
def test_no_tables_and_failed_requests_keep_status(status):
    result = summarize_merge_timings(events(tables=0, status=status), 1)
    assert result["complete"]
    assert result["requests"][0]["status"] == status
    assert result["phases_ms"]["apply"] == 0
    assert result["total_ms"] == 4


@pytest.mark.parametrize("change", [
    lambda r: r.pop(),                       # request finish absent
    lambda r: r.pop(0),                      # missing table event
    lambda r: r.append(deepcopy(r[-1])),      # duplicate request
    lambda r: r[-1].update(total_ms=100),     # double counted phases
    lambda r: r[-1].update(total_ms=-1),
    lambda r: r[-1].update(total_ms=float("nan")),
    lambda r: r[-1].update(schema_version=2),
    lambda r: r[-1].update(phases_ms={}),
    lambda r: r[0].update(request_id="unknown"),
])
def test_partial_or_invalid_logs_preserve_details_without_publishing_totals(change):
    records = events()
    change(records)
    result = summarize_merge_timings(records, 1)
    assert result["available"] and not result["complete"]
    assert result["issues"]
    assert result["total_ms"] is None and result["phases_ms"] is None
    assert result["requests"] or result["tables"]


def test_trace_reads_only_operation_logs_and_ignores_rejected_probes(tmp_path):
    log = tmp_path / "mo.log"
    log.write_text(json.dumps(events("previous")[-1]) + "\n")
    trace = SQLTrace(SimpleNamespace(), log)

    def merge():
        trace.records.extend([
            {"sql": "DATA BRANCH MERGE DATABASE src INTO dst", "error": "syntax error",
             "received_bytes": 10, "returned_rows": 0},
            {"sql": "DATA BRANCH MERGE src.t INTO dst.t", "error": None,
             "received_bytes": 20, "returned_rows": 1},
        ])
        with log.open("a") as out:
            for record in [None, [], {"msg": None}, {"msg": "DataBranch-HashDiff-Done"}, *events(tables=1)]:
                out.write(json.dumps(record) + "\n")
            out.write("incomplete line\n")
        return SimpleNamespace(ok=True, value={"merge_summary": "native"})

    _, result = trace.measure("merge", merge)
    assert result["server_merge_timing"]["complete"]
    assert result["server_merge_timing"]["total_ms"] == 7
    assert result["server_events"]["DataBranch-Merge-Request-Timing"] == 1
    assert result["merge_summary"] == "native"


def test_trace_counts_only_current_executor_lca_queries(tmp_path):
    log = tmp_path / "mo.log"
    sql = ("select pks.`__mo_data_branch_ordinal`, lca.`id` from `db`.`t`{MO_TS=10}"
           " as lca right join (values row(0,1)) as pks(ordinal,id) on lca.id=pks.id")
    probe = {"msg": "sql_executor exec", "sql": sql}
    log.write_text(json.dumps(probe) + "\n")  # previous operation must be excluded
    trace = SQLTrace(SimpleNamespace(), log)

    def diff():
        with log.open("a") as out:
            for record in [probe, probe,
                           {"msg": "frontend exec", "sql": sql},
                           {"msg": "sql_executor exec", "sql": "select 1"},
                           {"msg": "sql_executor exec", "sql": 42},
                           {"msg": "DataBranch-HashDiff-Done"}]:
                out.write(json.dumps(record) + "\n")
        return SimpleNamespace(ok=True)

    _, result = trace.measure("diff", diff)
    assert result["server_lca_probe_statements"] == 2
    assert result["statements"] == 0  # internal probes are separate from client SQL
    assert result["server_events"] == {"DataBranch-HashDiff-Done": 1}

    _, result = trace.measure("log", lambda: SimpleNamespace(ok=True))
    assert result["server_lca_probe_statements"] == 0
    _, result = SQLTrace(SimpleNamespace()).measure("log", lambda: SimpleNamespace(ok=True))
    assert "server_lca_probe_statements" not in result


def test_trace_counts_only_current_unlocked_branch_graph_reads(tmp_path):
    log = tmp_path / "mo.log"
    sql = ("select table_id, clone_ts, p_table_id, level, table_deleted "
           "from mo_catalog.mo_branch_metadata")
    probe = {"msg": "sql_executor exec", "sql": sql}
    log.write_text(json.dumps(probe) + "\n")
    trace = SQLTrace(SimpleNamespace(), log)

    def diff():
        with log.open("a") as out:
            for record in [probe, probe,
                           {"msg": "frontend exec", "sql": sql},
                           {"msg": "sql_executor exec", "sql": sql + " for update"},
                           {"msg": "sql_executor exec", "sql": [sql]},
                           {"msg": "DataBranch-HashDiff-Done"}]:
                out.write(json.dumps(record) + "\n")
        return SimpleNamespace(ok=True)

    _, result = trace.measure("diff", diff)
    assert result["server_branch_dag_statements"] == 2
    assert result["server_lca_probe_statements"] == 0
    assert result["statements"] == 0
    _, result = trace.measure("log", lambda: SimpleNamespace(ok=True))
    assert result["server_branch_dag_statements"] == 0
    _, result = SQLTrace(SimpleNamespace()).measure("log", lambda: SimpleNamespace(ok=True))
    assert "server_branch_dag_statements" not in result
