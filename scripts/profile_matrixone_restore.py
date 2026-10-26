"""Measure native RESET/REVERT on an exclusive disposable MatrixOne server.

MO_PORT=16001 python -m scripts.profile_matrixone_restore --tables 1 10 \
    --rows 1000 --changed 100 --reps 3 --server-log /tmp/mo.log --out /tmp/restore.json

Mutation names describe changes being undone. Each sample creates fresh tables
and snapshots, validates all rows and relation IDs, and removes its scratch data.
Only the single native restore call is timed. Server counters cover restore's
stage/apply/cleanup SQL, excluding nested CTAS INSERTs and catalog/transaction SQL.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

from dblib import matrixone as mo
from scripts.profile_matrixone_operations import SQLTrace, server_identity


CASES = ("noop", "insert", "delete", "update", "mixed")


def run_once(tables, rows, changed, operation, mutation, server_log):
    if tables < 1 or not 0 < 2 * changed < rows or operation not in ("reset", "revert") or mutation not in CASES:
        raise ValueError("positive tables, 0 < 2 * changed < rows, and supported operation/mutation required")
    conn = mo.connect(None)
    db = "bb_restore_profile_" + uuid.uuid4().hex[:12]
    before, after = db + "_before", db + "_after"
    names = [f"t{i}" for i in range(tables)]
    snapshots = []

    def query(sql, args=None):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def identities():
        return query("select relname,rel_id from mo_catalog.mo_tables where reldatabase=%s order by relname", (db,))

    try:
        query(f"create database {db}")
        for name in names:
            query(f"create table {db}.{name}(id int primary key,v int)")
            for start in range(0, rows, 1000):
                with conn.cursor() as cur:
                    cur.executemany(f"insert into {db}.{name} values(%s,%s)",
                                    [(i, i * 10) for i in range(start, min(rows, start + 1000))])
        original_ids = identities()
        query(f"create snapshot {before} for database {db}")
        snapshots.append(before)
        for name in names:
            if mutation in ("delete", "mixed"):
                query(f"delete from {db}.{name} where id < {changed}")
            if mutation in ("update", "mixed"):
                start = changed if mutation == "mixed" else 0
                query(f"update {db}.{name} set v=v+1 where id >= {start} and id < {start + changed}")
            if mutation in ("insert", "mixed"):
                with conn.cursor() as cur:
                    cur.executemany(f"insert into {db}.{name} values(%s,%s)",
                                    [(i, i * 10) for i in range(rows, rows + changed)])
        expected = [(i, i * 10) for i in range(rows)]
        if operation == "revert":
            query(f"create snapshot {after} for database {db}")
            snapshots.append(after)
            # REVERT must preserve changes outside the reversed delta.
            extra = rows + changed + 99
            for name in names:
                query(f"update {db}.{name} set v=v+10000 where id={rows - 1}")
                query(f"insert into {db}.{name} values({extra},99)")
            expected[-1] = (rows - 1, (rows - 1) * 10 + 10000)
            expected.append((extra, 99))
            sql = (f"DATA BRANCH REVERT DATABASE {db} FROM {db}{{snapshot='{after}'}} "
                   f"AGAINST {db}{{snapshot='{before}'}} TABLES({','.join(names)})")
        else:
            sql = f"DATA BRANCH RESET DATABASE {db} FROM {db}{{snapshot='{before}'}} TABLES({','.join(names)})"
        with SQLTrace(conn, server_log) as trace:
            _, measurement = trace.measure(operation, lambda: query(sql))
        for name in names:
            actual = list(query(f"select id,v from {db}.{name} order by id"))
            if actual != expected:
                raise AssertionError(f"{operation}/{mutation}/{name}: restored rows differ")
        if identities() != original_ids:
            raise AssertionError("relation IDs changed or staging tables leaked")
        work = measurement["server_restore_work"]
        if not work["complete"]:
            raise AssertionError(work["issues"])
        record, = work["records"]
        expected_rows = changed * tables * (3 if mutation == "mixed" else 0 if mutation == "noop" else 1)
        expected_empty = tables * (2 if mutation == "noop" else 0 if mutation == "mixed" else 1)
        if (record["unknown_stages"] != 0 or record["materialized_rows"] != expected_rows
                or record["empty_stages"] != expected_empty or record["table_count"] != tables
                or record["status"] != "ok" or measurement["statements"] != 1):
            raise AssertionError(f"unexpected restore stage accounting: {record}")
        catalogs = measurement["server_restore_metadata"]
        if catalogs:
            if len(catalogs) != 1:
                raise AssertionError("expected one catalog record per native restore")
            catalog = catalogs[0]
            endpoints = 3 if operation == "revert" else 2
            if (catalog.get("schema_version") != 1 or catalog.get("operation") != operation
                    or catalog.get("database_opens") != endpoints
                    or catalog.get("snapshot_resolutions") != endpoints - 1
                    or catalog.get("relation_opens") != endpoints * tables):
                raise AssertionError(f"unexpected restore metadata accounting: {catalog}")
        return {"tables": tables, "rows_per_table": rows, "changed_per_table": changed,
                "mutation": mutation, "operation": operation, "validated": True, "measurement": measurement}
    finally:
        try:
            query(f"drop database if exists {db}")
        finally:
            try:
                for snapshot in reversed(snapshots):
                    query(f"drop snapshot if exists {snapshot}")
            finally:
                conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables", type=int, nargs="+", default=[1, 10])
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--changed", type=int, default=100)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--server-log", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if min(args.tables) < 1 or args.reps < 1 or not 0 < 2 * args.changed < args.rows:
        parser.error("positive tables/reps and 0 < 2 * changed < rows required")
    if not args.server_log.is_file():
        parser.error("--server-log must be an existing exclusive INFO JSON log")
    report = {
        "adapter_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "adapter_dirty": bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip()),
        "adapter_source_sha256": hashlib.sha256(Path(mo.__file__).read_bytes()).hexdigest(),
        "profiler_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "trace_source_sha256": hashlib.sha256(Path(__file__).with_name("profile_matrixone_operations.py").read_bytes()).hexdigest(),
        "endpoint": f"{mo.MO_HOST}:{mo.MO_PORT}", "server": server_identity(),
        "scope": "one native restore statement; setup/validation/cleanup excluded; server work timings exclude transaction finish",
        "runs": [],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for tables in args.tables:
        for rep in range(args.reps):
            variants = [(op, case) for case in CASES for op in ("reset", "revert")]
            for operation, mutation in (variants if rep % 2 == 0 else reversed(variants)):
                run = run_once(tables, args.rows, args.changed, operation, mutation, args.server_log)
                run["repetition"] = rep
                report["runs"].append(run)
                args.out.write_text(json.dumps(report, indent=2) + "\n")
                m = run["measurement"]
                work = m["server_restore_work"]
                print(tables, operation, mutation,
                      "staging", work["records"][0].get("staging", "unreported"),
                      "sql", work["sql_statements"], "ms", round(m["elapsed_ms"], 2),
                      "create_ms", round(work["phases_ms"]["stage_create"], 2),
                      "drop_ms", round(work["phases_ms"]["stage_drop"], 2), flush=True)


if __name__ == "__main__":
    main()
