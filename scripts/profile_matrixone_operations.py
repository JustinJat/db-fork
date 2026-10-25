"""Count client SQL and received bytes for a reproducible branch workflow.

    MO_PORT=6001 python -m scripts.profile_matrixone_operations \
        --tables 1 10 --rows 1000 --changed 100 --reps 3 --out /tmp/mo-profile.json

Creates uniquely named scratch databases and removes them in finally blocks.
Setup/seed/cleanup and connection handshakes are excluded. Counts cover all
COM_QUERY calls, including queries made directly through cursors. Received
bytes include MySQL packet headers. These client counts exclude internal SQL/scans.
Run against a disposable local server for comparable latency measurements.
With --server-log, also count DataBranch events appended to a local JSON log
at INFO level, plus logged LCA SQL in server_lca_probe_statements and branch
graph reads in server_branch_dag_statements. This requires
an exclusive server with no log rotation; HashDiff events count internal diff
passes, not individual SQL.
Instrumented servers also report exclusive merge phase wall times. Request
totals include table timings and transaction finish; do not add table totals
again. Missing or incomplete timing logs are explicitly marked in the JSON.
--mutation delete replaces source updates with deletions; both workloads insert
one source row and update half the changed keys on the destination (conflicts).
--history-rows adds synthetic old commit-log rows immediately before MERGE,
with 90% already on the destination. Rows reuse the real base snapshot; this
models log size, not thousands of independent snapshots or data versions.
--log-copy legacy_or adapter compares the former log-copy SQL with the current
adapter inside complete workflows. This is independent of --modes (the native
MERGE interface). Seeding and history checks are outside operation timings.
"""

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
import uuid

from dblib import matrixone as mo
from dblib import result_collector as rc
from dblib.db_api import Ref
from scripts.profile_matrixone_commit_log import legacy_copy_sql


def summarize_merge_timings(records, expected_requests):
    """Correlate table events and sum only complete request-level measurements."""
    requests = [r for r in records if r.get("msg") == "DataBranch-Merge-Request-Timing"]
    tables = [r for r in records if r.get("msg") == "DataBranch-Merge-Table-Timing"]
    issues = []
    if not requests:
        issues.append("no request timings captured; server may lack instrumentation")
    if len(requests) != expected_requests:
        issues.append(f"expected {expected_requests} request timings, captured {len(requests)}")
    ids = [r.get("request_id") for r in requests]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(map(str, ids))) != len(ids):
        issues.append("missing or duplicate request IDs")

    def duration(value):
        return (type(value) in (int, float) and math.isfinite(value) and value >= 0)

    for record in requests + tables:
        phases = record.get("phases_ms")
        valid = (record.get("schema_version") == 1
                 and record.get("status") in ("ok", "error", "panic")
                 and duration(record.get("total_ms"))
                 and isinstance(phases, dict) and bool(phases)
                 and all(duration(v) for v in phases.values()))
        if not valid:
            issues.append("invalid or unsupported timing record")
        elif not math.isclose(sum(phases.values()), record["total_ms"], rel_tol=1e-6, abs_tol=1e-6):
            issues.append("phase durations do not sum to total_ms")
    for request in requests:
        children = [t for t in tables if t.get("request_id") == request.get("request_id")]
        if type(request.get("table_count")) is not int or request["table_count"] != len(children):
            issues.append("incomplete table timings for a request")
    if any(t.get("request_id") not in ids for t in tables):
        issues.append("table timing has no completed request")
    complete = bool(requests) and not issues
    phases = Counter()
    if complete:
        for request in requests:
            phases.update(request["phases_ms"])
    return {
        "available": bool(requests or tables),
        "complete": complete,
        "issues": list(dict.fromkeys(issues)),
        "total_ms": sum(r["total_ms"] for r in requests) if complete else None,
        "phases_ms": dict(phases) if complete else None,
        "requests": requests,
        "tables": tables,
    }


def summarize_restore_work(records, expected_requests, operation):
    """Restore-owned SQL only; catalog SQL and transaction finish are excluded."""
    work = [r for r in records if r.get("msg") == "DataBranch-Restore-Work"]
    issues = []
    if not work:
        issues.append("no restore work records; server may lack instrumentation or use a legacy path")
    if len(work) != expected_requests:
        issues.append(f"expected {expected_requests} restore records, captured {len(work)}")
    ids = [r.get("request_id") for r in work]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(map(str, ids))) != len(ids):
        issues.append("missing or duplicate restore IDs")
    keys = {"stage_create", "delete", "update", "insert", "stage_drop"}

    def integer(v):
        return type(v) is int and v >= 0

    def duration(v):
        return type(v) in (int, float) and math.isfinite(v) and v >= 0

    for record in work:
        counts, phases = record.get("sql_counts"), record.get("phases_ms")
        valid = (record.get("schema_version") == 1 and record.get("operation") == operation
                 and record.get("status") in ("ok", "error", "panic")
                 and all(integer(record.get(k)) for k in ("table_count", "sql_statements", "empty_stages",
                                                          "unknown_stages", "materialized_rows"))
                 and isinstance(counts, dict) and set(counts) == keys
                 and all(integer(v) for v in counts.values())
                 and isinstance(phases, dict) and set(phases) == keys | {"other"}
                 and all(duration(v) for v in phases.values()) and duration(record.get("total_ms")))
        if not valid:
            issues.append("invalid or unsupported restore work record")
            continue
        if sum(counts.values()) != record["sql_statements"]:
            issues.append("restore SQL counts do not sum to sql_statements")
        if record["empty_stages"] + record["unknown_stages"] > counts["stage_create"]:
            issues.append("restore stage counts exceed materialization attempts")
        if not math.isclose(sum(phases.values()), record["total_ms"], rel_tol=1e-6, abs_tol=1e-6):
            issues.append("restore phases do not sum to total_ms")
    complete = bool(work) and not issues
    counts, phases = Counter(), Counter()
    if complete:
        for record in work:
            counts.update(record["sql_counts"])
            phases.update(record["phases_ms"])
    return {"available": bool(work), "complete": complete, "issues": list(dict.fromkeys(issues)),
            "scope": "restore stage/apply/cleanup statements; excludes nested CTAS INSERTs, "
                     "catalog/authorization SQL and transaction finish; status is work status only",
            "sql_statements": sum(r["sql_statements"] for r in work) if complete else None,
            "sql_counts": dict(counts) if complete else None,
            "total_ms": sum(r["total_ms"] for r in work) if complete else None,
            "phases_ms": dict(phases) if complete else None, "records": work}


class SQLTrace:
    """Trace one buffered PyMySQL connection without changing the adapter."""

    def __init__(self, conn, server_log=None):
        self.conn = conn
        self.server_log = Path(server_log) if server_log else None
        self.records = []
        self.received_bytes = 0

    def __enter__(self):
        self.query = self.conn.query
        self.read_bytes = self.conn._read_bytes
        self.conn.query = self._query
        self.conn._read_bytes = self._read_bytes
        return self

    def __exit__(self, *exc):
        self.conn.query = self.query
        self.conn._read_bytes = self.read_bytes

    def _read_bytes(self, *args, **kwargs):
        data = self.read_bytes(*args, **kwargs)
        self.received_bytes += len(data)
        return data

    def _query(self, sql, *args, **kwargs):
        start, received = time.perf_counter(), self.received_bytes
        error = None
        try:
            return self.query(sql, *args, **kwargs)
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            result = self.conn._result
            text = sql.decode(self.conn.encoding) if isinstance(sql, bytes) else sql
            self.records.append({
                "sql": text,
                "elapsed_ms": (time.perf_counter() - start) * 1000,
                "received_bytes": self.received_bytes - received,
                "returned_rows": len(result.rows or ()) if not error and result else 0,
                "error": error,
            })

    def measure(self, operation, call):
        self.records.clear()
        offset = self.server_log.stat().st_size if self.server_log else None
        start = time.perf_counter()
        result = call()
        elapsed = (time.perf_counter() - start) * 1000
        if hasattr(result, "ok") and not result.ok:
            raise RuntimeError(f"{operation}: {result.error}")
        measurement = {
            "operation": operation,
            "elapsed_ms": elapsed,
            "statements": len(self.records),
            "received_bytes": sum(r["received_bytes"] for r in self.records),
            "returned_rows": sum(r["returned_rows"] for r in self.records),
            "statement_kinds": dict(Counter(" ".join(r["sql"].split()[:3]).upper()
                                            for r in self.records)),
            "queries": list(self.records),
            "execution_paths": list(getattr(result, "execution_paths", [])),
            "fallback_reasons": list(getattr(result, "fallback_reasons", [])),
        }
        if isinstance(getattr(result, "value", None), dict) and "merge_summary" in result.value:
            measurement["merge_summary"] = result.value["merge_summary"]
        if self.server_log:
            lca_probes = 0
            branch_dag_reads = 0
            events = Counter()
            records = []
            with self.server_log.open("rb") as log:
                log.seek(offset)
                for line in log:
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    event = record.get("msg", "")
                    if not isinstance(event, str):
                        continue
                    sql = record.get("sql")
                    if (event == "sql_executor exec" and isinstance(sql, str)
                            and sql.startswith("select pks.`__mo_data_branch_ordinal`")
                            and " as lca right join (values " in sql):
                        lca_probes += 1
                    if (event == "sql_executor exec" and sql ==
                            "select table_id, clone_ts, p_table_id, level, table_deleted "
                            "from mo_catalog.mo_branch_metadata"):
                        branch_dag_reads += 1
                    if event.startswith("DataBranch-"):
                        events[event] += 1
                        records.append(record)
            # Count executor log events, not client requests or duplicate SQL
            # text from other loggers. Requires the same exclusive INFO log.
            measurement["server_lca_probe_statements"] = lca_probes
            measurement["server_branch_dag_statements"] = branch_dag_reads
            measurement["server_events"] = dict(events)
            if operation == "merge":
                expected = sum(r["error"] is None and r["sql"].lstrip().upper().startswith("DATA BRANCH MERGE ")
                               for r in self.records)
                measurement["server_merge_timing"] = summarize_merge_timings(records, expected)
            if operation in ("reset", "revert"):
                prefix = f"DATA BRANCH {operation.upper()} DATABASE "
                expected = sum(r["error"] is None and r["sql"].lstrip().upper().startswith(prefix)
                               for r in self.records)
                measurement["server_restore_work"] = summarize_restore_work(records, expected, operation)
                # Catalog events cover enumeration before authorization/planning,
                # separately from stage/apply/cleanup work and client requests.
                measurement["server_restore_metadata"] = [
                    r for r in records if r.get("msg") == "DataBranch-Restore-Metadata"]
            if operation in ("diff", "merge") and not events["DataBranch-HashDiff-Done"]:
                raise RuntimeError("No HashDiff events captured; use an exclusive server with INFO JSON logging")
        return result, measurement


def server_identity():
    """Read build identity outside all measured operations."""
    conn = mo.connect(None)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), git_version()")
            version, git_version = cur.fetchone()
            return {"version": str(version), "git_version": str(git_version)}
    finally:
        conn.close()


def seed_merge_history(suite, target, source, base_commit, count):
    """Seed only the scratch workflow's logs, without changing their real heads."""
    if count <= 0:
        raise ValueError("history seed count must be positive")
    shared = count * 9 // 10
    template = suite._find_commit(target, base_commit)
    before = {}
    # Check both logs before writing either. Low sequences keep all synthetic
    # entries before real commits, including the commits reverted/reset below.
    for db in (target, source):
        total, first = suite._execute(
            f"SELECT COUNT(*), MIN(seq) FROM {mo._qt(db, mo.COMMITS_TABLE)}")[0]
        if first is None or int(first) <= count:
            raise ValueError("synthetic history would overlap the real commit sequence range")
        before[db] = int(total)
    columns = ", ".join(suite._LOG_COLS)
    for db, size in ((source, count), (target, shared)):
        for start in range(1, size + 1, 1000):
            values = [(i, f"profile-history-{i:x}", "commit", "synthetic profiling history",
                       template["snap"], template["db"], template["prev_snap"], template["prev_db"],
                       template["ts"], template["schema_json"])
                      for i in range(start, min(size + 1, start + 1000))]
            with suite.conn.cursor() as cur:
                cur.executemany(f"INSERT INTO {mo._qt(db, mo.COMMITS_TABLE)} ({columns}) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", values)
    return {"synthetic_source_rows": count, "synthetic_shared_rows": shared,
            "source_rows_before_merge": before[source] + count,
            "destination_rows_before_merge": before[target] + shared,
            "template_commit": base_commit, "template_snapshot": template["snap"]}


def check_merge_history(suite, target, count):
    stats = suite._execute(
        f"SELECT COUNT(*), COUNT(DISTINCT id), COALESCE(SUM(seq),0) "
        f"FROM {mo._qt(target, mo.COMMITS_TABLE)} WHERE seq BETWEEN 1 AND %s", (count,))[0]
    if tuple(map(int, stats)) != (count, count, count * (count + 1) // 2):
        raise AssertionError(("copied history", stats, count))


def run_once(table_count, rows, changed, mode, backend=mo, server_log=None, mutation="update",
             history_rows=0, log_copy="adapter"):
    if mutation not in ("update", "delete"):
        raise ValueError(f"unsupported mutation: {mutation}")
    if history_rows < 0:
        raise ValueError("history_rows must be nonnegative")
    if log_copy not in ("adapter", "legacy_or"):
        raise ValueError(f"unsupported log_copy: {log_copy}")
    db = "bb_mop_" + uuid.uuid4().hex[:10]
    suite = None
    measurements = []
    history = None
    try:
        backend.setup_database(db)
        suite = backend.MatrixOneToolSuite.init_for_bench(rc.ResultCollector(), db)
        suite._merge_summary_supported = False if mode == "legacy" else None
        if hasattr(suite, "_database_branch_supported"):
            suite._database_branch_supported = {
                "diff": None if mode == "database" else False,
                "merge": None if mode == "database" else False,
            }
        if hasattr(suite, "_database_restore_supported"):
            suite._database_restore_supported = {
                "reset": None if mode == "database" else False,
                "revert": None if mode == "database" else False,
            }
        with suite.conn.cursor() as cur:
            for i in range(table_count):
                cur.execute(f"CREATE TABLE t{i}(id INT PRIMARY KEY, v INT)")
                cur.executemany(f"INSERT INTO t{i} VALUES (%s,%s)", ((k, k) for k in range(rows)))

        def check(result):
            if not result.ok:
                raise RuntimeError(result.error)
            return result.value

        with SQLTrace(suite.conn, server_log) as trace:
            def measure(name, call):
                result, data = trace.measure(name, call)
                measurements.append(data)
                return result.value if hasattr(result, "value") else result

            base = check(suite.commit("main", "base"))
            measure("create", lambda: suite.branch("feature", "main"))
            # This is the adapter's branch switch, not a TCP handshake.
            measure("connect", lambda: suite._connect_impl(Ref("feature")))
            check(suite.exec([sql for i in range(table_count) for sql in (
                (f"DELETE FROM t{i} WHERE id < {changed}" if mutation == "delete"
                 else f"UPDATE t{i} SET v=v+1 WHERE id < {changed}"),
                f"INSERT INTO t{i} VALUES ({rows}, {rows})",
            )], refs=["feature"])[0])
            feature = measure("commit", lambda: suite.commit("feature", "feature work"))
            measure("log", lambda: suite.log("feature"))
            check(suite.exec([f"UPDATE t{i} SET v=v+2 WHERE id < {changed // 2}"
                              for i in range(table_count)], refs=["main"])[0])
            check(suite.commit("main", "main work"))
            measure("diff", lambda: suite.diff(f"main@{base}", f"feature@{feature}"))
            if history_rows:
                target, source = suite._db_for("main"), suite._db_for("feature")
                history = seed_merge_history(suite, target, source, base, history_rows)
            original_copy = suite._copy_log
            copy_times = []

            def copy_log(into_db, from_db, max_seq=None):
                start = time.perf_counter()
                try:
                    if log_copy == "adapter":
                        return original_copy(into_db, from_db, max_seq=max_seq)
                    sql = legacy_copy_sql(into_db, from_db)
                    if max_seq is not None:
                        sql += f" AND s.seq <= {int(max_seq)}"
                    return suite._execute(sql)
                finally:
                    copy_times.append((time.perf_counter() - start) * 1000)

            # Override only this scratch suite and only during the measured
            # MERGE; the production adapter and other operations stay intact.
            suite._copy_log = copy_log
            try:
                merged = measure("merge", lambda: suite.merge("main", "feature", on_conflict="ours"))
            finally:
                suite._copy_log = original_copy
            merge_measurement = measurements[-1]
            if len(copy_times) != 1:
                raise AssertionError(("expected one log copy", copy_times))
            merge_measurement["client_merge_timing"] = {
                "total_ms": merge_measurement["elapsed_ms"], "log_copy_ms": copy_times[0],
                "other_ms": merge_measurement["elapsed_ms"] - copy_times[0],
                "log_copy_calls": len(copy_times),
            }
            if history_rows:
                check_merge_history(suite, target, history_rows)
            if mode == "summary" and suite._merge_summary_supported is not True:
                raise RuntimeError("Server does not support MERGE OUTPUT SUMMARY")
            if mode == "database" and suite._database_branch_supported["merge"] is not True:
                raise RuntimeError("Server does not support database MERGE OUTPUT SUMMARY")
            expected_conflicts = table_count * (changed // 2)
            if merged["conflicts"] != expected_conflicts:
                raise AssertionError((merged["conflicts"], expected_conflicts))
            for i in range(table_count):
                actual = suite._execute(f"SELECT COUNT(*), SUM(v) FROM t{i}")[0]
                expected_count = rows + 1
                expected_sum = rows * (rows - 1) // 2 + rows + changed + changed // 2
                if mutation == "delete":
                    # SKIP preserves the destination's updated first half; only
                    # the source's remaining deletions are applied.
                    half = changed // 2
                    deleted = changed - half
                    expected_count -= deleted
                    deleted_sum = (half + changed - 1) * deleted // 2
                    expected_sum = rows * (rows - 1) // 2 + rows + 2 * half - deleted_sum
                if tuple(map(int, actual)) != (expected_count, expected_sum):
                    raise AssertionError((actual, expected_count, expected_sum))
            measure("revert", lambda: suite.revert("main", merged["hash"]))
            if mode == "database" and suite._database_restore_supported["revert"] is not True:
                raise RuntimeError("Server-side database REVERT was not used")
            for i in range(table_count):
                actual = suite._execute(f"SELECT COUNT(*), SUM(v) FROM t{i}")[0]
                if tuple(map(int, actual)) != (rows, rows * (rows - 1) // 2 + 2 * (changed // 2)):
                    raise AssertionError(("revert", actual))
            measure("reset", lambda: suite.reset("main", base))
            if mode == "database" and suite._database_restore_supported["reset"] is not True:
                raise RuntimeError("Server-side database RESET was not used")
            measure("delete", lambda: suite.delete("feature"))
            for i in range(table_count):
                actual = suite._execute(f"SELECT COUNT(*), SUM(v) FROM t{i}")[0]
                if tuple(map(int, actual)) != (rows, rows * (rows - 1) // 2):
                    raise AssertionError(actual)
        return {"mode": mode, "tables": table_count, "rows_per_table": rows,
                "changed_per_table": changed, "mutation": mutation, "measurements": measurements,
                "history_rows": history_rows, "history": history, "log_copy": log_copy}
    finally:
        if suite is not None:
            suite.close_connection()
        backend.drop_database(db)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables", type=int, nargs="+", default=[1, 10])
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--changed", type=int, default=100)
    parser.add_argument("--mutation", choices=["update", "delete"], default="update")
    parser.add_argument("--history-rows", type=int, nargs="+", default=[0],
                        help="synthetic source log rows added before MERGE; destination shares 90%%")
    parser.add_argument("--log-copy", nargs="+", choices=["adapter", "legacy_or"], default=["adapter"],
                        help="log-copy SQL strategy, separate from the native MERGE interface")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=["legacy", "summary", "database"],
                        default=["legacy", "summary", "database"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--server-log", type=Path, help="local INFO JSON log of an exclusive test server")
    args = parser.parse_args()
    if min(args.tables) < 1 or args.reps < 1 or not 0 < args.changed <= args.rows or min(args.history_rows) < 0:
        parser.error("positive table/repetition counts, 0 < changed <= rows, and nonnegative history rows are required")
    if not mo.MO_COUNT_CONFLICTS:
        parser.error("conflict reporting must be enabled for correctness checks")
    if args.server_log and not args.server_log.is_file():
        parser.error("--server-log must name an existing file")
    report = {
        "adapter_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "adapter_dirty": bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip()),
        "adapter_source_sha256": hashlib.sha256(Path(mo.__file__).read_bytes()).hexdigest(),
        "profiler_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "endpoint": f"{mo.MO_HOST}:{mo.MO_PORT}",
        "server": server_identity(),
        "scope": "client COM_QUERY only; excludes setup, seed (including synthetic history), "
                 "validation, cleanup, and handshake; client_merge_timing partitions the complete "
                 "adapter MERGE call, with server timings nested inside its other_ms",
        "runs": [],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for count in args.tables:
        for history_rows in args.history_rows:
            for rep in range(args.reps):
                # Alternate both native interface and log-copy strategy order.
                variants = [(mode, copy) for mode in args.modes for copy in args.log_copy]
                for mode, copy in (variants if rep % 2 == 0 else list(reversed(variants))):
                    run = run_once(count, args.rows, args.changed, mode, server_log=args.server_log,
                                   mutation=args.mutation, history_rows=history_rows, log_copy=copy)
                    run["repetition"] = rep
                    report["runs"].append(run)
                    args.out.write_text(json.dumps(report, indent=2) + "\n")
                    print(mode, count, "history", history_rows, copy,
                          {m["operation"]: m["statements"] for m in run["measurements"]}, flush=True)


if __name__ == "__main__":
    main()
