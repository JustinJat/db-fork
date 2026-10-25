# Database Benchmarking Framework

A parametrized and extensible benchmarking framework for branchable database
backends (Dolt, Neon, Xata, SeekDB, MatrixOne, Bauplan, plain Postgres copies,
...). Every backend
is driven through one git-like API (`dblib/db_api.py`): `branch`, `commit`,
`diff`, `log`, `merge`, `rebase`, `revert`, `reset`, `delete`, plus `exec()`
to run a workload script on one or more branches. Each operation is timed and
storage-measured; an operation that errors is recorded and the workload goes
on, while one the backend cannot perform ends the run as unsupported, naming
the verb. Includes both macrobenchmark and
microbenchmark workloads.

## Quick Start

```bash
# 1. Setup environment (installs Python 3.13 and all dependencies into .venv)
#    Requires uv (https://docs.astral.sh/uv/) and protoc (brew install protobuf)
uv sync

# 2. Run a macrobenchmark scenario (schema and data are generated)
# Mini config, always start with this
./scripts/run_macrobench.sh --mini --outdir run_stats rl_env dolt

# Full config at W=5 with a 2h timeout
./scripts/run_macrobench.sh --outdir run_stats --max-runtime-sec 7200 data_agent dolt 5

# 3. Run a microbenchmark (latency)
./scripts/run_single_thread_bench.sh dolt db_setup/tpcc_schema.sql 16

# 4. Run a microbenchmark (throughput)
./scripts/run_throughput_bench.sh dolt db_setup/ch-w1.sql --sweep-proportional

# 5. Run the unit tests (fake backend, no server needed)
uv run python -m pytest tests/
```

All commands are run from the repository root.

### Repository Layout

```
dblib/              # The git-like DB API, backend implementations, result collection
microbench/         # Microbenchmark runner and operations
macrobench/         # Macrobenchmark scenarios, schema generators and runner
util/               # Shared helpers (SQL loading, DB utilities)
db_setup/           # SQL dumps/schemas and database setup scripts
scripts/            # Benchmark entry points (run_*.sh)
scripts/plotting/   # Plotting and analysis scripts
tests/              # API tests against an in-memory fake backend
```

---

## Table of Contents

- [Database API](#database-api)
- [Macrobenchmarks](#macrobenchmarks)
- [Microbenchmarks](#microbenchmarks)
  - [Latency Benchmarks](#latency-benchmarks)
  - [Throughput Benchmarks](#throughput-benchmarks)
- [Plotting Results](#plotting-results)
- [Output Files](#output-files)

---

## Database API

`dblib/db_api.py` defines `DBToolSuite`, the interface every backend
implements. A backend overrides the protected hooks it supports
(`_branch_impl`, `_commit_impl`, ..., `_storage_bytes`, `_connect_impl`); the
public verbs wrap them with timing, storage measurement and result recording.

### Verbs

| Verb | Meaning | Returns |
|------|---------|---------|
| `branch(name, from_ref)` | create a branch | |
| `commit(ref, message)` | snapshot the branch's working state | commit id |
| `diff(ref_a, ref_b)` | differences between two refs | backend-specific summary |
| `log(ref, limit)` | recent commits | list of dicts |
| `merge(into, source, message, on_conflict)` | merge `source` into `into` | `fast_forward`, `conflicts`, `conflict_tables` (Dolt) |
| `rebase(ref, onto, on_conflict)` | replay `ref`'s commits on `onto` | `conflicts`, `conflict_tables` (Dolt) |
| `revert(ref, commit)` | add a commit undoing `commit` | |
| `reset(ref, to)` | move `ref` to a commit (or restore point) | |
| `delete(ref)` | delete a branch | |

A ref is a branch name or `branch@commit` on backends with commits. On a
backend without commits, a commit ref falls back to the branch head with a
warning and the row is flagged `commit_ref_fallback`.

`merge()` and `rebase()` take `on_conflict`: `"ours"` (default), `"theirs"`,
or a callable `resolve(db, conflicts)` that the backend calls on the
half-merged working set with a session and a list of
`{"table": name, "rows": [...]}` entries (Dolt's base/our/their columns).
The callable resolves conflicts with SQL through `db.sql()`; anything it
leaves is resolved as "ours". Schema conflicts (a dropped table modified on
the other side, two indexes on the same columns) cannot be resolved in place:
the backend aborts and the verb is recorded as FAILED with the reason.

Every verb returns an `OpResult` with `status` OK, UNSUPPORTED (the backend
has no such operation: zero latency, workload continues) or FAILED (the
backend tried and errored). Nothing raises unless `raise_on_error=True`;
`result.raise_for_status()` raises on demand. `DBToolSuite.capabilities()`
reports which verbs a backend supports, and `DBToolSuite.implementation()`
how each one is realised: `native` (one backend primitive), `composed`
(several native primitives driven by the backend class) or `simulated` (SQL
emulation of something the backend lacks). The macrobench e2e stats carry
both under `capabilities` and `implementation`.

| Backend | branch | commit/diff/log | merge | rebase/revert | reset | delete | commit refs | multi-branch exec | exec_async |
|---------|--------|-----------------|-------|---------------|-------|--------|-------------|-------------------|------------|
| `dolt`, `dolt_mysql` | yes | yes | yes | yes | yes | yes | yes | yes | yes |
| `neon` | yes | yes (LSN + log table) | yes (SQL three-way over postgres_fdw) | yes | yes (restore to LSN) | yes | yes | per-ref fallback | yes |
| `xata` | yes | | | | | yes | | | yes |
| `seekdb` | yes | yes (SCN snapshots) | yes (SQL three-way) | yes | yes | yes | yes | yes | yes |
| `matrixone` | yes | yes (snapshots) | yes (DATA BRANCH MERGE) | yes | yes (diff-driven) | yes | yes | yes | yes |
| `bauplan` | yes | yes (tags) | yes (native; row-level on conflict) | yes | yes (branch from tag) | yes | yes | | |
| `file_copy` | yes (CREATE DATABASE clone) | yes (clone per commit + log table) | yes (SQL three-way over postgres_fdw) | yes | yes (drop + clone from the snapshot) | yes | yes | yes (postgres_fdw) | yes |
| `chronos` | yes (interval fork, metadata only) | yes (checkpoint + log table) | yes (three-way over Chronos diffs) | yes | yes (diff-driven) | yes (deferred while children live) | yes (checkpoints) | yes (interval views) | |
| `terminusdb` (microbench only) | yes | commit: reads the head (every write commits); diff, log: yes | yes (`apply` of the diff since the common ancestor) | rebase: yes; revert: `apply` of the inverse diff | yes | yes | yes | | |

### exec()

```python
results = db.exec(script, refs=["feature"], mode="per_ref", label="eval")
```

runs `script` on each ref in turn (`mode="per_ref"`), or once with every ref
addressable from one session (`mode="multi"`, Dolt, SeekDB, MatrixOne, file_copy
over postgres_fdw and chronos over views on its interval tables; other
backends record it as UNSUPPORTED). A script is a list of SQL statements
(strings or `(sql, params)`), Python source, or a callable. Python source
runs with `db` (the session), `params` and `suite` in scope and may define
`run(db)`, whose return value becomes the result's `value`:

```python
script = """
def run(db):
    db.sql("UPDATE stock SET s_quantity = s_quantity - %s WHERE s_i_id = %s", (params["qty"], params["item"]))
    return db.sql("SELECT count(*) FROM stock WHERE s_quantity = 0")[0][0]
"""
```

Every statement issued through `db.sql()` autocommits and is recorded as its
own row (READ/INSERT/UPDATE/DELETE_ROWS/DDL) whose `num_keys_touched` is the rows affected (writes) or returned (reads), unless the script set it with `db.record_keys_touched(n)`. Per ref, `exec()` also records a CONNECT
row when it had to switch the connection to that branch, and one EXEC row
with the script's total latency and storage delta. `exec_async()` runs the
same thing on a connection pool (`open_async_pool(size)`) for throughput
measurement; its scripts must be a SQL list, a coroutine function, or source
defining `async def run(db)`.

### Result rows

Each parquet row carries `op_name`, `status`, `ref`, `label`, `exec_id`
(grouping the rows of one `exec()`), `latency`, `disk_size_before/after`,
`sql_query` (the statement, or the script text on EXEC rows),
`error_message`, `commit_ref_fallback` and the driver context (table, step,
thread). Run summaries include `workflow_supported`, the list of
`unsupported_ops`, per-op status counts and the backend's capabilities.

---

## Macrobenchmarks

The macrobenchmark runs one of six agent-workflow scenarios (BranchBench
S1-S6) against a backend through the git-like API, and records every branch
op and every statement, which verbs the backend could not perform, and
whether the sentinel-based invariance checks held.

| Scenario | Key | Shape | Branch ops |
|----------|-----|-------|------------|
| S1 Agentic RL environment | `rl_env` | T task branches, G rollout leaves each, forks from recorded steps | branch (from commit), commit, diff, delete |
| S2 Agent context management | `context_mgmt` | long spine, candidate branches per compaction | branch, commit, merge (ff), revert, rebase, delete |
| S3 Multi-agent collaboration | `multi_agent` | N agents merging into a spine that keeps changing | branch, commit, merge (with conflicts), delete |
| S4 Development agent | `dev_agent` | dev branches off a busy production spine, never merged; one spine migration adds a column the branches already added with another type (a schema conflict at their next rebase, which the branch resolves by moving its column and retrying; a backend that replays history merges the spine in instead) | branch, commit, rebase (with data and schema conflicts), merge (conflict recovery), delete |
| S5 Operations agent | `ops_agent` | dense commits, bad deploy, investigation branches, PITR | branch (from commit), commit, diff, reset, delete |
| S6 Data agent | `data_agent` | ingestion batches rebased and merged into a warehouse | branch, commit, reset, rebase, merge, delete |

Each scenario lives in `macrobench/scenarios/s<N>_<key>.py` with its
parametrized SQL and exec() scripts; `macrobench/faults.py` holds the fault
catalog and the TPC-C consistency conditions S1 and S5 use;
`macrobench/tpcc.py` holds the TPC-C transactions and CH queries that the
optional spine load runs.

### Configuration

A run is one `MacroBenchConfig` textproto (`macrobench/task.proto`):

```
run_id: "macro_data_agent_dolt"
backend: DOLT
database_setup { db_name: "macro_data_agent" cleanup: true generated {} }
schema { base: CH_BENCH scale_factor: 1 }                 # generated CH-benCHmark at W=1
workload {
  branch_ops { commit_interval: 2 }                        # commit every 2 steps
  data_ops { statements_per_step: 8 rows_per_write: 5     # exec() intensity
             write_fraction: 0.8 spine_clients: 4 analytical_fraction: 0.8 }
  data_agent { batches: 32 concurrent_batches: 4 batch_rows: 2000
               days_back: 3 steps_per_batch: 10 reset_prob: 0.1 }
}
```

- `schema` picks the base (CH-benCHmark or none), the scale factor W and
  row-density knobs, and the extension tables (`macrobench/schema/*.sql`;
  by default the scenario's own). With `database_setup.generated`, the
  runner creates an empty database and seeds it with the generators in
  `macrobench/datagen/`; `sql_dump` and `existing_db` still work.
- `workload.branch_ops` and `workload.data_ops` set the shared intensity
  (commit interval, retention, live-branch cap; statements per step, rows per
  write, read/write/DDL mix, worker threads, background spine clients).
- The scenario message holds the paper's structural parameters (T/G/S, N,
  fan-out, cycles, D, p, ...).
- `workload.branch_intensity` and `workload.data_intensity` are multipliers
  applied on top of the explicit knobs before the run (0 or 1 = unchanged).
  Branch intensity scales the number of branch verbs (branches, forks,
  rounds, rebases, investigation points) and divides `commit_interval`;
  data intensity scales the SQL per branch (statements per step, rows per
  write, spine clients, per-branch steps and rows). Thread counts, caps and
  probabilities are not scaled. `macrobench/intensity.py` lists the exact
  fields; the effective values are printed and written to the e2e stats.
- `invariants { disabled: true }` turns the sentinel checks off;
  `fail_fast: true` stops at the first failure.

`macrobench/configs/<key>.textproto` holds the paper-scale parameters and
`<key>_mini.textproto` a smoke-test size.

### Running

```bash
./scripts/run_macrobench.sh [--mini] [--outdir DIR] [--max-runtime-sec N] [--op-stall-sec N] \
    [--measure-storage] [--storage-sample-interval SEC] [--branch-intensity X] [--data-intensity Y] \
    <scenario> <backend> [scale_factor]
```

| Argument | Description |
|----------|-------------|
| `scenario` | `rl_env`, `context_mgmt`, `multi_agent`, `dev_agent`, `ops_agent`, `data_agent` |
| `backend` | `dolt`, `dolt_mysql`, `seekdb`, `matrixone`, `bauplan`, `neon`, `xata`, `file_copy`, `chronos` |
| `scale_factor` | W warehouses for the generated data (default: the config's) |
| `--max-runtime-sec N` | runtime cap; default one hour times the largest of the scale factor and the intensity multipliers (3600 s at scale 1, intensity 1), 0 = no cap. A run that reaches it (or gets Ctrl-C / SIGTERM) is recorded as `interrupted` with its rows and stats written |
| `--op-stall-sec N` | stall limit for one operation: a verb or an exec that has been running longer than this (default 600 s, 0 = off) is cancelled and recorded `FAILED` with a `stalled: cancelled after Ns` note; the run goes on, so later failures and invariant results are recorded too; the e2e stats list the cancelled operations under `stalls` with their outcome (`failed`, or `completed` when it finished before the cancel landed), and a run with a cancelled operation that failed ends with status `incomplete` instead of `completed` |
| `--storage-sample-interval SEC` | seconds between background storage samples (default 5, 0 = off) |
| `--measure-storage` | also measure storage around every operation |

```bash
./scripts/run_macrobench.sh --mini rl_env dolt
./scripts/run_macrobench.sh --outdir run_stats --max-runtime-sec 7200 multi_agent dolt
# twice the branch churn, half the SQL per branch
./scripts/run_macrobench.sh --mini --branch-intensity 2 --data-intensity 0.5 rl_env dolt
./scripts/run_macrobench.sh --measure-storage data_agent dolt 5
# or directly
uv run python -m macrobench.runner --config macrobench/configs/ops_agent_mini.textproto --outdir run_stats
```

A backend that lacks a verb does not run the scenario past it: the verb is
recorded as UNSUPPORTED, the run stops there with every worker cancelled,
`workflow_supported` is false and `status` is `unsupported: <VERB>: <reason>`
in the e2e stats. Failed operations, by contrast, never stop a run.

#### Storage

Every run records the backend's storage size (`DBToolSuite._storage_bytes()`:
the database's own directory on Dolt, the whole server data directory on
SeekDB and MatrixOne, which declare `STORAGE_SCOPE = "server"`) at four
workflow-level points, written to the e2e stats under `storage.points`:

| Point | When |
|---|---|
| `after_setup` | schema and seed data loaded, before the scenario |
| `after_workflow` | the scenario is done (its branches deleted) |
| `after_gc` | after the backend's GC/flush hook (`gc()`: `dolt_gc`, MatrixOne checkpoint; UNSUPPORTED on SeekDB) |
| `after_cleanup` | the run's database dropped (and, on Dolt, the dropped databases purged) |

A background sampler also records the size every 5 s during the scenario
(`storage.samples`, wall-clock seconds and bytes); `--storage-sample-interval
SEC` changes the interval (`0` turns it off), as does
`storage_sample_interval_sec` in the config. `--measure-storage` additionally
measures before and after every verb and every exec() call (two directory
walks per operation; off by default, and not attributable under concurrent
workers). The report's `storage.png` draws the sampled series with the four
points as markers; `summary.md` carries them as `storage_after_*_mb`.

### Output

```
run_stats/
├── <run_id>.parquet            # one row per branch op, statement, exec() and connect
└── <run_id>_e2e_stats.json     # status, support summary, invariants, scenario metrics
```

The e2e stats carry the workload parameters, the seed statistics,
`capabilities` and `implementation` (how the backend realises each verb:
native, composed or simulated), `workflow_supported` with `unsupported_ops`
and `op_status_counts`, the `invariants` results, the scenario's own
`metrics` (e.g. S3 merge order and conflicts, S5 time to recovery, S6
fast-forward vs three-way merges), the spine load's transaction counts and
the `storage` block (the four workflow-level points, the GC status, the
sampled series; see [Storage](#storage)).

---

## Microbenchmarks

Microbenchmarks measure latency and throughput for specific database operations under controlled conditions.

### Latency Benchmarks

Measure single-threaded operation latency with varying numbers of branches. For
multi-threaded load, use the throughput benchmarks.

#### Single-Threaded Latency

Use `scripts/run_single_thread_bench.sh` to measure single-threaded operation latency:

```bash
./scripts/run_single_thread_bench.sh <backend> <sql_dump_path> <num_branches> [OPTIONS]
```

##### Arguments

| Argument | Description |
|----------|-------------|
| `backend` | Database backend: `dolt`, `dolt_mysql`, `seekdb`, `matrixone`, `neon`, `xata`, `file_copy`, `chronos`, `terminusdb` |
| `sql_dump_path` | Path to SQL dump file (e.g., `db_setup/tpcc_schema.sql`) |
| `num_branches` | Number of branches to create for testing |

##### Options

| Option | Description |
|--------|-------------|
| `--seed <seed>` | Random seed for reproducibility |
| `--shape <shape>` | Branch tree shape: `spine`, `bushy`, or `fan_out` (default: `spine`) |
| `--operations <ops>` | Comma-separated list (e.g., `UPDATE,RANGE_UPDATE`) |
| `--range-size <n>` | Range size for RANGE_UPDATE operation (default: 200) |
| `--num-ops <n>` | Number of operations to perform |
| `--output-dir <dir>` | Output directory (default: `/tmp/run_stats`) |

##### Operations

| Group | `--operations` names |
|-------|----------------------|
| Data | `READ`, `INSERT`, `UPDATE`, `DELETE`, `RANGE_READ`, `RANGE_UPDATE` |
| Branch | `BRANCH_CREATE`, `BRANCH_CONNECT`, `BRANCH_DELETE`, `BRANCH_COMMIT`, `BRANCH_DIFF`, `BRANCH_LOG`, `BRANCH_MERGE`, `BRANCH_REBASE`, `BRANCH_REVERT`, `BRANCH_RESET` |
| Schema | the `DDL_*` operations |

The branch group covers the nine verbs of the shared API (`dblib/db_api.py`)
plus the connection switch. `BRANCH_COMMIT` to `BRANCH_RESET` each build the
state they need untimed and record only the verb:

| Operation | Untimed preparation | Timed |
|-----------|---------------------|-------|
| `BRANCH_COMMIT` | insert rows on the branch | `commit` |
| `BRANCH_DIFF` | once per branch: a child with committed inserts | `diff(branch, child)` |
| `BRANCH_LOG` | once per branch: `history_commits` commits | `log(branch, log_limit)` |
| `BRANCH_MERGE` | a child with committed inserts; the branch commits other rows | `merge(branch, child)` |
| `BRANCH_REBASE` | same as merge | `rebase(child, branch)` |
| `BRANCH_REVERT` | insert rows and commit them | `revert` of that commit |
| `BRANCH_RESET` | two commits of inserted rows | `reset` to the first |

`branch_op_config` in the task config sets `changes_per_op` (rows per
prepared side, default 1), `history_commits` and `log_limit` (default 10
each). Merges and rebases never conflict: the two sides insert different
keys. A verb the backend lacks is recorded as `UNSUPPORTED`.

##### Examples

```bash
# Run with 16 branches
./scripts/run_single_thread_bench.sh dolt db_setup/tpcc_schema.sql 16

# Run with custom seed and bushy branch shape
./scripts/run_single_thread_bench.sh neon db_setup/tpcc_schema.sql 32 --seed 12345 --shape bushy

# Run with custom range size
./scripts/run_single_thread_bench.sh dolt db_setup/tpcc_schema.sql 16 --operations RANGE_UPDATE --range-size 500
```

#### Output Files

Latency benchmark results are saved to the output directory:

```
<output_dir>/
├── single_thread/
│   ├── branch/
│   │   ├── <backend>_<dataset>_<N>_<shape>_branch.parquet
│   │   ├── <backend>_<dataset>_<N>_<shape>_branch_setup.parquet
│   │   └── <backend>_<dataset>_<N>_<shape>_branch_summary.json
│   └── connect/
│       └── (similar structure)
```

---

### Throughput Benchmarks

Measure throughput (operations per second) with independent control over threads, branches and concurrent requests.

#### Running Throughput Benchmarks

Use `scripts/run_throughput_bench.sh` with one of three sweep modes:

```bash
# Sweep concurrency (fix threads and branches, vary concurrent requests per thread)
./scripts/run_throughput_bench.sh <backend> <sql_dump_path> --sweep-concurrency --threads <N> --branches <N> [OPTIONS]

# Sweep branches (fix threads, vary branches)
./scripts/run_throughput_bench.sh <backend> <sql_dump_path> --sweep-branches --threads <N> [OPTIONS]

# Sweep proportionally (vary both threads and branches together)
./scripts/run_throughput_bench.sh <backend> <sql_dump_path> --sweep-proportional [OPTIONS]
```

The script runs in **async mode** by default: each thread keeps
`--concurrent-requests` ops in flight, each on its own pooled connection
checked out on the thread's branch. Async mode supports `dolt`, `dolt_mysql`,
`seekdb` and `neon`; use `--mode sync` for the other backends (one op at a time per
thread). Every point of a sweep uses the same mode, including concurrency 1.

Each thread works on one branch, assigned round-robin. With fewer threads
than branches, the extra branches exist but get no load; with more threads
than branches, threads share branches.

#### Arguments

| Argument | Description |
|----------|-------------|
| `backend` | Database backend: `dolt`, `dolt_mysql`, `seekdb`, `matrixone`, `neon`, `xata`, `file_copy`, `chronos` |
| `sql_dump_path` | Path to SQL dump file |
| `--sweep-concurrency` | Fix threads and branches, vary concurrent requests (requires `--threads` and `--branches`; async only) |
| `--sweep-branches` | Fix threads, vary branches (requires `--threads`) |
| `--sweep-proportional` | Vary both threads and branches proportionally |

#### Options

| Option | Description |
|--------|-------------|
| `--mode <async\|sync>` | Runner to use (default: `async`; `sync` for backends without async support) |
| `--threads <N>` | Fixed thread count (for `--sweep-concurrency` and `--sweep-branches`) |
| `--branches <N>` | Fixed branch count (for `--sweep-concurrency`) |
| `--threads-per-branch <N>` | Threads per branch ratio for `--sweep-proportional` (default: 4) |
| `--branch-list <list>` | Comma-separated branch counts (e.g., `1,2,4,8,16`) |
| `--concurrency-list <list>` | Comma-separated concurrency levels for `--sweep-concurrency` (default: `1,2,4,...,1024`) |
| `--concurrent-requests <n>` | Ops in flight per thread for the other sweeps (default: 1; > 1 needs async) |
| `--num-ops <n>` | Number of operations per thread (overrides the per-operation defaults) |
| `--point-ops <n>` / `--range-ops <n>` | Operations per thread for point / range operations |
| `--warmup-ops <n>` / `--warmup-fraction <f>` | Warm-up operations per thread, not counted in throughput |
| `--operations <ops>` | Comma-separated list (e.g., `READ,RANGE_READ`) |
| `--output-dir <dir>` | Output directory (default: `/tmp/run_stats`) |

Total timed operations per run are `num_ops x threads`; concurrent requests
overlap them but do not add to the count. Keep `threads x (concurrent requests + 1)`
below the server's connection limit.

#### Examples

```bash
# Fix 8 threads on 1 branch, vary concurrency: 1,2,4,8,16,32
./scripts/run_throughput_bench.sh dolt db_setup/ch-w1.sql --sweep-concurrency --threads 8 --branches 1 --concurrency-list "1,2,4,8,16,32"

# Fix 4 threads x 16 concurrent requests, vary branches (4 active, the rest idle)
./scripts/run_throughput_bench.sh dolt db_setup/ch-w1.sql --sweep-branches --threads 4 --concurrent-requests 16 --branch-list "4,16,64,256,1024"

# One thread per branch with 4 requests in flight each, 1-128 branches
./scripts/run_throughput_bench.sh neon db_setup/ch-w1.sql --sweep-proportional --threads-per-branch 1 --concurrent-requests 4

# Backend without async support
./scripts/run_throughput_bench.sh xata db_setup/ch-w1.sql --sweep-branches --threads 16 --mode sync

# Run only specific operations
./scripts/run_throughput_bench.sh dolt db_setup/ch-w1.sql --sweep-proportional --operations READ,RANGE_READ
```

#### Output Files

Throughput benchmark results are saved to the output directory:

```
<output_dir>/
├── <backend>_<dataset>_tp_t<threads>_b<branches>[_cr<concurrency>].parquet
├── <backend>_<dataset>_tp_t<threads>_b<branches>[_cr<concurrency>]_<operation>_threads<threads>_summary.json
└── <backend>_<dataset>_tp_t<threads>_b<branches>[_cr<concurrency>]_setup.parquet
```

Async runs include `_cr<concurrency>` in the name (also at concurrency 1); sync
runs do not. The summary JSON records `execution_mode` and `concurrent_requests`.

---

## Plotting Results

After running benchmarks, use the plotting scripts in the `scripts/plotting/` directory to generate visualizations.

### Macrobenchmark Report

`plot_macrobench.py` reads every `<run_id>_e2e_stats.json` / `<run_id>.parquet`
pair under one or more directories, groups the runs by scenario and backend,
and writes a summary plus comparison figures. Backends are read from the
stats files, so a directory holding Dolt and Neon runs compares them directly:

```bash
uv run python scripts/plotting/plot_macrobench.py --data-dir run_stats --outdir figures/macro

# several directories, filtered to two backends and two scenarios
uv run python scripts/plotting/plot_macrobench.py \
    --data-dir run_stats/dolt --data-dir run_stats/neon \
    --backends DOLT NEON --scenarios ops_agent data_agent \
    --outdir figures/macro
```

| Output | Content |
|---|---|
| `data/summary.md`, `data/summary.csv` | per run: status, elapsed/setup time, support, invariants, agent op counts and median latency per verb (spine load excluded), spine throughput and statement latency, scenario metrics |
| `time_breakdown.png` | summed latency split into branch ops (verbs plus the connection switch `exec()` does to reach a ref) and data ops (statements); spine load excluded |
| `latency_by_op_branch.png`, `latency_by_op_data.png`, `data/latency_by_op.md/.csv` | one panel per operation (branch verbs + CONNECT in one figure; statements inside `exec()` + EXEC in the other, with READ/UPDATE/DELETE split into point vs range by rows touched, READ:scan for aggregate/join reads, INSERT:bulk for multi-row inserts): median latency with a 95% CI, grouped by scenario (shaded bands), colour = backend; ops some backends lack are grouped last; the .md adds a backend x op support matrix |
| `latency_cross_branch.png`, `data/cross_branch.md/.csv` | one scenario-level read over several branches (rl_env's `score` over a task's 16 leaves, context_mgmt's `policy_check` over the candidates) as a single logical step: median wall-clock latency per backend, whisker to p90, annotated `multi` (one multi-ref `exec()`) or `per_ref x N` (the same read once per branch on a backend without multi-branch queries, N connection switches included); the table adds the summed exec latency and the per-branch cost |
| `latency_exec_by_label.png`, `data/exec_by_label.md/.csv` | `exec()` latency per script label (rollout_step, ingest, spine, ...) with statements per exec, and the mean exec time split into READ/INSERT/UPDATE/DELETE_ROWS/DDL/CONNECT/other |
| `progress.png` | completed agent steps (branch verbs and `exec()` runs, background load excluded) against elapsed seconds, one curve per backend |
| `exec_time_by_op.png` | per scenario, one horizontal 100% bar per script label and backend (adjacent rows) showing where `exec()` time goes: statement types, CONNECT, and `other` (untimed BEGIN/COMMIT/ROLLBACK round trips plus the Python between statements); mean ms and count at the bar end |
| `data/data_ops.md/.csv` | data statements (READ/INSERT/UPDATE/DELETE_ROWS/DDL) split by workload role: spine traffic vs. the agent's own statements (ingest, backfill, rollout_step, ...) |
| `cdf/latency_cdf_<scenario>.png` | latency CDF of each branch verb and data statement type (agent rows only), one line per backend |
| `storage.png` | storage over the run: the 5 s sampled series (or the per-operation sizes of runs made with `--measure-storage`) with the four workflow-level points as markers; log axis when database-scope and server-scope backends share a panel |

Only the newest run per (scenario, backend) is used unless `--all-runs` is
given; `--run-glob` narrows by run id (e.g. `'macro_*_mini_*'`).

### Microbenchmark Latency Plots

Plot microbenchmark latency results:

```bash
uv run python scripts/plotting/plot_branch_latency_micro.py \
    --data-dir <data_directory> \
    --outdir <output_figures_dir> \
    --operation <operation_type>
```

#### Arguments

| Argument | Description |
|----------|-------------|
| `--data-dir` | Directory with microbenchmark parquet files |
| `--branch-dir` | Directory with branch operation data (for combined plots) |
| `--connect-dir` | Directory with connect operation data (for combined plots) |
| `--outdir` | Directory to save figures |
| `--operation` | Operation type: `branch`, `connect`, `both`, or `combined` |

#### Examples

```bash
# Plot branch creation latency only
uv run python scripts/plotting/plot_branch_latency_micro.py \
    --data-dir run_stats_final/micro/single_thread/branch \
    --outdir figures/ \
    --operation branch

# Plot branch connection latency only
uv run python scripts/plotting/plot_branch_latency_micro.py \
    --data-dir run_stats_final/micro/single_thread/connect \
    --outdir figures/ \
    --operation connect

# Plot combined (branch and connect on single plot)
uv run python scripts/plotting/plot_branch_latency_micro.py \
    --branch-dir run_stats_final/micro/single_thread/branch \
    --connect-dir run_stats_final/micro/single_thread/connect \
    --outdir figures/ \
    --operation combined
```

### Microbenchmark Per-Operation Plot

One figure with every operation of the microbenchmark, one bar per backend:

```bash
uv run python scripts/plotting/plot_micro_ops.py \
    --data-dir <data_directory> [<data_directory> ...] \
    --outdir <output_figures_dir> [--branches 16]
```

It reads every result parquet under the directories (with the summary JSON
that names its backend) and writes `micro_ops_latency.png` and
`micro_ops_latency.csv`: median latency with a whisker to the 95th
percentile, on a log axis, for the six data operations and for the nine
verbs plus the connection switch. A verb a backend lacks is marked `n/a`.

### Microbenchmark Throughput Plots

Plot throughput vs threads/branches:

```bash
uv run python scripts/plotting/plot_throughput_experiments.py \
    --data-dir <data_directory> \
    --output <output_figures_path> 
```

---

## Output Files

Benchmark results are saved as Parquet files and JSON summaries.

### Macrobenchmark Output Structure

```
run_stats/
├── macro_rl_env_mini_dolt.parquet
├── macro_rl_env_mini_dolt_e2e_stats.json
├── macro_data_agent_dolt_w5.parquet
└── macro_data_agent_dolt_w5_e2e_stats.json
```

### Microbenchmark Output Structure

```
run_stats_final/micro/
├── single_thread/      # Single-threaded latency benchmarks
│   ├── branch/
│   └── connect/
├── multithread/        # Multi-threaded latency benchmarks
├── tp_fix_branch/      # Throughput: fixed branches, varying threads
├── tp_fix_thread/      # Throughput: fixed threads, varying branches
└── tp_proportional/    # Throughput: proportional threads and branches
```

### Parquet Schema

See [Result rows](#result-rows); the authoritative definition is
`dblib/result.proto`.

---

## Prerequisites

1. **[uv](https://docs.astral.sh/uv/)**: manages Python (3.13, pinned in
   `.python-version`) and the dependencies (locked in `uv.lock`)
2. **protoc** (`brew install protobuf`): the install step compiles the
   `.proto` files into the untracked `*_pb2.py` modules
3. **PostgreSQL-compatible backend**:
   - **Dolt**: Follow setup at https://github.com/dolthub/doltgresql
   - **Neon**: Configure via Neon console
4. **psql** client for database setup

Install with `uv sync` (add `--group dev` for pytest, then run
`uv run pytest tests/`). The project is installed in editable mode, and
`uv sync` / `uv run` recompile the protos whenever a `.proto` file changes.
Run Python through `uv run` (e.g. `uv run python scripts/...`), or activate
the environment with `source .venv/bin/activate`. The `scripts/run_*.sh` scripts
already use `uv run`. To add a dependency, use `uv add <package>` and commit
the updated `uv.lock`.

---

## Dolt (MySQL) backend — `dolt_mysql`

Runs against Dolt's MySQL-compatible server (port 3306). Start it with:

`dolt sql-server --host 127.0.0.1 --port 3306 --data-dir ~/dolt/databases`

and set a commit identity (`dolt config --global --add user.name` / `user.email`).

The loader reads Postgres `pg_dump` files (e.g. `ch-w1.sql`) directly and
converts them to MySQL, so no separate MySQL schema is needed.

Supports single-threaded, multi-threaded, and async (`concurrent_requests > 1`)
microbenchmark runs via `runner2.py`. In async mode each thread opens a pool
of `concurrent_requests` connections; `exec_async()` checks out the target
branch on a pooled connection the first time it is used there.

## SeekDB backend — `seekdb`

Runs against SeekDB (OceanBase's MySQL-compatible server, port 2881). Install
and start it with `brew tap oceanbase/seekdb && brew install seekdb &&
seekdb-start`, or run a source build's `bin/seekdb` from its base directory.

Connection settings come from `SEEKDB_HOST` (default `127.0.0.1`),
`SEEKDB_PORT` (`2881`), `SEEKDB_USER` (`root`) and `SEEKDB_PASSWORD` (empty).
Storage is measured on the server's data directory, `SEEKDB_DATA_DIR`
(default: `~/seekdb/store` if it exists, else the brew install's
`/opt/homebrew/var/seekdb/data`); it covers the whole server, since SeekDB
has no per-database directory. It uses the same `pg_dump` loader as
`dolt_mysql`. Every connection raises `ob_query_timeout` to one hour: the
default 10 s is too short for seeding and for the joins behind merge/rebase.

SeekDB branches by forking whole databases (copy-on-write, milliseconds), so
each branch is its own database: `main` is `<db_name>`, and branch `X` is
`<db_name>__X`. `branch()` runs `FORK DATABASE`, connecting runs `USE`, and
`delete()` runs `DROP DATABASE` (forked databases can take several seconds to
drop). A multi-branch script addresses another branch as
`` `<db>__<branch>`.`<table>` ``.

SeekDB has no commits, so the history verbs are built from two primitives it
does have: `current_scn()` with flashback reads (`<table> AS OF SNAPSHOT
<scn>`), and database forks.

- `commit()` records the current SCN in the branch database's `_bb_commits`
  table (its id is the SCN in hex) together with the table and column list at
  that point. Forks copy that table, so a branch inherits its parent's log;
  `log()` reads it.
- `diff()`, `reset()`, `revert()` and `branch(name, "b@commit")` read the
  commit's snapshot with flashback queries and apply the differences with
  anti-joins (`DELETE ... NOT IN`, `REPLACE INTO ... SELECT`). A fork's
  tables have no history from before the fork, so each log row names the
  database whose flashback holds its snapshot; a snapshot older than the
  server's `undo_retention` (default 30 min; raise it with
  `ALTER SYSTEM SET undo_retention = 86400`) can no longer be read. Flashback
  cannot roll back DDL, so a restore drops the tables and columns the commit's
  recorded schema does not list (indexes stay). An index built after row
  updates makes that table's earlier snapshots unreadable for good (error
  1412): a restore then keeps the table's current rows, a merge or rebase
  uses the fallback fork point or, failing that, a two-way merge for it,
  and the verb's result lists the table (`not_restored`,
  `fallback_base_tables`, `two_way_tables`) with a warning.
- `merge()` and `rebase()` are a SQL three-way merge: base = the fork point
  of whichever side descends from the other (the fork's own content right
  after the fork; the parent at the SCN just before the fork is kept as a
  fallback for tables whose history later DDL made unreadable), ours = the
  target database, theirs = the other branch. Rows are matched by primary key; a row changed differently on
  both sides is a conflict, resolved per `on_conflict` (a callable gets rows
  in Dolt's `base_*` / `our_*` / `their_*` shape). Tables and columns added
  on one side are added to the other; a differing primary key is a schema
  conflict and the verb fails without touching data. Tables without a
  primary key (`history`) only receive the other side's new rows. `rebase()`
  reads the upstream at one SCN, which becomes the branch's new fork point,
  and the merge after it is then a fast-forward.
- `SEEKDB_NATIVE_MERGE=1` makes `merge()` use SeekDB's own `MERGE TABLE ...
  STRATEGY OURS|THEIRS` per table instead. It has no common ancestor (every
  differing row is a conflict; the source's deletes are never applied) but
  measures the native primitive; conflicts are counted with `STRATEGY FAIL`.

A commit ref used with `exec()` is materialised as a temporary fork
(`<db>__tmp_*`) restored to that snapshot and dropped when the connection
closes.

Async mode (`use_async` / `concurrent_requests > 1`) uses an aiomysql pool,
like `dolt_mysql`. Each pool connection is opened on the worker's branch
database.

---

## MatrixOne backend — `matrixone`

Runs against MatrixOne (MySQL-compatible, port 6001). Build it from source
(`make build` in a checkout of https://github.com/matrixorigin/matrixone, Go
1.26+ and cmake needed; v4.2.5 was used here) and start it with
`./mo-service -launch ./etc/launch/launch.toml`, or run the
`matrixorigin/matrixone` image. Connection settings come from `MO_HOST`
(default `127.0.0.1`), `MO_PORT` (`6001`), `MO_USER` (`root`) and
`MO_PASSWORD` (`111`, the standalone default). Storage is measured on the
server's data directory, `MO_DATA_DIR` (default `~/mo/matrixone/mo-data`),
which covers the whole server.

The native-summary and database-level commands measured below are extensions
on [zuyu/matrixone, branchbench-opt](https://github.com/zuyu/matrixone/tree/branchbench-opt),
including commits `8fae23f97`, `c870e3748`, `314f7fb5a`, and `9b5b22463`.
Build that branch (or the stated commit) to reproduce those measurements;
a stock MatrixOne build can select the legacy interface. Check the profiler's
server identity and observed paths before comparing results.

MatrixOne ships "git for data" primitives: database and table branches with
recorded lineage (`DATA BRANCH CREATE DATABASE ... FROM ... {snapshot}`), a
three-way `DATA BRANCH DIFF` / `DATA BRANCH MERGE` that finds the lowest
common ancestor itself, `DATA BRANCH PICK ... BETWEEN SNAPSHOT`, named
snapshots with time-travel reads (`t{snapshot = 'x'}`, readable even after
the database is dropped) and `RESTORE DATABASE ... {snapshot}`. As on
SeekDB, each branch is its own database (`main` is `<db_name>`, branch `X`
is `<db_name>__X`), and a multi-branch script addresses another branch as
`` `<db>__<branch>`.`<table>` `` or a commit as `` `<db>`.`<table>`{snapshot = '...'} ``.

A commit is a database snapshot; its message and order live in the branch
database's `_bb_commits` table, which branches inherit. Per verb
(`MatrixOneToolSuite.IMPLEMENTATION` / `IMPLEMENTATION_NOTES` carry the
same summary for the report):

- `branch()` (native): `CREATE SNAPSHOT` on the parent (the fork point)
  and `DATA BRANCH CREATE DATABASE ... FROM ... {snapshot}`; the new branch
  is snapshotted too so its own delta can be read later.
- `commit()` (composed): `CREATE SNAPSHOT FOR DATABASE` plus the log row.
  `log()` (simulated) reads the table.
- `diff()` (native): `DATA BRANCH DIFF DATABASE ... OUTPUT SUMMARY`, with
  one result row per table. Older servers use table-level summaries.
- `merge()` (composed): `DATA BRANCH MERGE DATABASE <src> INTO <dst>
  TABLES (...) WHEN CONFLICT SKIP|ACCEPT OUTPUT SUMMARY` ("ours" | "theirs")
  on servers supporting this extension. Older servers use the table-level
  `MERGE ... OUTPUT SUMMARY` interface. Each table's result contains
  `source_inserted`, `source_deleted`, `source_updated`,
  `destination_inserted`, `destination_deleted`, `destination_updated`,
  and `conflicts`. These are logical differences **before** the merge,
  independent of the conflict policy; equal changes are excluded.
  Older servers and custom resolvers use a separate summary and both sides'
  native diffs against the fork snapshot. A resolver receives Dolt's
  `base_*`/`our_*`/`their_*` row shape.
  A callable runs over the SKIP result. Tables and columns the source has
  and the target lacks are added first (`DATA BRANCH CREATE TABLE ... FROM`,
  `ALTER TABLE ADD COLUMN`); a differing primary key is a schema conflict
  and the verb fails without touching data. Set `MO_COUNT_CONFLICTS=0` to
  omit conflict reporting when `on_conflict` is "ours"/"theirs" (this also
  avoids fetching conflict rows on the legacy path). `MO_MERGE_SUMMARY=0`
  selects the legacy path for comparison. Automatic fallback occurs once
  per connection, only after a parser rejection at `OUTPUT`; execution and
  transport errors are never retried as a legacy merge.
  Parser-triggered fallback warns once per rejected interface and connection.
  The merge result includes `merge_summary`: `native` (database or table
  summaries), `legacy`, `mixed`, or `none` (no eligible data tables).
  `MO_DATABASE_BRANCH=0` disables database-level DIFF/MERGE for comparisons.
  A parser rejection at `DATABASE` selects the older interface once per
  operation and connection. Database summaries include table names and
  endpoint-existence flags, and DIFF enumerates tables at the requested
  snapshot, including tables dropped since then.
  The optimized server collects statistics before update expansion and
  conflict filtering in the same `HashDiff` execution used for apply.
  It reuses retained destination batches to exclude equal changes and
  releases matched primary keys as both sides arrive. Worst-case summary
  memory remains proportional to changed keys. Apply stages are created
  only when a nonempty batch needs applying, so a no-op does not create
  temporary tables. The database-level MERGE uses one transaction across
  the selected data tables; schema reconciliation and adapter commit-log
  updates are separate. The table-level fallback has per-table atomicity.
  Conflict counts have one known comparison caveat: when a primary-key table
  was created independently on both related branches after their fork,
  native summaries count differing rows with the same key as conflicts.
  The legacy path reports zero because that table is absent from the fork
  snapshot. Thus `MO_MERGE_SUMMARY=0` is not a count-equivalent control for
  this case. Both paths skip conflict counting for keyless tables.
- `rebase()` (composed): a re-fork. A temporary clone of the upstream's
  head snapshot receives the branch's delta since its fork point through
  `DATA BRANCH PICK ... BETWEEN SNAPSHOT` (the branch's own snapshots;
  key-less tables are replayed row by row), conflicts with the upstream's
  delta over the same period are resolved per `on_conflict` ("ours" = the
  upstream, "theirs" = the branch, as in git), and the clone replaces the
  branch. Its lineage now starts at the upstream head, so MatrixOne's LCA
  for a later merge is that head and the merge sees only newer changes.
- `reset()` (native data operation with adapter bookkeeping):
  `DATA BRANCH RESET DATABASE <branch> FROM <db>{snapshot = 'commit'}`
  restores data in place, retaining table identities and branch lineage.
  It also accepts a commit inherited from the parent. Primary-key tables
  apply the differences; keyless tables replace their contents, including
  duplicate rows. Normal mode reconciles schema separately. Older servers
  use the existing diff-driven SQL restore; schema differences also take
  that path until native RESET support has been established. Native-only
  mode (`DBFORK_NATIVE_ONLY=1`) skips schema reconciliation and falls back
  to `RESTORE DATABASE` on older servers for same-database snapshots; that
  older command replaces table identities and affects later lineage diffs.
- `revert()` (native data operation with adapter bookkeeping):
  `DATA BRANCH REVERT DATABASE <branch> FROM <db>{snapshot = 'after'}
  AGAINST <db>{snapshot = 'before'}` applies the inverse of the commit.
  It preserves later changes to other keys; changes to the same key are
  overwritten by the before-image. Normal mode uses this path when all
  three schemas match and retains the existing row replay for older servers
  or schema-changing commits. As before, the adapter skips keyless tables
  during revert; the raw server command rejects them. Set
  `MO_DATABASE_RESTORE=0` to compare the legacy reset/revert paths. Only an
  initial parser rejection permits fallback; runtime errors are propagated
  without replaying the operation.
- `delete()` (native): `DATA BRANCH DELETE DATABASE` (`DROP DATABASE` after
  a restore, which leaves the database without branch metadata).

To measure client SQL counts, returned rows, response bytes, and latency:

```sh
MO_PORT=6001 python -m scripts.profile_matrixone_operations \
  --tables 1 10 --rows 1000 --changed 100 --reps 3 --out /tmp/mo-profile.json
```

The script creates and cleans up uniquely named scratch databases. It counts
every client `COM_QUERY`, including direct cursor calls; setup, seeding,
cleanup, and handshakes are excluded. `connect` means switching the adapter
to an existing branch. `create` means branching from the current head.
The workload changes 100 source rows and 50 target rows per table, inserts
one source row, then merges with `ours`, reverts, and resets. Row counts and
sums are checked after merge, revert, and reset. There are no schema migrations.
Historical refs, chunked row replay, and different connection state can
change the counts.

Operation results and Parquet rows include `execution_paths` and
`fallback_reasons` for MatrixOne diff/merge/reset/revert. Merge paths are
`database_merge_summary`, `table_merge_summary`, and `legacy_table_merge`;
`custom_resolver` records an invoked resolver. An empty table selection is
`no_data_tables`. Paths are unique per operation, not per-table counters.
Failed operations retain the path that errored; syntax-rejected probes are
listed only as fallback reasons. An empty list means no instrumented path
was reached, not that the native path ran. Cached parser rejections retain
their reason on subsequent operations; explicitly disabled paths are labeled
`disabled`.

Run JSON includes `execution_path_counts` by operation/path/status and
`fallback_reason_counts`; microbench verb repetitions also include the raw
lists and `merge_summary` when returned. These counts cover recorded operations,
excluding untimed setup and warm-up. The profiler records the same lists, `merge_summary`, `SELECT
version(), git_version()` as `server`, the adapter commit, and whether tracked
adapter files were dirty. Server identity queries are outside measurements.
The recorded versions are the server's own values; a binary built without
revision metadata may return `unknown`, which is preserved as such.
Static `implementation.notes` describe available strategies, not the path a
particular server executed.

The `_bb_commit_id` index was added to speed commit-id lookups, but its net
effect has **not** been isolated in these measurements. The extra hidden
index table cloned with each branch and index maintenance on commit inserts
are costs included in the totals. These results do not establish an index
benefit; that needs a separate indexed/unindexed comparison across log sizes,
including lookup, branch creation, commit latency, and storage.

Measured on 2026-10-25 against local MO commit `8fae23f97`, comparing original
adapter `1ca34a3` with optimized adapter `c037fed` using native summaries:

| Operation | Original: 1 table | Optimized: 1 table | Original: 10 tables | Optimized: 10 tables |
| --- | ---: | ---: | ---: | ---: |
| connect | 2 | 2 | 2 | 2 |
| log | 2 | 2 | 2 | 2 |
| delete | 3 | 3 | 3 | 3 |
| commit | 5 | 5 | 5 | 5 |
| create | 7 | 7 | 7 | 7 |
| diff | 13 | 13 | 40 | 40 |
| revert | 17 | 17 | 89 | 89 |
| reset | 14 | 14 | 77 | 77 |
| merge | 30 | 15 | 156 | 24 |

For 10-table merge, median latency over three local runs changed from
2874 ms to 1488 ms; returned rows from 1716 to 76; response bytes from
77,359 to 11,557. On the same optimized adapter, disabling native summaries
used 64 statements and had a 1927 ms median. These are small local diagnostic
runs, not throughput or tail-latency measurements.

For internal work, add `--server-log /path/to/mo.json.log` with an exclusive
test server configured for INFO JSON logging. The report counts appended
`DataBranch-*` events, including `DataBranch-HashDiff-Done`; these measure
internal diff passes, not total engine SQL statements. Log rotation and
other clients make this attribution invalid.

With the local merge timing change (`026daa338` on
[`branchbench-opt`](https://github.com/zuyu/matrixone/tree/branchbench-opt)), the
same `--server-log` option adds `server_merge_timing` to each merge measurement:
`total_ms`, `phases_ms`, and raw `requests`/`tables` correlated by `request_id`.
The server emits `DataBranch-Merge-Request-Timing` and
`DataBranch-Merge-Table-Timing` at INFO level, with numeric milliseconds and
`schema_version: 1`. Both plain MERGE and MERGE OUTPUT SUMMARY are covered,
including database-level MERGE. No SQL result columns change.

| Phase | Work included |
| --- | --- |
| `executor_acquire` | Acquire the mutation executor/transaction and lineage-owner lifecycle admission |
| `enumerate` | Database table enumeration and selection |
| `authorize` | Database-wide table authorization before writes |
| `relations` | Resolve table endpoints and relation metadata |
| `prepare_worker` | Prepare the table worker |
| `lineage` | Resolve branch ancestry and the common ancestor |
| `reconcile_schema` | Reconcile endpoint schemas in the engine |
| `diff` | Traverse changes and calculate summaries, excluding synchronous apply callbacks |
| `stage_init` | Create staging tables, including their initial DROP IF EXISTS |
| `apply` | Encode batches, load stages, execute target DML, clear stage rows, and flush remaining buffers |
| `stage_cleanup` | Drop staging tables after apply |
| `summary_output` | Prepare/save the summary result |
| `txn_finish` | Finish the owned transaction or explicit transaction's statement |
| `other` | Remaining handler work, resource cleanup, and timing/logging overhead |

These are exclusive **wall times**, not CPU times; internal SQL retries and
waits stay in the phase that performs them. Request phases include all child
table phases. The profiler aggregates **only request events**, so ten table
requests and one database request with ten table events are both counted
correctly. Phase values sum to the handler's `total_ms`; SQL parsing, outer
protocol work, client network time, and adapter catalog/schema/log queries
remain outside that total and are visible in the existing client query trace.

Request `status` is `ok`, `error`, or `panic`, recorded after transaction finish;
a table can succeed before a later table or transaction commit fails. The
`finish_action` records the attempted `commit`/`rollback`, `statement` for an
explicit transaction, or `not_acquired` if executor acquisition did not finish.
For an explicit transaction, a later user COMMIT/ROLLBACK is excluded. A no-op
summary merge reports zero stage/apply times. The adapter's legacy path still
performs extra DIFF queries outside the server MERGE timings.

Older servers produce `available: false`, `complete: false`, and null totals.
Missing request/table events, duplicate IDs, unsupported timing versions, or
inconsistent phase sums also produce `complete: false`, with `issues` and raw
records retained; partial timings are never presented as a complete total.
Without `--server-log`, no server timing field is added. This instrumentation
does not itself claim a merge performance improvement.

MO commit `500f53066` prepares each MERGE staging table at the first nonempty
flush that consumes it, reuses it across batches, and cleans up only attempted
stages. This covers plain MERGE, table summaries, and database MERGE. PICK and
portable SQL output retain their previous staging behavior. Direct row updates
and keyless inserts do not create an unused stage; an empty merge creates none.
Stage preparation inside apply is charged only to `stage_init`, preserving the
exclusive timing accounting above.

For ordinary primary-key tables, the server's staging DDL count changes as
follows. Each stage costs an initial DROP IF EXISTS, a CREATE TABLE AS SELECT,
and a final DROP; target DML and stage-row clearing are additional statements.

| Writes actually applied | Before (`026daa338`) | On demand (`500f53066`) |
| --- | ---: | ---: |
| INSERT only | 9 | 3 |
| DELETE only | 9 | 3 |
| Batched UPDATE only | 9 | 3 |
| INSERT and batched UPDATE | 9 | 6 |
| INSERT, DELETE, and batched UPDATE | 9 | 9 |

A local comparison on 2026-10-25 used the same clean adapter (`b3adbf3`),
1,000 rows/table, 100 source updates plus one insert/table, and 50 conflicting
destination updates/table with the `ours` policy. Each server ran three
repetitions per table count and interface, alternating interface order. The
baseline server ran first, followed by the optimized server. Medians in ms:

| Interface / tables | Client merge before → after | Server merge before → after | Stage init + cleanup before → after |
| --- | ---: | ---: | ---: |
| Table summary / 1 | 288 → 264 | 147 → 125 | 56 → 37 |
| Database / 1 | 275 → 280 | 117 → 121 | 41 → 40 |
| Table summary / 10 | 1429 → 1404 | 1283 → 1239 | 540 → 399 |
| Database / 10 | 1397 → 1219 | 1264 → 1080 | 567 → 368 |

The 10-table database merge reduced client latency by about 13% and combined
staging time by 35% in this sample. The table-summary total improved only 2%,
and the one-table database total increased 2%; these small runs do not establish
a speedup for every workload. Each staging total is computed per run before
taking its median. Client SQL counts are unchanged: 15 for one table, and
24/15 for ten tables using the table/database interfaces. All runs checked
merge conflict counts and data after merge, revert, and reset.

To reproduce, run the following against each server build separately, using
its own exclusive INFO JSON log and a different output filename:

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_operations \
  --tables 1 10 --rows 1000 --changed 100 --reps 3 \
  --modes summary database --server-log /tmp/mo.json.log \
  --out /tmp/mo-stage-profile.json
```

In a separate traced run of the same 10-table workload, legacy merge emitted
40 `HashDiff-Done` and 40 `GetRelations-Done` events; the initial native summary
implementation (`8fae23f97`) emitted 20 and 10 respectively. It shared relation
resolution but still did two diff passes per table.

MO commit `c870e3748` collects the summary within the merge's existing diff
traversal. A fresh comparison on 2026-10-25 kept adapter `7891e91`, the workload,
and INFO logging configuration fixed, with three repetitions per server:

| Merge metric | Two passes: 1 table | One pass: 1 table | Two passes: 10 tables | One pass: 10 tables |
| --- | ---: | ---: | ---: | ---: |
| Median latency (ms) | 296 | 259 | 1661 | 1404 |
| Client SQL statements | 15 | 15 | 24 | 24 |
| Returned rows | 13 | 13 | 76 | 76 |
| Response bytes | 5989 | 5989 | 11557 | 11557 |
| `HashDiff-Done` | 2 | 1 | 20 | 10 |
| `Hashmap-Build-Done` | 4 | 2 | 40 | 20 |
| `SnapshotScan-Done` | 8 | 4 | 80 | 40 |
| `GetRelations-Done` | 1 | 1 | 10 | 10 |

Use `--modes summary --server-log /path/to/mo.json.log` with the profiler above
to compare these server versions. This optimization reduces server work;
the SQL interface and client round trips are unchanged. The local median
improved by about 13% for one table and 15% for ten tables.

A separate 20,000-row check updated 12,000 source rows and made the same
updates to 6,000 destination rows. Both versions reported exactly 6,000
source updates and zero conflicts. One measured merge changed from 1291 ms
to 693 ms. Three subsequent no-op merges returned all-zero summaries and
kept the row count and sum unchanged; their median changed from 175 ms to
186 ms, so this run shows no no-op latency improvement. Both versions already
need only one diff pass for a no-op. These small samples do not establish
throughput or tail latency.

MO commit `314f7fb5a` adds database-level DIFF/MERGE, and `9b5b22463` adds
server-side RESET/REVERT. A final comparison on 2026-10-25 used that server
for both modes, with the restore-enabled adapter, INFO logging, and three
repetitions per mode (`--modes summary database`). The order alternated
between repetitions. `summary` uses table-level summaries and client row
restore; `database` uses the new database commands. Client statement counts
were identical across repetitions:

| Operation | Table interface: 1 table | Database interface: 1 table | Table interface: 10 tables | Database interface: 10 tables |
| --- | ---: | ---: | ---: | ---: |
| diff | 13 | 9 | 40 | 9 |
| merge | 15 | 15 | 24 | 15 |
| revert | 17 | 11 | 89 | 11 |
| reset | 14 | 7 | 77 | 7 |

| Operation | Table/client path median (ms): 1 table | Database path median (ms): 1 table | Table/client path median (ms): 10 tables | Database path median (ms): 10 tables |
| --- | ---: | ---: | ---: | ---: |
| diff | 54 | 35 | 195 | 154 |
| merge | 285 | 274 | 1446 | 1519 |
| revert | 149 | 141 | 940 | 525 |
| reset | 106 | 96 | 836 | 530 |

For ten tables, revert returned 44 rows / 5,127 response bytes instead of
593 / 29,866; reset returned 22 / 2,317 instead of 572 / 28,052. The remaining
rows are adapter metadata: native RESET/REVERT return only status. This small
local sample shows fewer round trips and faster restore, but no ten-table
merge latency improvement. It is not a throughput or tail-latency result.

These counts include adapter metadata and commit bookkeeping. The native
data operation itself takes one request regardless of table count. Server
merge work remains one HashDiff per table. MERGE checks permissions for all
selected tables before applying data and rolls back the entire data statement
if a later table fails. Schema changes are still reconciled by the adapter.
RESET/REVERT likewise preflight the selected tables, stage all changes, and
apply them in one transaction. Schema reconciliation and adapter commit-log
updates remain outside that data transaction. The server currently computes
restore differences using joins over snapshot endpoints, so reducing client
SQL does not imply constant server work or an incremental storage-level
restore. MO commit `31d7c8437` fixes snapshot read-view reuse so a query joining
two historical versions of the same table reads the correct version on each
side; the restore commands depend on this fix.

MatrixOne's diff is reliable for two snapshots of one table, for a clone
(or chain of clones) against its ancestor's head, and for a clone against
the exact snapshot it was cloned from; it drops the ancestor's updates and
deletes when a clone is compared with an *older* snapshot of the ancestor,
which is why `rebase()` replays the branch's delta with `PICK` instead of
merging the branch into the clone. `DATA BRANCH DIFF ... OUTPUT AS` fails
on a table altered since the base (v4.2.5), so diff rows are fetched to the
client. A native-merge conflict between an update and a delete keeps the
update (the row is resurrected). Snapshot names are silently truncated to
64 characters; the backend keeps its own below that. `CREATE INDEX IF NOT
EXISTS` is not accepted (S2 falls back to `CREATE INDEX`).

A commit ref used with `exec()` is materialised as a temporary branch
(`<db>__tmp_*`) created from the snapshot and dropped when the connection
closes. `drop_database()` drops every branch database and every snapshot of
the run (`bb_<db>_*`). Async mode uses an aiomysql pool, like `dolt_mysql`.

---

## Bauplan backend — `bauplan`

Runs against a [Bauplan](https://www.bauplanlabs.com/) lakehouse (Iceberg
tables over S3, with git-like branches, commits, tags and merges in its
catalog). Macrobench only for now: no SQL dump loading (so no microbench) and
no `exec_async`.

### Setup

1. A Bauplan API key (Settings → API Keys), either as `BAUPLAN_API_KEY` in
   `.env` or in a `~/.bauplan/config.yaml` profile (the default profile is
   used; pick another with `BAUPLAN_PROFILE`). The branch prefix is the key's
   user (`client.info()`), or `BAUPLAN_USERNAME`.
2. An S3 bucket the Bauplan deployment can read. The public sandbox only
   imports from a **public, listable** bucket, so use a dedicated bucket:
   everything the benchmark writes there is world-readable.
   ```json
   {"Statement": [{"Effect": "Allow", "Principal": "*",
     "Action": ["s3:GetObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::<bucket>", "arn:aws:s3:::<bucket>/*"]}]}
   ```
3. AWS credentials allowed to write and delete under that prefix, and the
   location, in `.env` (see [Environment Variables](#environment-variables)).
   The bucket's region is looked up, so `AWS_REGION` need not match it.

`uv run pytest tests/test_bauplan_live.py` checks the setup (slow: tens of
minutes; skipped when `BAUPLAN_S3_URI` is unset). The local parts are
covered offline by `tests/test_bauplan_workspace.py`.

### How it works

A non-admin user can only write branches named `<user>.<name>`, so a run's
root branch (the benchmark's `main`) is `<user>.<db_name>`, created from
`BAUPLAN_SOURCE_REF` (default `main`), branch `x` is `<user>.<db_name>__x`,
and the run's tables live in their own namespace, `bb_<db_name>`.

Bauplan has no SQL writes (`query()` is read-only), so `exec()` works on a
local copy: reads go to `query()` on the branch's head as of the script's
first statement; a statement that writes runs on local DuckDB copies of the
tables it touches (pulled with `scan()`, primary key enforced), and reads of
those tables then run locally too. When the script ends, each changed table
is written to S3 as Parquet and loaded with `create_table(replace=True)` +
`import_data` on a temporary branch, which is merged into the branch: one
atomic commit per `exec()`. If another writer changed one of those tables
meanwhile, the merge conflicts and the script runs again from the new head
(up to `BAUPLAN_PUBLISH_RETRIES` times), so no update is lost. Writes are
therefore visible to other sessions when the script ends, not per
statement; per-statement rows time only the local work, and the EXEC row
includes the publish.

Iceberg keeps neither primary keys nor declared types the way merge needs
them, so each table version (a replaced table gets a new id) has a schema
sidecar in S3, `<BAUPLAN_S3_URI>/<db>/_schema/<id>.json`.

- `branch()` (native): `create_branch`, from a branch or a commit's tag.
- `commit()` (composed): every Bauplan write is already a commit, so a
  commit tags the branch head: `<user>.<db>.c<id>[.m<base32 message>]`.
- `log()` (composed): `get_commits`, keeping the commits the run's tags point
  at, plus the commits a merge carried over (listed in its `bb_commits`
  commit property).
- `diff()` (simulated): tables whose ids differ are row-diffed locally.
- `merge()` (native): `merge_branch`. Bauplan detects conflicts per table;
  on a `MergeConflictError` the conflicting tables are merged row by row
  locally (three-way, base = the newest shared commit), tables only the
  source changed are copied with `revert_table`, and the result is merged in.
  `on_conflict` ("ours", "theirs" or a callable getting Dolt-shaped rows)
  applies to rows both sides changed.
- `rebase()` (composed): a branch from the upstream gets the branch's
  changes as above (`"ours"` = the upstream, as in Dolt), then replaces the
  branch (`delete_branch` + `rename_branch`).
- `reset()` (composed): a branch from the commit's tag replaces the branch.
- `revert()` (simulated): the commit's row changes, inverted locally and
  published.
- `delete()` (native): `delete_branch`.

A deleted or renamed branch's `<name>@<hash>` refs stop resolving, so commit
refs resolve through tags. Commit refs are read-only in `exec()`. Multi-branch
scripts are not supported (`query()` reads one ref). `drop_database()`
deletes the run's branches, tags and S3 files.

### Behaviour to expect

- Each `exec()` that writes costs a temporary branch, an S3 upload plus
  `create_table` and `import_data` jobs per changed table (about 10 s each;
  tables are published in parallel), and a merge. Reads through `query()`
  take about 2 s. Start with `--mini`.
- Every write rewrites the whole table.
- Conflicts are detected per table, so concurrent writers to one table
  (the TPC-C spine load) often re-run their scripts.
- FOREIGN KEY and CHECK constraints are accepted and ignored; indexes are
  not supported.
- Bauplan stores tables only, so a view's definition is kept in the
  branch's `_bb_views` table (published, branched and merged like a table)
  and the view is recreated locally over the tables it reads; a statement
  that reads a view therefore runs locally.
- Per-operation storage is not measured (`--measure-storage` is ignored).

---

## Neon backend — `neon` (Lakebase Postgres on Neon)

Runs against Neon's API (`console.neon.tech/api/v2`) with `NEON_API_KEY_ORG`
from `.env`. Each run creates a project of its own (`project_<db_name>`,
region `NEON_REGION`, default `aws-us-east-1`, Postgres `NEON_PG_VERSION`
17, a 2-day history window `NEON_HISTORY_RETENTION_SEC`) and deletes it
afterwards; stale benchmark projects left by interrupted runs are deleted
before a run starts. Every compute is a fixed `NEON_COMPUTE_CU` (2 CU) and never
auto-suspends (`NEON_BRANCH_SUSPEND_TIMEOUT_SEC=-1`): Neon's scale-to-zero
counts only running statements as activity, so with the plan default (5
min on Launch) a compute that a worker holds but has not queried for 5
minutes, or one a merge reads over `postgres_fdw` while busy elsewhere, is
suspended under its connections ("SSL connection has been closed
unexpectedly"). Computes are suspended explicitly instead, by the
active-compute budget below and on delete.

Branches are Neon branches with one read-write compute each. Neon versions
storage but has no commits, diff or merge, so the adapter builds them from
point-in-time branching (`parent_lsn`), branch restore and `postgres_fdw`:

- `branch()` (native): `POST /branches` with `parent_id`, `parent_lsn` for a
  commit ref, and a compute; the verb waits for the operations to finish
  and writes a fork row on the new branch (its own LSN and the parent point
  it was cloned from, the base of later merges).
- `commit()` (composed): a row in the branch's `_bb_commits` table plus
  `pg_current_wal_flush_lsn()` as the commit's point in time; `log()` reads
  the table. A commit ref is read through a temporary branch at that LSN,
  cached between uses, deleted when the connection closes.
- `diff()`: per table a row hash on each compute; the differing tables are
  pulled over `postgres_fdw` into temp tables and compared locally.
- `merge()`: SQL three-way merge on the target's compute. The base is a
  temporary branch at the source's fork LSN (its creation is inside the
  verb's latency), theirs is the source's compute, both pulled over
  `postgres_fdw`; rows are matched by primary key, conflicts resolved per
  `on_conflict` with the same callable row shape as the other backends;
  deletes go in reverse foreign-key order and upserts in forward order
  (the role is not a superuser, so constraints stay on).
- `rebase()`: the same three-way merge of the upstream into the branch,
  then a rebase row moves the branch's base to the upstream's LSN so the
  merge back only sees newer changes.
- `revert()`: temporary branches at the commit's LSN and its predecessor,
  inverse delta applied locally.
- `reset()` (native): `POST /branches/{id}/restore` to the commit's LSN.
  Neon requires `preserve_under_name` and keeps the pre-restore state in a
  backup branch (`<branch>_bk_*`) that cannot be deleted before the project.
- `delete()` (native): `DELETE /branches/{id}`; a branch that still has
  children is deferred and retried after later deletes and at close.
- Multi-branch queries (`exec(mode="multi")`) are not provided: Neon has no
  multi-branch query semantics, and the scenarios' cross-branch reads run
  once per branch instead (recorded as the `multi_ref_exec: unsupported`
  implementation entry).

Quotas are capacity, not errors. Neon limits *active computes* per
project (20 on the Launch plan, the default branch exempt), not branches,
so the process keeps at most `NEON_ACTIVE_BUDGET` (18) computes active:
when a compute is needed above that, the least recently used one that no
connection holds is suspended first (`NEON_SUSPEND_ON_SWITCH=1` instead
suspends a branch's compute whenever the connection moves off it); a
suspended compute resumes on the next connect, and both land in `CONNECT`.
A connect refused for the compute limit rebuilds the registry from
Neon's own list of active computes, suspends the least recently used
ones no connection holds, and retries (`active_compute_limit_hits`,
`limit_suspends` and `active_compute_unregistered` in the e2e stats'
`backend_observations` count how often that happened). All API calls share one process-wide token bucket under the
documented 700 requests/minute (`NEON_API_RATE_PER_MIN`, `NEON_API_BURST`);
bucket waits are part of the verb's cost and are summed in the e2e stats'
`backend_observations`, while reactive waits (429/423/503 backoff, compute
limit) are recorded as `API_RETRY_WAIT` rows. Connections use the unpooled
endpoint: the pooler runs PgBouncer in transaction mode, where session
state (temp tables, the `search_path` postgres_fdw sets on its remote
sessions) leaks between clients. Storage is the sum of the branches'
`logical_size` from the API; there is no GC hook.

---

## PostgreSQL file-copy backend — `file_copy`

Plain PostgreSQL (18 or later) where a branch is a database. The one native
branching primitive is `CREATE DATABASE <child> TEMPLATE <parent> STRATEGY =
FILE_COPY`; with `file_copy_method = clone` (the run sets it server-wide,
`FILE_COPY_METHOD`) the data files are reflinked, so on a copy-on-write
file system (XFS with `reflink=1`, Btrfs, APFS) a clone shares its blocks
with the template until either side writes. Connection settings come from
`PGSQL_USER`, `PGSQL_PASSWORD`, `PGSQL_HOST`, `PGSQL_PORT` (also read from
`.env`). This repository's server is PostgreSQL 18 on an XFS loop volume
mounted at `/mnt/pgbench` (`sudo -u postgres /usr/lib/postgresql/18/bin/pg_ctl
-D /mnt/pgbench/pgdata -l /mnt/pgbench/pg.log start`, port 5434).

Everything beyond branch and delete is built on that clone, on
`postgres_fdw` (one database reading another's tables) and on plain SQL;
`DBToolSuite.implementation()` records which is which:

- `branch()` (composed): the clone of the parent, plus, for a fork from a
  branch head, a second clone of the child kept as the merge base (the
  parent moves on); a fork from a commit uses the commit's snapshot as its
  base. A template may have no other connection while it is copied, so the
  backend keeps every connection it opens in a process-wide registry: a
  clone closes the idle connections to its template (a parked connection
  reopens on its next use, recorded as a `CONNECT`), waits for the busy
  ones (a verb or exec in flight) and holds new ones back until the copy is
  done. The wait is part of the verb's latency; the e2e stats'
  `backend_observations` count `parked_connections`, `quiesce_waits`,
  `quiesce_wait_sec` and `clone_retries`.
- `commit()` (composed): a row in the branch's `_bb_commits` table, then a
  clone of the branch database as the commit's snapshot (read-only; a
  commit ref connects to it). A clone costs two checkpoints plus one
  reflink per data file, with the branch's connections parked meanwhile.
  `log()` reads the table.
- `diff()`, `merge()`, `rebase()`, `revert()` (simulated): the same SQL
  three-way machinery as Neon's (`dblib/pg_merge.py`), on local databases
  instead of computes. The merge base is the fork or last-rebase snapshot
  of whichever side descends from the other; `rebase()` first clones the
  upstream (its head keeps moving under load) and that clone becomes the
  branch's new base, so commits are not replayed and lineage is not
  rewritten; `revert()` applies the inverse of (predecessor snapshot ->
  commit snapshot) and records the result as a commit with its own
  snapshot.
- `reset()` (composed): `DROP DATABASE` of the branch, then `CREATE DATABASE
  ... TEMPLATE <commit snapshot>`; the snapshot's log ends at that commit.
- `delete()` (native): `DROP DATABASE` of the branch, its bases and its
  snapshots; a snapshot another branch's log still references (a commit
  merged into the spine, a fork from a commit) is kept until the run's
  cleanup, so reverting a deleted candidate's commit still works.
- Multi-branch queries (simulated): `db.table(ref, t)` imports the other
  ref's table into the session's database through `postgres_fdw` (once per
  exec; the foreign-server connection is dropped when the exec ends, since
  it would otherwise count as a connection to that database).

Storage: with `PGSQL_DATA_DIR` set to the mount point of a volume dedicated
to the server, storage is the volume's usage minus WAL (`pg_ls_waldir()`),
which counts shared blocks once (`server` scope); without it, the sum of
`pg_database_size()` over the run's databases, which counts them once per
clone. `gc()` is a `CHECKPOINT`. All databases of a run share the prefix
`<db_name>__`; cleanup (and the start of a run) drops them with `FORCE`.

---

## Chronos backend — `chronos`

[Chronos](https://github.com/mitdbg/chronos) (arXiv:2609.14889) is a
bolt-on branching library, not a server: every registered table lives once
in a physical interval table (`_chronos_b_interval_<t>`) whose rows carry a
half-open visibility interval `[live_lo, live_hi)` and a tombstone; a
branch owns an interval and reads the versions that contain its branch
point. A fork is a metadata transaction (the parent's interval is split, no
data is copied), a read is a range predicate, a write splits the inherited
version at the writer's interval. The library speaks its own Python API
(`ChronosBranchContext`: `create_branch`, `checkout` sessions with
`query`/`execute`, `create_checkpoint`, `diff`, `merge_apply`,
`delete_branch`) and runs the application's SQL itself over a parsed
subset. `dblib/chronos.py` is a shim between that API and the suite.

Setup: the backend uses the same PostgreSQL server as `file_copy`
(`PGSQL_*`), one database per run, and needs the optional dependency
(`uv sync --extra chronos`, a source build of `chronos-core` pinned to the
commit the backend was written against; it needs `libpq-dev`, `libboost-dev`,
`libsqlite3-dev`, `python3-dev` and a C++17 compiler). The run's schema and
seed data are loaded as ordinary PostgreSQL tables with full SQL; the first
versioning verb *adopts* them: every table is registered with Chronos (its
rows copied into the interval table, a table without a primary key gets a
synthetic `_bb_rowid` key, the original is dropped) — the integration path
the Chronos documentation describes. From then on every statement goes
through a Chronos branch session. Chronos runs with
`enable_schema_branching=True` because the scenarios create tables and
columns on branches; in that mode Chronos refuses its native
`merge_apply`, so merge and everything derived from it are built here on
its native diff and bulk-write primitives. `DBToolSuite.implementation()`
records which is which:

- `branch()` (composed): `create_checkpoint` on the parent (the fork base,
  kept for later merges) and `create_branch_from_checkpoint`; a fork from a
  commit forks from that commit's checkpoint. Both are metadata
  transactions. Chronos has no checkpoint deletion, so fork bases and
  commits pin their versions for the run.
- `commit()` (composed): `create_checkpoint` (an immutable interval split;
  a commit ref is a read-only session on it) plus a row in the `_bb_log`
  bookkeeping table, which `log()` reads (simulated: Chronos keeps no
  commit history).
- `diff()` (composed): `ChronosBranchContext.diff` between two branches; a
  commit ref is read through a temporary branch forked from its checkpoint.
- `merge()`, `rebase()` (simulated): a three-way merge in the shim —
  native `diff(base, source)` and `diff(base, target)` against the fork or
  last sync checkpoint, conflicts compared by key, the changes applied with
  native `upsert_rows`/`delete_keys`, tables and columns added on the
  source replayed as DDL on the target (a differing key or type is a schema
  conflict and fails the verb). After a merge or rebase the source gets a
  sync checkpoint that becomes the next base. `rebase()` is the same merge
  with the upstream as the source (the branch's own changes are "theirs").
- `revert()` (simulated): the inverse of the diff between the commit's
  checkpoint and its predecessor's, applied with the same primitives, then
  a commit. `reset()` (simulated): the diff from the branch to the commit's
  checkpoint applied back, with tables and columns added since dropped;
  Chronos cannot move a branch's read point.
- `delete()` (native): `delete_branch` (metadata; versions are reclaimed by
  Chronos's background GC). Chronos deletes a branch's whole subtree, so a
  branch with live children is tombstoned (hidden) and deleted once they
  are gone (`deferred_deletes` in `backend_observations`).
- Multi-branch queries (simulated): `db.table(ref, t)` creates a temporary
  view over the interval table with the visibility predicate of the ref's
  branch point — what Chronos itself substitutes for a table — and the
  SELECT, rewritten by the session for its own branch, runs on a plain
  connection.

What the six-scenario sweep showed (PostgreSQL 18 on this box, CH scale 1,
chronos-core 607418d): a fork, checkpoint or delete costs 10 to 25 ms, in
the range of Dolt's and well below the clone-based backends, and the
per-statement overhead of Chronos's own DML executor is 2 to 10× plain
PostgreSQL. Three properties of the design dominate the longer scenarios:

- History accumulates in the live tables. Every write after a checkpoint
  splits the row into an old and a new version in the same physical table,
  checkpoints pin the old versions for the run, and SELECTs carry no index
  on the interval bounds, so analytical queries scan every version. S4
  (spine checkpoint every 2 s under eight TPC-C clients) had four
  order_line versions per visible row after twelve minutes, and the spine's
  CH queries dominated its 41 minutes.
- Branch-local DDL copies the table. `ALTER TABLE` on a branch creates a new
  physical table with the rows visible to that branch (S4: one copy of
  customer per dev branch, one of stock per spine migration).
- Branch management contends with the branch's writers. Every write holds a
  `FOR SHARE` lock on its branch's metadata row and `create_branch`,
  `create_checkpoint` and `delete_branch` need the row exclusively (they
  also raise `chronos_session_barrier_in_progress` instead of waiting when
  another one is in flight). The shim retries the barrier and holds a
  per-branch gate while the management call runs (`gate_waits`,
  `barrier_retries_*` in `backend_observations`); without it S5's branch
  deletes starved under the spine load until the stall watchdog cancelled
  them.

Two more things the shim guards against: the background interval-GC worker
dies on its first error (in S1 "out of shared memory", PostgreSQL's
`max_locks_per_transaction` exhausted by a GC transaction touching every
physical table) and would then block the context's close forever, so the
shim clears the flag and runs the reclamation synchronously in `gc()`
(`gc_worker_died`); the rows a merge applies are re-read from the
source branch rather than taken from the diff's reported values
(`diff_values_stale` counts disagreements); and a bulk `upsert_rows` or
`delete_keys` that raises a duplicate version key falls back to one
statement per row through Chronos's own executor
(`bulk_write_fallback_rows`). The two merge guards came from S6, where the
first run lost one roll-up increment (a merge wrote a row back to its
pre-rebase value) and the second run's merge failed on exactly that
duplicate key; the third run passed every invariant with neither guard
firing, so both failures are intermittent and are recorded, not explained
(`run_stats/macro_20261011/logs/data_agent_chronos_run{1,2}.log`). The
shim's merge is not atomic: it writes table by table into the live target,
so a failure midway leaves a partial merge, unlike Chronos's staged native
merge. Concurrent TPC-C deliveries on
the spine race on the tombstone of the same new_order row and fail with a
duplicate key, which Chronos retries three times before the driver's own
retry (`DELETE_ROWS` failures in the stats).

The SQL shim: SELECTs run as Chronos rewrites them (each registered table
replaced by its visible-version subquery; joins, CTEs, subqueries and
window functions pass through to PostgreSQL). DML is executed by Chronos's
own evaluator over its subset — `INSERT ... VALUES` with a column list,
`UPDATE` with simple assignments, `DELETE`; no `INSERT ... SELECT`,
`RETURNING`, `NOW()`, `BETWEEN`, scalar subqueries. A statement it rejects
is simulated: the shim evaluates it as a SELECT on the branch (PostgreSQL
computes the new values) and applies the rows with
`upsert_rows`/`delete_keys` (`simulated_update` etc. in
`backend_observations`). `CREATE TABLE` is normalised to what Chronos
accepts (table-level `PRIMARY KEY` made inline; `NOT NULL`, `UNIQUE`,
`CHECK`, `FOREIGN KEY` and non-constant defaults dropped; a keyless table
gets `_bb_rowid`), `CREATE VIEW` is simulated by inlining the view's query
into later SELECTs, a `UNIQUE` index becomes a plain one (Chronos enforces
no constraints beyond the logical key). Parameters are translated from
`%s` to Chronos's named form. Storage is `pg_database_size()` of the run's
database (`database` scope); `gc()` waits for Chronos's background
reclamation and runs `VACUUM`.

---

## TerminusDB backend — `terminusdb`

[TerminusDB](https://terminusdb.org) is a document/graph database with
git-like branches. It has **no SQL interface**: data goes through a JSON
document API, WOQL (a Datalog-style query language) and GraphQL, and every
write request is one commit. The macrobenchmark issues SQL, so TerminusDB
is wired for the **microbenchmark only**; `dblib/terminusdb.py` runs no
SQL and is not in the macrobenchmark's backend list.

Start a server and install the one extra dependency (a SQL parser, used to
recognise the microbenchmark's statements):

```bash
docker run -d --name terminusdb -p 127.0.0.1:6363:6363 \
    -e TERMINUSDB_ADMIN_PASS=root terminusdb/terminusdb-server:latest
uv sync --extra terminusdb
TERMINUSDB_REUSE_LOAD=1 ./scripts/run_single_thread_bench.sh terminusdb db_setup/ch-w1.sql 16 \
    --operations READ,INSERT,BRANCH_CREATE,BRANCH_MERGE
uv run pytest tests/test_terminusdb_live.py tests/test_tpcc_woql_live.py
```

| Variable | Meaning (default) |
|----------|-------------------|
| `TERMINUSDB_URL` | server (`http://127.0.0.1:6363`) |
| `TERMINUSDB_USER`, `TERMINUSDB_PASSWORD` | credentials (`admin`, `root`) |
| `TERMINUSDB_TEAM` | organisation that owns the database (`admin`) |
| `TERMINUSDB_STORAGE_DIR` | host path of the server's storage directory, for storage measurements (whole server) |
| `TERMINUSDB_CONTAINER` | alternatively, the Docker container to run `du` in |
| `TERMINUSDB_LOAD_BATCH` | documents per request while loading (`5000`) |
| `TERMINUSDB_REUSE_LOAD` | `1`: keep the loaded database between runs and restore it (delete the other branches, reset `main`) instead of loading the dump again |
| `TERMINUSDB_TIMEOUT_SEC` | give up on a request after this long (wait forever) |

### Data model and statements

A table is a document class whose properties are the columns (all
optional); a row is a document with the id `<table>/<pk1>_<pk2>...`. The
loader reads the SQL dumps the other backends load (`CREATE TABLE`,
`ALTER TABLE ... PRIMARY KEY`, `COPY ... FROM stdin`, `INSERT`) and writes
a schema plus documents; each class keeps its column list, SQL types and
primary key in `@metadata`. `util/db_helpers.py` reads that catalog where
it queries `information_schema` on the SQL backends.

The microbenchmark sends SQL text through `exec()`. The backend parses
each statement once and issues the matching native call:

| Statement | TerminusDB call |
|-----------|-----------------|
| `SELECT ... WHERE <whole primary key> = ...` | `GET` one document by id |
| `INSERT INTO t (...) VALUES (...)` | `POST` documents |
| `UPDATE` of every non-key column `WHERE <whole primary key>` | `PUT` (replace) one document |
| `DELETE ... WHERE <whole primary key>` | `DELETE` one document by id |
| `SELECT` with any other conjunctive filter, `COUNT(*)`, `LIMIT` | one WOQL query |
| `UPDATE` / `DELETE` with any other filter | one WOQL query, one commit |

An equality in a filter is a triple with the value as its object (an index
lookup); a range bound binds the column and compares, and a row-value
comparison `(a, b) >= (x, y)` is expanded lexicographically. Anything else
(joins, `GROUP BY`, `ORDER BY`, expressions in `SET`, DDL, `BEGIN`) raises
`NotSupportedError` and the `exec()` is recorded `FAILED`. The `DDL_*`
operations are therefore not available.

### Verbs

| Verb | Implementation |
|------|----------------|
| `branch`, `delete`, `log`, `diff`, `reset`, `rebase` | one native call each |
| `commit` | every write is already a commit, so `commit()` writes nothing: it reads the branch head and returns it as the commit id (`simulated`). The first write since the previous `commit()` is remembered from the write responses, so `revert` can undo all a `commit()` covers |
| `merge` | TerminusDB has no merge. Composed: the source head, the common ancestor (remembered per branch pair; found from the two logs the first time) and one `apply` of the diff between them as a squash commit. `apply` refuses a conflicting diff (HTTP 409, nothing applied) and names the documents; `ours` then applies the source's other documents and `theirs` all of them. A callable policy is treated as `ours` |
| `revert` | one log request for the parent of the first write the commit covers, then `apply` of the inverse diff |
| connection switch | the API is stateless: a switch changes the URL of the next request and costs nothing |
| commit refs | reads at `branch@commit` are native; writes there are refused |

`exec_async` and multi-branch exec are not supported.

### What the microbenchmark measured

One run on TerminusDB 12.0.7 in Docker on a laptop (WSL2), `db_setup/ch-w1.sql`
(609,078 rows in 12 tables), table `orders` (30,000 rows), 16 spine
branches, one thread; `results/terminusdb/micro_w1/run.sh` is the driver.
Loading the dump took about 12 minutes.

| Operation | Median | p95 | n |
|-----------|--------|-----|---|
| Read (one row) | 6.8 ms | 8.0 ms | 1000 |
| Insert | 43 ms | 68 ms | 1000 |
| Update (one row) | 47 ms | 101 ms | 1000 |
| Delete (one row) | 51 ms | 77 ms | 1000 |
| Range read (200 rows) | 1.3 s | 1.3 s | 200 |
| Range update (200 rows) | 1.6 s | 2.8 s | 200 |
| Branch create | 18 ms | 21 ms | 50 |
| Connect | < 0.01 ms | < 0.01 ms | 949 |
| Commit | 12 ms | 19 ms | 100 |
| Diff | 21 ms | 26 ms | 100 |
| Log (10 entries) | 25 ms | 28 ms | 100 |
| Merge | 121 ms | 162 ms | 100 |
| Rebase | 21.8 s | 40.7 s | 20 |
| Revert | 131 ms | 228 ms | 100 |
| Reset | 20 ms | 26 ms | 100 |
| Branch delete | 18 ms | 35 ms | 15 |

* **Every write is a commit**, so the insert, update and delete latencies
  include one. A point read is one request.
* **Range operations scan.** A range on the primary key is a WOQL filter
  over the property triples of the whole class; there is no ordered index
  to seek in, so a 200-row range over 30,000 rows costs over a second.
* **Rebase grows with the database.** It took 0.3 s on an almost empty
  database and about 22 s on this one (two sizes measured, nothing in
  between). Merge and revert, which use `apply`, stayed near 0.1 s.
* **Rebase fails now and then with a bare HTTP 500** and leaves the branch
  untouched; the same request then succeeds. The backend repeats it (up to
  4 attempts) and counts `rebase_retries` in the summary's
  `backend_observations`. In an isolated test 3 of 40 rebases needed it;
  3 of the 20 here took about twice the median.
* **Concurrent writers slow each other down.** In one small run with four
  threads, each inserting on its own branch, an insert took about 280 ms
  against 30 ms with one thread on the same data, and total throughput
  fell from about 33 to 6 inserts per second. Not investigated further.
* **One TCP connection per request.** On a kept-alive connection the
  response body waits for a delayed ACK: a point read took 56 ms against
  5 ms on a fresh connection. The backend sends `Connection: close`.

### The macrobenchmark's workload in WOQL

`macrobench/tpcc_woql.py` is `macrobench/tpcc.py` written by hand for
TerminusDB: the five TPC-C transactions and the five CH queries, one
document-API or WOQL request per SQL statement, in the same order.
`tests/test_tpcc_woql_live.py` loads the same seeded data into PostgreSQL
and TerminusDB, runs the SQL on one and the WOQL on the other, and checks
that the transactions return the same values, leave the same table
contents, and that the five queries return the same rows.

```bash
uv run python scripts/run_terminusdb_tpcc.py --output-dir run_stats/terminusdb_tpcc
```

seeds the CH dataset the macrobenchmark uses (seed 42, one warehouse), runs
a TPC-C mix on a fresh branch, then each CH query, and records every
request under the SQL statement it stands for, in the usual parquet layout.

One run on the same server, at the macrobenchmark's default scale
(608,435 rows: 100,000 items and stock rows, 30,000 customers and orders,
299,394 order lines; seeding took 13 minutes), 200 transactions and three
runs of each query looking back 7 days; results in
`results/terminusdb/tpcc_woql/`:

| Transaction or query | Median | p95 | n |
|----------------------|--------|-----|---|
| new_order (about 35 requests) | 1.2 s | 2.1 s | 96 |
| payment (4 requests) | 158 ms | 279 ms | 80 |
| delivery (10 districts, 7 requests each) | 11.9 s | 22.8 s | 6 |
| order_status (3 requests) | 71 ms | 137 ms | 7 |
| stock_level (2 requests) | 232 ms | 574 ms | 11 |
| CH Q1 | 238.6 s | 240.8 s | 3 |
| CH Q3 | 32.0 s | 32.1 s | 3 |
| CH Q6 | 15.0 s | 15.0 s | 3 |
| CH Q12 | 183.3 s | 184.2 s | 3 |
| CH Q18 | 29.1 s | 29.1 s | 3 |

All 200 transactions and all 15 query runs completed. A request that
writes one row took 29 to 43 ms (a commit each) and a read by primary key
8 to 16 ms, whatever the statement, so a transaction costs roughly its
number of requests times that. The statements that are not a lookup are
slower: `MIN(no_o_id)` over a district's 900 pending orders took 0.6 s and
the stock-level count 0.2 s. The CH queries, which scan and group the
order lines, took from 15 s (Q6) to 4 minutes (Q1). WOQL does not reorder a
conjunction, so these timings depend on how each query is written; they
are one hand-written form, checked for correctness but not tuned by a
TerminusDB expert.

This is a measure of TerminusDB running this relational workload, not of
TerminusDB at its best: the tables are kept as flat documents with foreign
keys as plain values, so every join is a value join, where a schema
designed for TerminusDB would link documents. A sequence of requests is
also not a transaction: each write commits on its own, and another client
can interleave.

---

## Environment Variables

Create a `.env` file for backend-specific configuration:

```bash
# Neon API key for programmatic access
NEON_API_KEY_ORG=your_key_here

# PostgreSQL server of the file_copy backend (PostgreSQL 18, file_copy_method = clone)
PGSQL_USER=postgres
PGSQL_PASSWORD=password
PGSQL_HOST=127.0.0.1
PGSQL_PORT=5434
PGSQL_DATA_DIR=/mnt/pgbench   # volume dedicated to the server; storage = its usage minus WAL
# The chronos backend uses the same server (PGSQL_*), one database per run.

# TerminusDB server of the terminusdb backend (see its section for the rest)
TERMINUSDB_URL=http://127.0.0.1:6363
TERMINUSDB_USER=admin
TERMINUSDB_PASSWORD=root

# Database connection strings (if needed)
DOLT_CONNECTION_STRING=postgresql://user:pass@localhost:5432/dbname
NEON_CONNECTION_STRING=postgresql://user:pass@host.neon.tech/dbname

# Bauplan (see "Bauplan backend"); the key can instead come from ~/.bauplan/config.yaml
BAUPLAN_API_KEY=...
BAUPLAN_S3_URI=s3://<bucket>/<prefix>
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-2
```

---

## Additional Resources

- **Scenario configurations**: See `macrobench/configs/` and `macrobench/task.proto`
- **Microbenchmark configs**: See `microbench/configs/` for example configurations
- **Database schemas**: See `db_setup/` for SQL dump files

