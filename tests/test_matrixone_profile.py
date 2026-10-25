"""Merge timing logs must distinguish unavailable data and avoid double counting."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.profile_matrixone_operations import SQLTrace, summarize_merge_timings


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
