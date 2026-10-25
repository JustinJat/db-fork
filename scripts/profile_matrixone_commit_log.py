"""Compare MERGE log copying and isolate commit-id index costs on scratch logs.

    MO_PORT=16001 python -m scripts.profile_matrixone_commit_log \
        --rows 10 1000 10000 --reps 3 --out /tmp/mo-commit-log.json

Requires an exclusive disposable server. Each source has N synthetic commits;
the destination already contains 90% of them. Compare the former OR predicate
with the current adapter, with and without _bb_commit_id. Timings exclude seed,
BEGIN, validation, ROLLBACK, and cleanup. Copy and append statements run inside
transactions that are rolled back after checking the result. These are warm
log-statement timings, not complete commit/MERGE/clone or storage measurements.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import median
import subprocess
import time
import uuid

from dblib import matrixone as mo


def legacy_copy_sql(target, source):
    """The adapter's pre-optimization statement, retained for controlled A/B."""
    cols = mo.MatrixOneToolSuite._LOG_COLS
    return (f"INSERT INTO {mo._qt(target, mo.COMMITS_TABLE)} ({', '.join(cols)}) "
            f"SELECT {', '.join('s.' + c for c in cols)} "
            f"FROM {mo._qt(source, mo.COMMITS_TABLE)} s WHERE s.kind IN ('commit', 'merge') "
            f"AND NOT EXISTS (SELECT 1 FROM {mo._qt(target, mo.COMMITS_TABLE)} d "
            "WHERE d.seq = s.seq OR d.id = s.id)")


def run_size(conn, count, reps):
    prefix = "bb_log_profile_" + uuid.uuid4().hex[:10]
    databases = []
    suites = {}
    runs, plans = [], {}

    def query(sql, args=None):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def totals(db):
        return tuple(map(int, query(
            f"SELECT COUNT(*), COALESCE(SUM(seq),0) FROM {mo._qt(db, mo.COMMITS_TABLE)}")[0]))

    overlap = count * 9 // 10
    try:
        for indexed in (True, False):
            source, target = [prefix + ("_idx" if indexed else "_plain") + suffix
                              for suffix in ("_src", "_dst")]
            for db in (source, target):
                query(f"CREATE DATABASE {mo._quote(db)}")
                databases.append(db)
                query(mo._COMMITS_DDL.format(table=mo._qt(db, mo.COMMITS_TABLE)))
                if not indexed:
                    query(f"DROP INDEX _bb_commit_id ON {mo._qt(db, mo.COMMITS_TABLE)}")
            columns = ", ".join(mo.MatrixOneToolSuite._LOG_COLS)
            for start in range(1, count + 1, 1000):
                values = [(i, f"c{i}", "commit", "synthetic history", "snapshot", source,
                           None, None, datetime(2026, 1, 1), "{}")
                          for i in range(start, min(count + 1, start + 1000))]
                with conn.cursor() as cur:
                    cur.executemany(f"INSERT INTO {mo._qt(source, mo.COMMITS_TABLE)} ({columns}) "
                                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", values)
            query(f"INSERT INTO {mo._qt(target, mo.COMMITS_TABLE)} "
                  f"SELECT * FROM {mo._qt(source, mo.COMMITS_TABLE)} WHERE seq <= %s", (overlap,))
            suite = object.__new__(mo.MatrixOneToolSuite)
            sql = []
            suite._execute = sql.append
            suite._copy_log(target, source)
            current = sql[0]
            suite._execute = query
            suites[indexed] = suite, source, target
            plans[str(indexed)] = {
                variant: [row[0] for row in query("EXPLAIN " + statement)]
                for variant, statement in (("legacy_or", legacy_copy_sql(target, source)),
                                            ("adapter", current))
            }

        for rep in range(reps):
            for indexed in ((True, False) if rep % 2 == 0 else (False, True)):
                suite, source, target = suites[indexed]
                actions = {
                    "legacy_or": lambda: query(legacy_copy_sql(target, source)),
                    "adapter": lambda: suite._copy_log(target, source),
                    "lookup_hit": lambda: suite._find_commit(target, f"c{overlap}"),
                    "lookup_miss": lambda: suite._find_commit(target, "missing"),
                    "append_log_row": lambda: suite._add_commit(
                        target, "commit", "appended", "snapshot", target, None, None,
                        seq=count + 1, commit_id="appended", schema_json="{}"),
                }
                order = list(actions) if rep % 2 == 0 else list(reversed(actions))
                for operation in order:
                    query("BEGIN")
                    try:
                        start = time.perf_counter()
                        try:
                            result = actions[operation]()
                        except ValueError:
                            if operation != "lookup_miss":
                                raise
                            result = None
                        elapsed = (time.perf_counter() - start) * 1000
                        if operation in ("legacy_or", "adapter"):
                            assert totals(target) == (count, count * (count + 1) // 2)
                        elif operation == "append_log_row":
                            assert result == "appended"
                            assert totals(target) == (overlap + 1, overlap * (overlap + 1) // 2 + count + 1)
                        elif operation == "lookup_hit":
                            assert result["id"] == f"c{overlap}"
                        else:
                            assert result is None
                        runs.append({"rows": count, "destination_rows": overlap,
                                     "indexed": indexed, "repetition": rep,
                                     "operation": operation, "elapsed_ms": elapsed})
                    finally:
                        query("ROLLBACK")
                    assert totals(target) == (overlap, overlap * (overlap + 1) // 2)
        return runs, plans
    finally:
        for db in reversed(databases):
            query(f"DROP DATABASE IF EXISTS {mo._quote(db)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, nargs="+", default=[10, 1000, 10000])
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if min(args.rows) < 2 or args.reps < 1:
        parser.error("at least 2 rows and 1 repetition are required")
    conn = mo.connect(None)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), git_version()")
            version, revision = cur.fetchone()
        report = {
            "adapter_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "adapter_dirty": bool(subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip()),
            "adapter_source_sha256": hashlib.sha256(Path(mo.__file__).read_bytes()).hexdigest(),
            "endpoint": f"{mo.MO_HOST}:{mo.MO_PORT}",
            "server": {"version": str(version), "git_version": str(revision)},
            "scope": "synthetic log statements inside BEGIN/ROLLBACK; excludes transaction boundaries, "
                     "setup, validation, cleanup, schema fingerprint, snapshots, and clone/storage costs",
            "runs": [], "plans": {},
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        for count in args.rows:
            runs, plans = run_size(conn, count, args.reps)
            report["runs"].extend(runs)
            report["plans"][str(count)] = plans
            args.out.write_text(json.dumps(report, indent=2) + "\n")
            for indexed in (True, False):
                medians = {operation: round(median(r["elapsed_ms"] for r in runs
                           if r["indexed"] == indexed and r["operation"] == operation), 3)
                           for operation in dict.fromkeys(r["operation"] for r in runs)}
                print(count, "indexed" if indexed else "unindexed", medians, flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
