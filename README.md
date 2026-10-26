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

The `_bb_commit_id` index was added to speed commit-id lookups. The earlier
whole-workflow measurements do **not** isolate its net effect: the extra hidden
index table cloned with each branch and index maintenance on commit inserts
are included in the totals. The later
[commit-log profiler](scripts/profile_matrixone_commit_log.py) compares copying,
lookups, and log-row inserts with/without the index, as described below. It
still does not measure clone/storage costs or complete commit latency, so an
overall index benefit remains unproven.

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

The same option records `server_lca_probe_statements`: matching
`sql_executor exec` records for the branch engine's LCA snapshot SELECTs during
each operation. It excludes earlier log entries, duplicate SQL text emitted by
other loggers, reader fallback scans, and other internal SQL. This is separate
from the client `statements` count; without `--server-log`, the field is absent.

`server_branch_dag_statements` counts the branch engine's full, unlocked
`mo_branch_metadata` graph SELECTs in the same operation log interval. It
excludes `FOR UPDATE` publication checks and duplicate text from other loggers.
It measures graph reads, separately from the LCA row probes above, and is absent
without `--server-log`. Both counters require the internal executor's INFO logs;
they are not a count of all internal SQL.

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

MO commit `5a2943202` removes insert staging from MERGE. Ordinary inserts now
use one batched `INSERT INTO target (columns) VALUES ...` through the same
transaction/statement executor used by staged writes. This retains RC retry
handling and rollback, including rollback of earlier tables when a later
table fails. At this commit, UPDATE and DELETE retained their on-demand stages;
later integer fast paths are described below. PICK, portable SQL output, and
the existing keyless/exact-float insert paths are unchanged.

Compared with `500f53066`, this removes three staging DDL statements per table
that needs ordinary inserts, and reduces each insert batch from three DML
statements (stage load, INSERT SELECT, stage clear) to one. Thus INSERT-only
MERGE creates no stages; at this commit INSERT+UPDATE used one, and
INSERT+DELETE+UPDATE used two. These are server-generated statements; client
SQL counts do not change.

A fresh comparison on 2026-10-25 used baseline `500f53066`, direct-insert build
`5a2943202`, and the same clean adapter `ac82249`. The workload and procedure
were the same as above: three repetitions per interface/table count, alternating
interface order, with the baseline server measured first. Medians in ms:

| Interface / tables | Client merge before → after | Server merge before → after | Stage init + cleanup before → after |
| --- | ---: | ---: | ---: |
| Table summary / 1 | 245 → 227 | 113 → 83 | 32 → 20 |
| Database / 1 | 270 → 230 | 114 → 88 | 44 → 21 |
| Table summary / 10 | 1274 → 933 | 1139 → 744 | 361 → 198 |
| Database / 10 | 1199 → 896 | 1046 → 756 | 357 → 205 |

In this sample, ten-table client latency fell about 27% for table summaries
and 25% for database MERGE. This is a small local diagnostic comparison, not a
throughput or tail-latency result. Client statement counts remain 24/15 for
ten-table table/database merges and 15 for both one-table modes. The same
profiler command above reproduces the workflow; use separate logs/output files
for the two server revisions.

Regression checks cover precise decimals, binary values, ENUM, NULL, generated
columns, defaults, source-added columns, unique constraint failures, explicit
transaction rollback, and database-wide rollback after a later table fails.
The engine's existing schema compatibility rules still apply; the adapter
continues to reconcile schema before native merge. Live checks also confirmed
zero staging time for insert-only merges across all three MERGE interfaces.

MO commit `b9342f82a` clears each MERGE UPDATE/DELETE stage immediately before
its next load, instead of after every applied batch. The final batch is
reclaimed by DROP. A stage consumed by N batches now needs N−1 clearing
statements instead of N; a single-batch stage needs none. Staging DDL and
client SQL counts are unchanged. Clearing still uses the same transaction and
retryable statement executor, and remains charged to `apply`; final DROP
remains in `stage_cleanup`. PICK and portable SQL output retain their existing
clearing behavior.

A fresh comparison on 2026-10-25 used baseline `5a2943202`, optimized build
`b9342f82a`, and clean adapter `60be6f1`. It used the same workload, INFO logs,
three repetitions, alternating interfaces, and baseline-first procedure above.
Medians in ms:

| Interface / tables | Client merge before → after | Server merge before → after | Apply before → after | Stage init + cleanup before → after |
| --- | ---: | ---: | ---: | ---: |
| Table summary / 1 | 203 → 205 | 69 → 59 | 29 → 7 | 19 → 22 |
| Database / 1 | 186 → 214 | 69 → 60 | 31 → 7 | 16 → 23 |
| Table summary / 10 | 940 → 665 | 771 → 508 | 284 → 60 | 214 → 194 |
| Database / 10 | 762 → 656 | 644 → 496 | 293 → 67 | 179 → 214 |

Ten-table client latency improved about 29%/14% for table/database merges;
their apply phases fell about 79%/77%. One-table client latency increased
about 1%/15%, despite lower server times. The database case also spent more
time initializing/dropping stages. These small local samples show the removed
apply work, not a universal latency improvement. All 24 before/after runs
checked conflicts and data after merge/revert/reset, with complete server
timings; client merge counts remain 15 for one table and 24/15 for ten tables.
Use the same profiler command above with these server revisions to reproduce.

Regression coverage includes repeated stage loads, clear failures, cancellation,
and a database merge with 90,000 updates plus 85,000 deletes, exceeding the
81,920-row flush limit. The large case checks both explicit transaction rollback
and committed results. Eighteen live cases across plain/table-summary/database
MERGE check data, summaries, timings, and absence of leaked stages.

A separate probe exposed an existing large repeated-merge failure in
`5a2943202` and `b9342f82a`: seed 90,000 `(id,v)=(i,i)` rows,
fork the database, increment every source `v`, and merge with ACCEPT; increment
every source `v` again, then repeat database MERGE with OUTPUT SUMMARY inside
BEGIN. The second merge could fail with duplicate primary key error 1062.

MO commit `272d41114` fixes the underlying change-compaction bug. Removing an
old tombstone could free the memory backing the iterator's primary key; a
subsequent lookup could then lose the surviving deletion marker and classify
an UPDATE as an INSERT. A reusable key buffer per shard now keeps that key
valid throughout compaction. This adds no SQL statements and requires no
adapter change.

The deterministic regression clears freed allocator buffers and checks repeated
updates, update-then-delete, and insert/update/delete histories. It fails on the
old code and passes with the fix. The original 90,000-row SQL regression checks
database-summary rollback and plain MERGE commit. Live checks on the built
`272d41114` server also pass rollback, commit, and an unchanged repeat merge for
plain, table-summary, and database interfaces, checking row values, summary
counts, and absence of leaked stages. The Branch/hashmap/snapshot-join regression
suite passes as well.

MO commit `3f54a1a83` applies small integer MERGE updates with
`UPDATE target JOIN (VALUES ROW(...), ...) ... SET ...`, avoiding UPDATE staging.
Every projected writable value and matching primary-key component must be an
integer. Explicit casts preserve signedness, uint64 limits, and all-NULL columns.
Each update batch is limited to 128 rows and 512 values, counting both writable
columns and the repeated matching keys. Larger batches retain the original raw
VALUES for staging; unsupported types and keyless updates retain their existing
paths. PICK and portable SQL output are unchanged.

For an eligible UPDATE-only merge with one batch, server-generated apply SQL
falls from five statements (initial DROP, CREATE, stage load, UPDATE, final DROP)
to one UPDATE. INSERT plus eligible UPDATE needs no stages; at this commit
DELETE retained its stage. Batches may switch between inline and staged updates
within one merge, preserving clear-before-reuse semantics. Both paths use the same
transaction and retryable statement executor. Client SQL counts do not change.

A comparison on 2026-10-25 used baseline `272d41114`, final build `3f54a1a83`,
and the same clean adapter `39a583f`. It used the profiler command above with
1,000 rows/table, 100 source updates plus one insert, 50 conflicting destination
updates, and the `ours` policy: 50 updates/table are applied. Each server ran
three repetitions per interface/table count, alternating interfaces, with the
baseline measured first. Medians in ms:

| Interface / tables | Client merge before → after | Server merge before → after | Stage init + cleanup before → after |
| --- | ---: | ---: | ---: |
| Table summary / 1 | 212 → 169 | 47 → 35 | 19 → 0 |
| Database / 1 | 183 → 195 | 50 → 38 | 20 → 0 |
| Table summary / 10 | 711 → 510 | 543 → 343 | 200 → 0 |
| Database / 10 | 633 → 438 | 483 → 287 | 207 → 0 |

Ten-table client latency fell about 28%/31% for table/database MERGE. One-table
database client latency increased about 7%, despite reduced server time. All
final optimized runs recorded zero staging initialization/cleanup time, and
all 24 before/after runs checked conflicts and merge/revert/reset data with
complete server timings. Client merge statements remain 15 for one table and
24/15 for ten-table table/database modes. These are small local diagnostic
samples, not throughput or tail-latency guarantees.

The batch limits avoid the parsing cost of large typed VALUES expressions.
An uncapped prototype slowed a 90,000-row update from 5.94 s to 10.36 s, so large
updates keep staging. A separate bulk check on the final build used table MERGE
with ACCEPT, updated every source row, and made no destination changes. Three
fresh database pairs per size/build produced these client medians in ms:

| Updated rows | Baseline `272d41114` | Final `3f54a1a83` |
| --- | ---: | ---: |
| 1,000 | 138 | 84 |
| 10,000 | 754 | 865 |
| 90,000 | 5939 | 5700 |

All these final bulk batches use staging. The 10,000-row median increased 15%;
these samples do not establish a bulk-update speedup. To reproduce this workload,
use a fresh database pair for each repetition, change the series upper bound for
each size, and time only the MERGE statement over the client connection:

```sql
CREATE DATABASE bulk_dst;
CREATE TABLE bulk_dst.t(id INT PRIMARY KEY, v INT);
INSERT INTO bulk_dst.t SELECT result,result FROM generate_series(1,90000) g;
DATA BRANCH CREATE DATABASE bulk_src FROM bulk_dst;
UPDATE bulk_src.t SET v=v+1;
DATA BRANCH MERGE bulk_src.t INTO bulk_dst.t WHEN CONFLICT ACCEPT OUTPUT SUMMARY;
SELECT COUNT(*),SUM(v<>id+1) FROM bulk_dst.t;
DROP DATABASE bulk_src;
DROP DATABASE bulk_dst;
```

Regression coverage checks row/value limits, inline batches between staged
loads, failures and cancellation, integer boundaries, composite keys, NULL,
generated/default columns, source additions, unique constraints, explicit
rollback, and database rollback on a later-table failure. The 90,000-row
repeated-update and DECIMAL stage-reuse cases also pass. Eighteen live checks
across plain/table-summary/database MERGE verify data, counts, complete timings,
and cleanup, including zero staging time for small integer updates.

MO commit `322c08ee1` coalesces small serial LCA probes across hash shards.
Each side's tombstone keys share one query when there are at most 1,024 keys
and they fit the existing memory-derived batch limit. Source and destination
still probe their respective ancestor snapshot timestamps separately. Larger
inputs, tighter memory limits, and parallel consumers keep the per-shard path.
The change benefits serial branch operations, including MERGE, without changing
the SQL interface or client round trips.

The larger batches exposed a mixed-update bug: ACCEPT can restore rows deleted
on the destination and update other rows in the same batch. The old retained-row
filter removed the ordinary updates instead of retaining them. The commit also
corrects that filter. Regression tests cover integer and DECIMAL apply paths,
composite keys, equal changes, all three MERGE interfaces, explicit rollback,
commit, and repeated no-op merge. A separate unit regression checks one probe
instead of ten, the 1,024-key boundary, memory limits, missing ancestor rows,
query/delivery failures, and cancellation.

A fresh comparison on 2026-10-25 used baseline `3f54a1a83`, optimized server
`322c08ee1`, and the same clean adapter `8e3151d`. It used the profiler command
above with 1,000 rows/table, 100 source updates plus one insert, 50 conflicting
destination updates, and `ours`. Each server ran three repetitions per
interface/table count, alternating interfaces, with the baseline first.
Medians in ms; query counts are actual executor log entries:

| Interface / tables | Client merge before → after | Server merge before → after | Diff before → after | LCA probe SQL before → after |
| --- | ---: | ---: | ---: | ---: |
| Table summary / 1 | 160 → 156 | 27 → 22 | 12 → 7 | 20 → 2 |
| Database / 1 | 180 → 171 | 30 → 27 | 15 → 7 | 20 → 2 |
| Table summary / 10 | 541 → 331 | 376 → 206 | 202 → 56 | 200 → 20 |
| Database / 10 | 412 → 275 | 283 → 140 | 179 → 48 | 200 → 20 |

Ten-table client latency fell about 39%/33% for table/database MERGE. Their
diff phases fell about 72%/73%, while logged LCA queries fell 90%. One-table
client improvements were only 3%/5%. Client statement counts remain 15 for one
table and 24/15 for ten-table table/database modes. All 24 runs checked merge,
revert, reset, and conflict counts, and had complete server timing records.
The new profiler field makes the internal query reduction visible in subsequent
JSON output; the comparison above counted the same executor records within the
recorded MERGE request intervals.

The separate all-row UPDATE workload and SQL recipe above produced these
three-run client medians. These measurements do not show a bulk-update speedup:

| Updated rows | Baseline `3f54a1a83` | Optimized `322c08ee1` |
| --- | ---: | ---: |
| 1,000 | 120 | 124 |
| 10,000 | 748 | 894 |
| 90,000 | 5875 | 6176 |

The 10,000/90,000-row cases keep per-shard probes. These small local samples
support the reduction in queries for small change sets, not a universal latency
or throughput improvement. The Branch/hashmap/snapshot-join Go regression suite
and the 90,000-row repeated-merge tests pass. Eighteen live checks verify all
three interfaces, data/counts, complete timings, expected probe counts, and
absence of leaked stages.

MO commit `91e759ddb` applies small integer-key MERGE deletions directly with
`DELETE FROM target WHERE key IN (...)`, including tuple matching for composite
keys. All explicit primary-key components must be signed or unsigned integers;
non-key columns may have other types. Each deletion batch is limited to 128
rows and 512 key values. Larger batches, noninteger keys, and keyless tables
retain staging; PICK and portable SQL output are unchanged.

An eligible single-batch DELETE-only merge now generates one apply statement
instead of five (initial DROP, CREATE, stage load, DELETE, final DROP). Small
eligible INSERT/UPDATE/DELETE mixtures need no stages. Direct and staged deletes
use the same transaction and retryable executor; a direct batch between staged
batches preserves clear-before-reuse behavior. Client SQL counts do not change.

The profiler now accepts `--mutation delete`, recorded as `mutation` in each
run. It deletes the first `--changed` source keys and inserts one source row;
the destination updates the first half of those keys. Under `ours`, conflicting
deletions are skipped. The default remains `--mutation update`. Both workloads
check conflicts and data after merge, revert, and reset, including odd change
counts. Run against each build separately with an exclusive INFO JSON log:

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_operations \
  --tables 1 10 --rows 1000 --changed 100 --mutation delete --reps 3 \
  --modes summary database --server-log /tmp/mo.json.log \
  --out /tmp/mo-delete-profile.json
```

A comparison on 2026-10-25 used baseline `322c08ee1`, built server `91e759ddb`,
and the unchanged `cc52d4a` adapter with the profiler extension above. Both
JSON files record a dirty tracked tree because this profiler change was not
yet committed. Each server ran three repetitions per interface/table count,
alternating interfaces, with the baseline first. Each table had 1,000 rows,
100 source deletions plus one insert, and 50 destination updates; SKIP applied
50 deletions/table. Medians in ms:

| Interface / tables | Client merge before → after | Server merge before → after | Stage init + cleanup before → after |
| --- | ---: | ---: | ---: |
| Table summary / 1 | 181 → 148 | 43 → 15 | 23 → 0 |
| Database / 1 | 177 → 177 | 40 → 20 | 20 → 0 |
| Table summary / 10 | 552 → 291 | 365 → 151 | 196 → 0 |
| Database / 10 | 491 → 254 | 337 → 114 | 214 → 0 |

Ten-table client medians fell about 47%/48% for table/database MERGE. One-table
database client latency was unchanged despite lower server time. All 24 runs
checked conflicts and merge/revert/reset data with complete server timings.
Client merge statements remain 15 for one table and 24/15 for ten-table
table/database modes. These are small local diagnostic samples, not throughput
or tail-latency guarantees.

A separate table-summary MERGE with ACCEPT, no destination changes, and three
fresh database pairs per size/build checked threshold and bulk behavior:

| Seeded rows | Deleted rows | Baseline client ms | Optimized client ms | Optimized staging |
| --- | ---: | ---: | ---: | --- |
| 256 | 128 | 43 | 13 | none |
| 256 | 129 | 43 | 33 | retained |
| 1,000 | 900 | 56 | 55 | retained |
| 10,000 | 9,000 | 145 | 141 | retained |
| 90,000 | 85,000 | 769 | 747 | retained |

The 128-row case records zero staging time; 129 rows and the bulk cases retain
staging. These samples show no bulk regression but do not establish a bulk
speedup. To reproduce, vary the seed/delete bounds below and time only MERGE:

```sql
CREATE DATABASE delete_dst;
CREATE TABLE delete_dst.t(id INT PRIMARY KEY, v INT);
INSERT INTO delete_dst.t SELECT result,result FROM generate_series(1,90000) g;
DATA BRANCH CREATE DATABASE delete_src FROM delete_dst;
DELETE FROM delete_src.t WHERE id<=85000;
DATA BRANCH MERGE delete_src.t INTO delete_dst.t WHEN CONFLICT ACCEPT OUTPUT SUMMARY;
SELECT COUNT(*),SUM(v),MIN(id),MAX(id) FROM delete_dst.t;
DROP DATABASE delete_src;
DROP DATABASE delete_dst;
```

Regression coverage includes signed/unsigned integer boundaries, composite and
quoted keys, nullable non-key columns, SKIP/ACCEPT, explicit rollback, repeated
no-op merges, and rollback of an earlier direct DELETE when a later table fails.
Lifecycle tests check row/value limits, direct batches between staged loads,
failure/cancellation, and type/mode fallback. The Branch/hashmap/snapshot-join
suite and existing 90,000-row stage-reuse/repeated-merge regressions pass.
Eighteen checks against the built server cover plain, table-summary, and
database MERGE, data/counts, complete timings, and absence of leaked stages;
small eligible delete and mixed workloads record zero staging time.

MO commit `d33aa9d38` loads and builds the branch graph once per database
DIFF/MERGE statement, reusing it across the serial table loop. This applies only
to the server's private transaction, which already holds the lineage lifecycle
gate. Explicit user transactions retain their per-table reads. The graph is
loaded after authorization, lazily at the first table that needs it, and is
never kept across statements or on a session. Empty databases and DIFFs with
only one-sided tables issue no graph query. Table-level interfaces are unchanged.

For ten paired tables this reduces graph SELECTs from ten to one for both
database DIFF and MERGE. It does not change LCA row probes, client SQL counts,
conflict policy, or transaction boundaries. `server_branch_dag_statements` makes
this reduction visible in profiler JSON; MERGE's `lineage` phase includes the
shared load in the first table's time and only graph traversal in later tables.

A comparison on 2026-10-25 used baseline `91e759ddb`, built server `d33aa9d38`,
and the unchanged `cb09515` adapter with the new profiler counter. Both result
files record a dirty tracked tree for that instrumentation change. The default
UPDATE workflow used 1,000 rows/table, 100 source updates plus one insert,
50 conflicting destination updates, and `ours`. Each server ran three
repetitions per interface/table count, alternating interfaces, baseline first.
Medians in ms:

| Interface / tables | Client MERGE before → after | Server MERGE before → after | Lineage before → after | MERGE graph SELECTs before → after |
| --- | ---: | ---: | ---: | ---: |
| Table summary / 1 | 158 → 135 | 17 → 23 | 1.1 → 1.1 | 1 → 1 |
| Database / 1 | 155 → 149 | 22 → 23 | 1.8 → 1.1 | 1 → 1 |
| Table summary / 10 | 349 → 372 | 199 → 217 | 14.0 → 16.2 | 10 → 10 |
| Database / 10 | 349 → 311 | 184 → 152 | 18.3 → 2.1 | 10 → 1 |

Ten-table database MERGE's lineage phase fell about 89%, and its client median
fell about 11%. The unoptimized table-level control became slower. Database
DIFF also reduced graph queries from ten to one, but its client median rose
from 90 to 100 ms (one-table: 22 to 29 ms). These small local samples demonstrate
the query reduction, not a general DIFF latency improvement or a throughput
guarantee. All 24 workflow runs checked conflicts and merge/revert/reset data
with complete MERGE timings. Ten-table database MERGE still uses 15 client
statements and 20 LCA row probes; database DIFF still uses 9 client statements.

Reproduce against each build separately, with its own INFO JSON log and output:

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_operations \
  --tables 1 10 --rows 1000 --changed 100 --reps 3 \
  --modes summary database --server-log /tmp/mo.json.log \
  --out /tmp/mo-dag-profile.json
```

Regression checks cover empty graphs, failures/cancellation, system-account
context restoration, and fresh metadata for a new request. The database SQL
regression uses opposite fork directions, then an ALTER generation and a new
clone edge on the same connection; DIFF, SKIP/ACCEPT, rollback, and no-op repeat
results must remain correct. Existing Branch/hashdiff/lifecycle/snapshot tests
also pass. The profiler test checks current-operation attribution and excludes
locking queries, malformed entries, and duplicate loggers.
Eleven checks against the built server verify both database operations with
current/snapshot endpoints and private/explicit transactions, plus empty and
one-sided inputs. Two paired tables issue one graph query privately and two
inside BEGIN; empty/one-sided operations issue zero. Snapshot checks include
an ALTER and a dropped source table after the snapshot, and explicit MERGE
rollback restores both destination tables.

The adapter now reads both MERGE endpoint catalogs in one SQL statement before
schema reconciliation, including on the table-summary and legacy paths. A small
derived table tags each requested database with an input position; SQL performs
the case-insensitive name match, so identical table names, case aliases, and
empty catalogs stay separate. A constant schema filter also lets the planner
restrict the information-schema joins. Columns, defaults, and primary-key index
order retain their existing meanings. Database names are bound parameters.

Schema reconciliation uses the same combined read when neither catalog was
supplied, including during rebase. Supplied catalogs are preserved. Nothing is
cached between operations: a later schema change or branch generation is read
again, and historical source tables still get their visibility checks. The
post-merge schema fingerprint is still read afresh for the commit log; it is
not inferred from the pre-merge catalogs.

For the profiler's workload, every MERGE mode saves one client statement:

| Interface | 1 table before → after | 10 tables before → after |
| --- | ---: | ---: |
| Legacy | 19 → 18 | 64 → 63 |
| Table summary | 15 → 14 | 24 → 23 |
| Database | 15 → 14 | 15 → 14 |

Measured on 2026-10-25 against the same running `d33aa9d38` server, with the
clean `ccc50f3` adapter as baseline and this catalog change applied afterward.
The after JSON records the baseline HEAD with `adapter_dirty=true`; the source
change is the one described here, and comparison artifacts include its source
hash. The default UPDATE workflow used 1,000 rows/table, 100 source updates plus
one insert, 50 destination updates, and `ours`. Each adapter ran three
repetitions per mode/table count, alternating mode order, baseline first.
Medians in ms; catalog time is the per-run sum of the two old queries or the
single new query, excluding the later fingerprint:

| Interface / tables | Client MERGE before → after | Catalog queries before → after | Server MERGE before → after |
| --- | ---: | ---: | ---: |
| Legacy / 1 | 203 → 165 | 76 → 29 | 24 → 23 |
| Table summary / 1 | 181 → 139 | 96 → 53 | 21 → 23 |
| Database / 1 | 157 → 134 | 74 → 30 | 24 → 23 |
| Legacy / 10 | 806 → 803 | 76 → 32 | 212 → 220 |
| Table summary / 10 | 373 → 370 | 56 → 54 | 208 → 237 |
| Database / 10 | 285 → 245 | 58 → 30 | 148 → 156 |

The ten-table database client median fell about 14%; ten-table legacy and
table-summary totals were essentially unchanged. These are small local samples,
not a general latency or throughput guarantee. All 36 workflows checked
conflicts and merge/revert/reset data with complete server MERGE timings. Each
after merge issued one combined catalog query and still issued one fresh
fingerprint query. Returned row counts were unchanged; native/legacy execution
path reporting retained its existing values.

To reproduce, use the same server build and run each adapter revision separately:

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_operations \
  --tables 1 10 --rows 1000 --changed 100 --reps 3 \
  --modes legacy summary database --server-log /tmp/mo.json.log \
  --out /tmp/mo-catalog-profile.json
```

This step changes only the adapter; measurements use server `d33aa9d38` for both
versions. Correctness coverage includes metadata isolation, case aliases, empty
databases, composite key order, defaults, quoted names, filtering views and
bookkeeping tables, schema refresh, and error propagation before any schema
write. All 104 MatrixOne adapter tests pass, including live merge/rebase,
snapshot refs, custom resolvers, conflicts, reset, and revert.

Commit-log copying now uses two equality `NOT EXISTS` predicates, one for
`seq` and one for `id`, instead of one predicate containing `OR`. The statement
still copies only source `commit`/`merge` rows absent from the destination by
both keys, with the same optional historical sequence cutoff. It remains one
`INSERT ... SELECT`, with no client-side key cache. MERGE and rebase both use
this helper. Non-unique commit IDs remain supported, including two new source
rows sharing an ID; a repeated copy observes the newly populated destination.

On server `d33aa9d38`, EXPLAIN shows the old disjunctive anti-join replaced by
two equality anti-joins. This avoids the long-history scaling problem without
requiring the commit-id index. Measured on 2026-10-25 with adapter `1059271` plus
this change: synthetic logs with N source commits and 90% already present on
the destination, five repetitions alternating query and index order. Each
copy was validated and rolled back before the next trial. Median statement
latencies in ms, excluding BEGIN/ROLLBACK, setup, and checks:

| Source log rows | With `_bb_commit_id`: old OR → new | Without index: old OR → new |
| --- | ---: | ---: |
| 10 | 1.45 → 2.67 | 2.10 → 2.64 |
| 1,000 | 32.85 → 2.86 | 29.07 → 2.87 |
| 10,000 | 2723.27 → 17.43 | 2735.97 → 10.98 |

The indexed 10,000-row copy is about 156 times faster in this local test;
the 10-row copy costs about 1 ms more. These are log-copy statement timings,
not whole-MERGE timings or a general throughput guarantee. Client statement
counts and native/legacy path reporting are unchanged.

The same experiment separately measures `_find_commit` hits/misses and an
`_add_commit` log-row insert with the schema already supplied and snapshot
creation excluded. At 10,000 source / 9,000 destination rows, indexed versus
unindexed medians were 1.67 / 1.32 ms for a lookup hit, 1.21 / 1.07 ms for a
miss, and 1.70 / 1.07 ms for an insert. At 1,000 rows the index reduced lookup
times; these small samples show no consistent benefit. The index is
retained pending larger workloads and clone/storage measurements.

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_commit_log \
  --rows 10 1000 10000 --reps 5 --out /tmp/mo-commit-log.json
```

The JSON records server identity, adapter revision/dirty state/source hash,
raw timings, and both EXPLAIN plans. It retains the former SQL explicitly for
a same-server comparison. Live regression coverage checks independent `seq`
and `id` collisions, non-unique source IDs, excluded log kinds, inclusive
cutoffs (including zero), every copied field, repeat copies, fresh history,
and rollback. All 108 MatrixOne adapter tests pass, including the live tests.
This step changes only the adapter.

The operation profiler also measures the log-copy change inside a complete
MERGE. `--history-rows` adds synthetic old commit rows just before MERGE, with
90% already present on the destination. Their sequence numbers precede all
real commits, and they reuse the real base commit's snapshot and schema. This
models larger logs; it does not model thousands of distinct snapshots or data
versions. Source/destination log sizes include the workflow's existing rows
as well: a 10,000-row seed produces 10,004 source and 9,003 destination rows
before merging. The `_bb_commit_id` index remains enabled.

`--log-copy legacy_or adapter` changes only the benchmark suite's log-copy
implementation during MERGE. `legacy_or` runs the previous SQL, while `adapter`
calls the current `_copy_log`. This option is separate from `--modes`, which
selects the native MERGE interface. Every trial uses fresh scratch databases
and checks conflicts, copied history, merged data, revert, and reset. Seeding
and checks are outside measurements. The whole adapter MERGE, including log
copying, recording the merge commit, and creating its snapshot, is timed.

Measured on 2026-10-25 with server `d33aa9d38`, adapter `3389618`, and this
profiler extension. The JSON records a dirty tree for the profiler change,
plus source hashes. The adapter implementation itself is unchanged. Each
table has 1,000 rows; the source updates 100 and inserts one, the destination
updates 50, and conflict policy is `ours`. Three repetitions per combination
alternate the old/new order. Database-level MERGE medians in ms:

| Data tables | Added source log rows | Complete MERGE: old → new | Log-copy phase: old → new | Whole MERGE speedup |
| --- | ---: | ---: | ---: | ---: |
| 1 | 0 | 131 → 128 | 6.3 → 7.8 | 1.02× |
| 1 | 1,000 | 157 → 125 | 55.4 → 9.8 | 1.26× |
| 1 | 10,000 | 2944 → 180 | 2787.7 → 28.9 | 16.32× |
| 10 | 0 | 276 → 261 | 7.0 → 7.9 | 1.06× |
| 10 | 1,000 | 287 → 285 | 35.5 → 9.5 | 1.01× |
| 10 | 10,000 | 3023 → 343 | 2710.0 → 40.0 | 8.81× |

The log-copy phase here includes normal autocommit and the workflow's real
schema payloads. The earlier statement microbenchmark used `{}` schema
payloads inside an explicit transaction, excluding its BEGIN/ROLLBACK.

The earlier 156× figure is a log-statement microbenchmark result. The complete
MERGE improves about 16× with one table and 9× with ten tables in this
10,000-row-history experiment. Short-history results show no dependable
end-to-end improvement: local timing variation can absorb the smaller log-copy
savings, as the ten-table / 1,000-row case illustrates. These are small local
samples, not general latency guarantees.

At 10,000 history rows, the remainder of MERGE stayed roughly constant:
157 → 157 ms with one table and 308 → 304 ms with ten. In the ten-table result,
the server's native MERGE was 160 → 166 ms; catalog and post-merge fingerprint
queries still account for roughly 74 ms after the change. All database-level
trials issued 14 client statements. `client_merge_timing` records `total_ms`,
`log_copy_ms`, `other_ms`, and `log_copy_calls` per run. The two phase values
sum to the total for each run; separately calculated medians need not add up.
Server phase timings are included in `other_ms`, so they must not be added
again to the client total.

The same 10-table / 10,000-history-row workload also improves with the other
MERGE interfaces (three repetitions each, medians in ms):

| MERGE interface | Complete MERGE: old → new | Whole MERGE speedup | Client SQL: old → new |
| --- | ---: | ---: | ---: |
| Legacy | 3576 → 886 | 4.04× | 63 → 63 |
| Table summary | 3039 → 384 | 7.91× | 23 → 23 |
| Database summary | 3023 → 343 | 8.81× | 14 → 14 |

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_operations \
  --tables 1 10 --rows 1000 --changed 100 --history-rows 0 1000 10000 \
  --log-copy legacy_or adapter --modes database --reps 3 \
  --server-log /tmp/mo.json.log --out /tmp/mo-log-e2e.json
```

To reproduce the additional interface controls, use `--tables 10
--history-rows 10000 --modes legacy summary` with the same remaining options.
All 48 workflows passed data/conflict/history/revert/reset checks and had
complete server timing records. The 27 focused profiler tests passed, including
six live workflows crossing all three interfaces and both copy strategies.
Tests check history-sequence protection, unchanged SQL counts, actual strategy
selection, and exclusion of history seeding/validation from operation timings.
This step adds profiling and test coverage; the production adapter and server
implementations remain those from the preceding step.

The adapter now reads both endpoints' latest fork/rebase/root log rows with
one `UNION ALL` statement. Each endpoint has its own `ORDER BY seq DESC LIMIT 1`
subquery, and an input-position tag routes the results independently of UNION
execution order. Missing fork points still raise an error naming that endpoint;
server failures propagate. Relationship selection still uses the newest
matching fork/rebase row and recognizes earlier generations of a branch.

MERGE's fork-point reads fall from two statements to one. Rebase reuses those
same rows to obtain its own base, reducing three reads to one. Results are
local to that operation; a later call reads fresh rows. Catalog and post-merge
schema fingerprint queries retain their existing behavior.

For the operation profiler's standard workload, client MERGE counts are now:

| MERGE interface | 1 table: before → after | 10 tables: before → after |
| --- | ---: | ---: |
| Legacy | 18 → 17 | 63 → 62 |
| Table summary | 14 → 13 | 23 → 22 |
| Database summary | 14 → 13 | 14 → 13 |

Measured on 2026-10-25 against the same `d33aa9d38` server: clean adapter
`fe50d0d` first, then that revision with the fork-point change applied. The
after JSON records a dirty tree and the adapter source hash. Each version ran
three repetitions per interface/table count, alternating interface order,
with the standard 1,000-row / 100-source-change UPDATE workflow. Medians in ms:

| Interface / tables | Complete MERGE: before → after | Fork-point reads: before → after |
| --- | ---: | ---: |
| Legacy / 1 | 177 → 191 | 2.53 → 2.48 |
| Table summary / 1 | 138 → 135 | 2.13 → 1.75 |
| Database / 1 | 128 → 145 | 2.41 → 2.17 |
| Legacy / 10 | 795 → 834 | 2.40 → 2.49 |
| Table summary / 10 | 331 → 361 | 2.31 → 2.32 |
| Database / 10 | 273 → 262 | 2.33 → 2.41 |

The established benefit is one fewer client statement per MERGE and two fewer
per rebase. These small local samples do not establish a latency improvement:
fork-point reads remain around 2 ms and whole-operation medians move in both
directions. All 36 workflows pass merge/conflict/revert/reset checks, retain
their execution paths and returned-row counts, and have complete server
timings. Reproduce with the same server and each adapter revision separately:

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_operations \
  --tables 1 10 --rows 1000 --changed 100 --reps 3 \
  --modes legacy summary database --server-log /tmp/mo.json.log \
  --out /tmp/mo-fork-point-profile.json
```

All 129 MatrixOne adapter tests pass, including live rebase and subsequent
merge after a generation change, reverse-direction rebase, custom resolvers,
snapshot references, reset, and revert. New coverage checks shuffled UNION
results, repeated/case-alias inputs, quoted database names, missing fork
points, error propagation, latest-row selection, and reuse without caching
across operations. A traced rebase confirms one ancestry query returning two
rows. This step changes only the adapter.

In a separate traced run of the 10-table UPDATE workload, legacy merge emitted
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
RESET/REVERT likewise preflight the selected tables and apply their changes
in one transaction. REVERT and RESET tables without foreign-key dependencies
read delta subqueries directly; FK-related RESET tables retain staging.
Schema reconciliation and adapter commit-log
updates remain outside that data transaction. The server currently computes
restore differences using joins over snapshot endpoints, so reducing client
SQL does not imply constant server work or an incremental storage-level
restore. MO commit `31d7c8437` fixes snapshot read-view reuse so a query joining
two historical versions of the same table reads the correct version on each
side; the restore commands depend on this fix.

MO commit `2de2c4d43` on
[branchbench-opt](https://github.com/zuyu/matrixone/tree/branchbench-opt)
reduces RESET/REVERT work inside the server. CTAS affected-row counts identify
empty delta stages without additional queries. An empty delete stage skips
DELETE; an empty row stage skips UPDATE and INSERT. Unknown counts retain the
original DML. Keyless RESET still deletes current rows even when the historical
source is empty. All stages are materialized before any destination writes.
Cleanup drops at most 128 stages per statement; a failed batch preserves the
error and attempts individual cleanup before transaction rollback.

For tables with a primary key and a writable non-key column, the measured
restore-owned SQL counts are identical for RESET and REVERT:

| Changes being undone | Before: 1 table | After: 1 table | Before: 10 tables | After: 10 tables |
| --- | ---: | ---: | ---: | ---: |
| No changes | 7 | 3 | 70 | 21 |
| Insert only (restore deletes rows) | 7 | 4 | 70 | 31 |
| Delete only (restore reinstates rows) | 7 | 5 | 70 | 41 |
| Update only | 7 | 5 | 70 | 41 |
| Mixed insert/delete/update | 7 | 6 | 70 | 51 |

These counts cover CTAS, target DML, and stage DROP statements submitted by the
restore executor. They exclude nested CTAS INSERTs, catalog/authorization SQL,
and transaction control. Each native restore still takes one client request;
the adapter's metadata and commit bookkeeping are unchanged. Both CTAS queries
per primary-key table remain, so this does not remove snapshot joins or stage
creation costs.

A local comparison on 2026-10-25 used 1,000 rows per table, 100 affected rows
per mutation kind (300 for the mixed case), and three repetitions for each
operation/workload/table count. The baseline was `d33aa9d38` with only restore
work instrumentation/refactoring added, built as `d33aa9d38+restore-counts`;
its DML and per-stage cleanup were unchanged. The optimized binary identifies
itself as `2de2c4d43ba8a54d4a5bb83c9d02a11db2009364`. Both used the same
isolated port 16001, data directory, INFO logging, adapter source, and profiler.
All 120 native calls passed data, relation-ID, cleanup, and stage-count checks.

| Changes being undone, 10 tables | RESET before/after median (ms) | REVERT before/after median (ms) |
| --- | ---: | ---: |
| No changes | 432 / 334 | 343 / 277 |
| Insert only | 445 / 270 | 452 / 310 |
| Delete only | 429 / 321 | 568 / 358 |
| Update only | 522 / 363 | 520 / 412 |
| Mixed | 491 / 380 | 524 / 500 |

The ten-table medians improved by 1.05–1.65x in this sample. SQL reductions
were deterministic; latency varied substantially between repetitions. Server
versions ran in separate blocks, with operation/case order reversed on alternate
repetitions. One-table insert-only RESET did not improve (47 / 49 ms), and
ten-table mixed REVERT improved only slightly. These are local single-request
measurements, not throughput or tail-latency claims.

`scripts.profile_matrixone_restore` measures the single native request on
fresh scratch data, checking every resulting row, unchanged relation IDs,
absence of leaked stages, and CTAS row counts. REVERT cases also verify that
later changes outside the reversed delta survive. Use an exclusive local
server with INFO JSON logging and no log rotation:

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_restore \
  --tables 1 10 --rows 1000 --changed 100 --reps 3 \
  --server-log /path/to/mo.json.log --out /tmp/mo-restore.json
```

The JSON records server `version()`/`git_version()`, adapter commit/dirty
state, source hashes, raw client queries, and `server_restore_work` counts and
phase timings. The operation profiler also captures these records for its
full adapter RESET/REVERT calls. Missing or inconsistent instrumentation is
marked incomplete rather than reported as zero work. Work timings begin after
planning and end before transaction finish; `status=ok` describes that work,
not the eventual transaction commit. Client elapsed time includes the complete
native request.

Go branch/diff/snapshot regression tests and 154 Python adapter/profile/live
tests passed. Coverage includes unknown or stale affected-row counts, empty
keyless sources, key-only and composite primary keys, exact decimal/binary/NULL
values, explicit rollback, late constraint failures, cleanup errors and panics,
and cleanup batch boundaries. Final complete adapter workflows at 1 and 10
tables passed merge/conflict/restore checks and captured complete restore work
records. Client counts remain 11 for REVERT and 7 for RESET in that workload.

MO commit `4efa5a736` reuses the endpoint snapshots and table definitions
already read during RESET/REVERT catalog enumeration. The retained metadata
exists only during the current statement's planning; each later request loads
it again. Database and table snapshot scope checks and per-table authorization
remain in place. DIFF/MERGE enumeration retains only table names as before.

For ten selected ordinary tables, with no additional tables/views in the
catalog, enumeration plus restore plan construction now makes these calls:

| Metadata API calls | RESET before → after | REVERT before → after |
| --- | ---: | ---: |
| Database opens | 22 → 2 | 43 → 3 |
| Historical endpoint resolutions | 11 → 1 | 22 → 2 |
| Relation opens | 40 → 20 | 70 → 30 |

These are calls on the catalog/planning path, not SQL statement counts. They
exclude authorization and metadata reads inside generated SQL execution. The
former `getRelations` pairs contributed two database/relation opens and one
historical resolution per pair; RESET called it once per table, REVERT twice.
The new planner uses the enumerated definitions directly, including the
destination definition that REVERT previously opened twice.

The profilers retain `DataBranch-Restore-Metadata` records under
`server_restore_metadata`: database opens, historical resolutions, relation
opens, and catalog enumeration time. Older servers produce an empty list.
This timing excludes authorization, plan construction, stage/apply/cleanup,
and transaction finish; it must not be added to client elapsed time. The raw
restore profiler checks the new counts when present. The successful catalog
event is emitted before authorization and does not imply request success.

A separate 2026-10-25 comparison used the committed binaries `2de2c4d43`
and `4efa5a736`, the same isolated port/data directory and INFO logging, and
the restore profiler's 1/10-table, 1,000-row, 100-changed-row cases with three
repetitions. All 120 native calls passed row, relation-ID, cleanup and stage
checks. The baseline logged 10/20 `GetRelations-Done` events per ten-table
RESET/REVERT; the new build logged zero and its catalog counters matched the
table above in every sample. Restore-owned SQL counts and client requests
were unchanged from the preceding optimization.

| Changes being undone, 10 tables | RESET before/after median (ms) | REVERT before/after median (ms) |
| --- | ---: | ---: |
| No changes | 324 / 290 | 281 / 280 |
| Insert only | 267 / 314 | 341 / 306 |
| Delete only | 333 / 345 | 337 / 321 |
| Update only | 380 / 382 | 438 / 381 |
| Mixed | 391 / 420 | 450 / 442 |

This sample establishes fewer metadata calls, not a consistent end-to-end
speedup. One-table results were also mixed and mostly slower in the second
block. The versions ran in separate blocks, alternating operation/case order
within repetitions. Stage creation and cleanup still dominate and their
timings varied. The new catalog enumeration itself took a median 1.6 ms for
ten-table RESET and 2.6 ms for REVERT across the 15 samples per operation;
there is no corresponding isolated catalog timing in the baseline.

Go branch/diff/snapshot regressions cover metadata refresh between statements,
recreated snapshot names, late schema mismatches, wrong-database/table-only
snapshots, denied writes before any table changes, hidden indexes/views, and
metadata read failures. Python profiling tests check current-operation
attribution and keep catalog time separate from restore work. All 154 Python
adapter/profile/live tests passed on the built server. Complete 1/10-table
adapter workflows also passed and captured one catalog record per native
restore, with no repeated relation-pair resolution and unchanged client counts.

MO commit `5a1dfa610` targets the expensive staging cleanup statement itself.
RESET/REVERT now use `CREATE TEMPORARY TABLE ... AS SELECT` and bounded
`DROP TEMPORARY TABLE` batches through the background executor. This keeps
stages in the restore's owner transaction and avoids ordinary-table work such
as publication/view invalidation and role-privilege cleanup. Physical cleanup
is synchronous; it is not deferred to session close or background GC. The
existing temporary-alias journal follows transaction/statement rollback.

A fresh comparison against `4efa5a736` used the same 1/10-table, 1,000-row,
100-changed-row workloads and three repetitions per scenario, with INFO logging
on the exclusive local port 16001. All 120 native calls passed row, identity,
stage-count and cleanup checks. For ten tables, `stage_drop` is one statement
dropping 20 staging tables; its measured duration is therefore the latency of
that single background SQL call, including parsing, planning and execution.

| Changes undone, 10 tables | RESET DROP before/after (ms) | REVERT DROP before/after (ms) | RESET request before/after (ms) | REVERT request before/after (ms) |
| --- | ---: | ---: | ---: | ---: |
| No changes | 248 / 50 | 162 / 48 | 342 / 183 | 280 / 184 |
| Insert only | 184 / 50 | 154 / 68 | 292 / 178 | 295 / 170 |
| Delete only | 156 / 46 | 149 / 53 | 275 / 152 | 322 / 192 |
| Update only | 153 / 74 | 192 / 66 | 331 / 310 | 435 / 286 |
| Mixed | 184 / 46 | 183 / 49 | 442 / 287 | 496 / 280 |

These are medians. The ten-table cleanup statement improved by 2.05–4.93x;
complete native requests improved by 1.07–1.87x. Restore-owned SQL counts and
client request counts did not change. Stage creation still matters: update-only
RESET's CTAS phase rose from 74 to 112 ms in this sample, offsetting part of
the cleanup gain. One-table delete-only RESET did not improve overall (31 / 36
ms), despite faster cleanup. Versions ran in separate blocks, alternating
operation/case order across repetitions; this is not a throughput or tail-latency
result.

The complete adapter workflow was also measured against the same two builds
with `python -m scripts.profile_matrixone_operations --tables 1 10 --rows 1000 --changed
100 --reps 3 --modes database`, including adapter metadata and commit
bookkeeping. Here the optimized build ran first, then the baseline, each on
port 16001 with `--server-log` and `--out` supplied. All twelve workflows passed
their result checks and captured complete restore work records.

| Tables | Operation | Before median (ms) | After median (ms) | Speedup |
| ---: | --- | ---: | ---: | ---: |
| 1 | RESET | 82.2 | 84.6 | 0.97x |
| 1 | REVERT | 127.2 | 147.4 | 0.86x |
| 10 | RESET | 405.8 | 308.6 | 1.31x |
| 10 | REVERT | 519.9 | 391.9 | 1.33x |

Ten-table adapter latency fell by about 24%; single-table adapter latency did
not improve in this sample, even though its cleanup phase fell from about
21–23 ms to 5 ms. Client SQL counts remain seven for RESET and eleven for
REVERT. These three-repetition medians do not establish a general single-table
benefit or stable latency distribution.

Work records now include `staging: "temporary"`. The restore profiler prints
the staging path, `create_ms`, and `drop_ms` alongside total request latency.
Older work records retain their counts and are labeled `unreported` in console
output rather than having a staging path inferred. JSON still records the
server's exact `git_version()` and all phase timings.

Go branch/snapshot/temporary-table regressions and 155 Python adapter/profile/
live tests passed. New integration checks cover explicit COMMIT/ROLLBACK,
repeated late failures followed by successful restores on the same connection,
preservation of user-created temporary tables, dotted database names, and an
uncommitted destination database. They check that physical staging tables are
gone before transaction finish, including their generated temporary prefixes.

MO commit `99c014307` introduced an intermediate bounded-key path, superseded
by the direct subqueries in `3a3aba8fe` described below. At `99c014307`,
delete staging tables were removed for bounded integer-key
deltas in RESET/REVERT. A server-side SELECT probes one row beyond the smaller
of 128 rows and 512 key cells per table. If the delta fits, the server retains
the exact signed/unsigned integer literals and applies `DELETE ... WHERE key
IN (...)`; an empty result skips that DELETE. Composite integer keys are
supported, with matching key types required at every endpoint. Source-row
stages and unsupported key types retain the temporary-table path. Every delta
is still materialized before any target write, under the same owner transaction;
no keys are returned to the client.

An overflowing probe releases its result and falls back to CTAS, so this case
runs the delta query twice and adds one background SELECT per table. The row
limit bounds retained results, not the scan/join work required by the probe.
The fast path replaces a CTAS with a SELECT: restore-owned SQL counts stay the
same while fewer staging tables are created and dropped.

Restore work logs use schema version 2, adding the `delete_probe` SQL count and
timing, `inline_delete_tables` (including empty deltas), and
`fallback_delete_tables` (probe overflow). `empty_stages` and
`materialized_rows` now cover both in-memory and temporary-table deltas, without
counting overflow probes twice. The profilers retain support for version 1
logs, expose probe timings, and label missing path counters as `unreported`.

A fresh comparison of `5a1dfa610` and `99c014307` used the same exclusive
port 16001, INFO logging, 1,000 rows per table, and three repetitions. With
100 changed rows, all ten-table scenarios used the bounded path: 20 CTASs
became 10 CTASs plus 10 SELECTs, and the single cleanup statement dropped 10
tables instead of 20. Median native request latency was:

| Changes undone, 10 tables | RESET before/after (ms) | REVERT before/after (ms) |
| --- | ---: | ---: |
| No changes | 202.3 / 143.4 | 215.8 / 114.2 |
| Insert only | 179.4 / 130.9 | 197.3 / 111.8 |
| Delete only | 185.8 / 135.1 | 194.0 / 149.7 |
| Update only | 268.0 / 203.2 | 274.5 / 226.9 |
| Mixed | 290.1 / 231.4 | 304.5 / 215.0 |

The ten-table CTAS phase fell from 77–120 ms to 42–62 ms across these cases;
the new probe phase cost 7–10 ms. Complete native requests improved by
1.21–1.89x. For example, update-only RESET's stage creation fell from 90.6 to
54.0 ms and cleanup from 76.0 to 24.4 ms, with 8.5 ms of probes. Its 41
restore-owned SQL calls and one client request did not change.

The full adapter comparison, including metadata and commit bookkeeping, used
the same operation-profiler command as above:

| Tables | Operation | Before median (ms) | After median (ms) |
| ---: | --- | ---: | ---: |
| 1 | RESET | 74.8 | 77.1 |
| 1 | REVERT | 146.2 | 106.1 |
| 10 | RESET | 291.4 | 249.1 |
| 10 | REVERT | 341.0 | 283.6 |

Ten-table adapter latency fell by 14.5% for RESET and 16.8% for REVERT, with
client SQL counts still seven and eleven respectively. One-table results are
mixed: adapter RESET did not improve, and native update-only RESET rose from
25.7 to 30.0 ms even though most other single-table cases were faster.

A second native run used `--tables 10 --changed 300`. Insert-only and mixed
cases then overflowed the delete-key probe at every table, adding ten SELECTs
per request. Their medians show the cost and variance of the fallback:

| Changes undone, 10 tables | RESET before/after (ms) | REVERT before/after (ms) | SQL calls before/after |
| --- | ---: | ---: | ---: |
| Insert only | 218.8 / 205.6 | 195.9 / 218.1 | 31 / 41 |
| Mixed | 557.7 / 445.9 | 550.7 / 579.2 | 51 / 61 |

The extra probe phase itself cost 6.5–10.1 ms per ten-table request. Other
phases varied too; these results do not establish a benefit for overflowing
deltas. In particular, the two REVERT fallback cases slowed by 11.3% and 5.2%.
All 180 native calls across both versions and both workload sizes passed row,
identity, materialization-count and cleanup checks; all twelve adapter
workflows passed. The baseline ran first and the optimized build second,
alternating native operation/case order between repetitions. These are local
three-sample medians, not throughput or tail-latency claims.

The server was built from the recorded commit. Go branch/snapshot/temporary-
table regressions and 165 Python adapter/profile/live tests passed. New tests
cover empty pipelines, signed/unsigned integer extremes, composite keys,
128/129-row and 512-cell boundaries, overflow fallback, cancellation, failure
cleanup, and explicit transaction rollback while preserving relation IDs.

MO commit `3a3aba8fe` replaces restore's bounded `IN (...)` key lists with
`DELETE ... JOIN (SELECT ...)`, keeping the delta query inside the SQL plan:

```sql
DELETE d FROM target.t d
JOIN (
    SELECT a.id
    FROM source.t{snapshot='after'} a
    LEFT JOIN source.t{snapshot='before'} b ON a.id = b.id
    WHERE b.id IS NULL
) s ON d.id = s.id;
```

The example is REVERT's delete delta; generated SQL uses the resolved snapshot
timestamps. RESET compares the current destination with the historical source
in the same derived-table form. This removes the separate probe, integer-only
restriction, literal serialization, 128-row/512-cell threshold, and overflow
CTAS replay. The derived-table boundary is supported by the existing DELETE
planner, including reads of the target's current and historical versions.

At this commit, all row deltas were still staged before any target write;
the next optimization below removes that staging for eligible tables. For RESET tables
with foreign keys or referencing child tables, delete keys are staged too,
preserving their pre-write set across earlier cascades. REVERT delete queries
read immutable snapshots and can use the subquery even with foreign keys.
Keyless RESET continues to replace contents while retaining table identity.
The transaction owner, rollback, and synchronous stage cleanup are unchanged.

This commit's restore work logs use schema version 3. `subquery_delete_tables` counts
issued subquery DELETEs; `staged_delete_tables` counts successfully created
delete stages. The separate `delete_probe` phase is gone; subquery execution
is included in `delete`. `empty_stages` and `materialized_rows` describe actual
temporary stages, excluding delete rows read inside the subquery. Profile
validation handles this distinction and continues to accept versions 1 and 2.

A fresh comparison against `99c014307` used the same native restore and full
adapter commands, with INFO logging on exclusive port 16001, 1,000 rows per
table and three repetitions. All 180 native calls and twelve adapter workflows
passed their correctness checks. Every primary-key table in these fixtures
used the subquery path, including the 300-row cases that previously overflowed.

| Changes undone, 10 tables, 100 changed rows | RESET before/after (ms) | REVERT before/after (ms) | Internal SQL before/after |
| --- | ---: | ---: | ---: |
| No changes | 114.1 / 99.8 | 123.9 / 123.0 | 21 / 21 |
| Insert only | 142.5 / 139.1 | 146.6 / 131.6 | 31 / 21 |
| Delete only | 100.3 / 134.4 | 141.0 / 181.1 | 41 / 41 |
| Update only | 222.5 / 254.3 | 238.9 / 206.6 | 41 / 41 |
| Mixed | 265.1 / 255.6 | 230.8 / 232.3 | 51 / 41 |

| Changes undone, 10 tables, 300 changed rows | RESET before/after (ms) | REVERT before/after (ms) | Internal SQL before/after |
| --- | ---: | ---: | ---: |
| No changes | 112.3 / 144.3 | 98.6 / 155.2 | 21 / 21 |
| Insert only | 168.3 / 148.8 | 216.0 / 133.0 | 41 / 21 |
| Delete only | 149.2 / 152.8 | 130.0 / 132.3 | 41 / 41 |
| Update only | 272.0 / 357.0 | 312.1 / 351.4 | 41 / 41 |
| Mixed | 524.1 / 499.0 | 573.8 / 542.4 | 61 / 41 |

Internal SQL counts include stage/apply/cleanup calls, excluding nested CTAS
INSERTs and catalog/transaction SQL; each native operation still uses one
client request. For 300-row insert-only REVERT, CTAS time fell from 111.6 to
51.9 ms and cleanup from 47.1 to 24.8 ms; the former 8.8 ms probe disappeared,
while DELETE itself rose from 9.1 to 13.9 ms. Unlike the previous probe path,
an empty delete delta still executes a DELETE plan. Eliminating probes and
literal lists therefore does not guarantee lower latency for empty/small deltas.

| Full adapter workflow | Before median (ms) | After median (ms) |
| --- | ---: | ---: |
| 1 table RESET | 58.0 | 74.1 |
| 1 table REVERT | 121.9 | 125.3 |
| 10 tables RESET | 235.7 | 224.6 |
| 10 tables REVERT | 257.0 | 262.5 |

The full adapter's ten-table RESET improved by 4.7%; REVERT was 2.1% slower.
Client counts remain seven and eleven. The larger overflow penalty is removed,
but the latency results are mixed, with variation in unchanged CTAS/cleanup
phases too. Versions ran in separate blocks, baseline first, and native case
order alternated between repetitions. These local three-sample medians do not
establish an overall or tail-latency improvement.

The recorded server commit was built and passed Go branch/snapshot/temporary-
table regressions plus 175 Python adapter/profile/live tests. Coverage includes
integer extremes, decimal and composite keys, 300-row composite deltas,
historical/current read views, explicit rollback, parent/child cascades, and
rollback of an earlier subquery DELETE after a later unique-constraint failure.

MO commit `3b6502605` on
[branchbench-opt](https://github.com/zuyu/matrixone/tree/branchbench-opt)
also removes row staging for REVERT and for RESET tables without foreign-key
dependencies. At this commit, UPDATE joins the delta subquery directly, and
INSERT reads the same subquery with an anti-join against the current destination. No CTAS,
stage probe, or stage cleanup is issued for these tables. The derived queries
retain typed keys, including composite and decimal keys, without literal lists.

REVERT reads immutable before/after snapshots. RESET's delta also reads the
current target: DELETE removes keys absent from the source snapshot, UPDATE
restores matching rows, and INSERT re-evaluates the delta to fill remaining
missing keys. This is safe for tables without FK dependencies. FK-related
RESET tables still materialize both deltas before any table is modified, so
earlier cascades cannot change their intended restore set. REVERT uses direct
subqueries even with foreign keys; keyless RESET inserts the full historical
rows after a row DELETE, preserving duplicates and relation identity. Keyless
REVERT remains unsupported.

Work log schema version 4 adds `subquery_row_tables` (tables for which a direct
UPDATE or INSERT was issued) and `staged_row_tables` (successful row CTASs).
`staging: "none"` means no stage creation was attempted; `"temporary"` means
at least one was attempted, including failed work. Existing delete path counts
remain, and create/drop counts and times are explicitly zero for stage-free
work. `materialized_rows: 0` describes physical staging, not a zero-row delta.
Both profilers validate these counters; versions 1–3 remain supported.

For an ordinary primary-key table with writable non-key columns, the new path
issues three internal DML statements even when the delta is empty. Ten-table
no-op/insert-only work therefore changes from 21 calls to 30, while
delete/update/mixed work changes from 41 to 30. This counts restore-owned
stage/apply/cleanup calls, excluding nested CTAS INSERTs and catalog/transaction
SQL. The native client request count is still one. Direct queries can scan
snapshot data twice for UPDATE and INSERT; removing staging is not a guarantee
of lower latency for every table size or delta.

The next commit, `208cd3224`, simplifies INSERT while keeping the same
stage-free execution and statement counts. RESET reads historical rows missing
from the current target directly; it no longer computes the full update delta
and joins the target a second time. REVERT first selects before-images absent
from the current target, then compares only those candidates with the after
snapshot. That comparison is still necessary: a row unchanged by the reverted
commit but deleted later must not be resurrected. The transaction, UPDATE,
DELETE, and FK-related RESET staging behavior are unchanged by this follow-up.

A three-build comparison used `3a3aba8fe` (row stages), `3b6502605` (direct
row subqueries), and `208cd3224` (simplified INSERT), in that order on exclusive
port 16001 with INFO JSON logging. Each build ran 102 native restore calls and
six full adapter workflows. All 306 native calls validated every restored row,
relation identity, and work counters; all eighteen adapter workflows validated
their data. Runs used three repetitions, alternating native case order within
repetitions, with no concurrent test or build workload. Values below are local
medians, not throughput or tail-latency results.
Changed counts apply per mutation category: the mixed 100-row case combines
100 inserts, 100 deletes, and 100 updates per table.

| Changes undone, 10 tables × 1,000 rows, 100 changed | RESET staged / direct / final (ms) | REVERT staged / direct / final (ms) |
| --- | ---: | ---: |
| No changes | 121.1 / 103.6 / 73.1 | 123.1 / 91.0 / 90.9 |
| Insert only | 131.6 / 111.6 / 83.1 | 118.9 / 109.5 / 96.0 |
| Delete only | 128.9 / 202.0 / 246.2 | 144.4 / 203.5 / 183.2 |
| Update only | 206.6 / 156.6 / 149.7 | 187.1 / 170.4 / 142.9 |
| Mixed | 233.6 / 246.3 / 331.2 | 254.3 / 265.4 / 271.3 |

| Changes undone, 10 tables × 1,000 rows, 300 changed | RESET staged / final (ms) | REVERT staged / final (ms) |
| --- | ---: | ---: |
| No changes | 140.0 / 74.8 | 113.9 / 103.2 |
| Insert only | 141.0 / 106.8 | 145.5 / 127.2 |
| Delete only | 140.6 / 234.8 | 119.0 / 180.0 |
| Update only | 354.5 / 242.7 | 407.5 / 295.8 |
| Mixed | 453.0 / 542.0 | 436.6 / 490.9 |

Removing stages helped no-op, insert-only, and update-only ten-table requests
in this sample, but did not improve every workload. In the 100-row delete-only
RESET case, CTAS and DROP medians fell from 45.6 and 24.3 ms to zero, while
INSERT rose from 9.8 ms to 143.0 ms in the first direct version and 187.4 ms in
the final version. The INSERT simplification did not fix this regression.
By contrast, update-only RESET's INSERT fell from 47.0 to 30.3 ms between the
two stage-free versions. These phase medians are independent and need not sum
to the median request latency.

The profiler now accepts `--mutations`, allowing a larger-table check without
running unrelated cases. One table with 100,000 rows and 100 changed rows
(`mixed` changes 100 rows in each mutation category) exposed additional DML
cost; the diagnosis below identifies an unintended bulk-restore planning mode:

| Large-table request | Staged / direct / final median (ms) |
| --- | ---: |
| RESET update | 135.0 / 140.8 / 148.5 |
| REVERT update | 150.3 / 174.6 / 166.8 |
| RESET mixed | 147.2 / 194.4 / 153.9 |
| REVERT mixed | 130.5 / 169.0 / 184.7 |

| Full adapter workflow, 1,000 rows, 100 updates | Staged / direct / final median (ms) |
| --- | ---: |
| 1 table RESET | 73.8 / 54.6 / 74.7 |
| 1 table REVERT | 140.9 / 124.1 / 122.6 |
| 10 tables RESET | 239.1 / 186.0 / 149.2 |
| 10 tables REVERT | 274.7 / 241.0 / 250.4 |

The final ten-table adapter RESET is 37.6% faster than the staged baseline and
REVERT is 8.8% faster; client counts remain seven and eleven. These gains do
not establish that stage-free execution is universally faster. At `208cd3224`,
deleted-row restoration, mixed changes, and large-table sparse deltas still
needed work on the DML execution plans. FK-related RESET staging remains.

For each server build, start a separate exclusive INFO JSON log and run:

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_restore --tables 1 10 \
  --rows 1000 --changed 100 --reps 3 --server-log /tmp/mo.log --out /tmp/restore-small.json
MO_PORT=16001 python -m scripts.profile_matrixone_restore --tables 10 \
  --rows 1000 --changed 300 --reps 3 --server-log /tmp/mo.log --out /tmp/restore-large.json
MO_PORT=16001 python -m scripts.profile_matrixone_restore --tables 1 \
  --rows 100000 --changed 100 --mutations update mixed --reps 3 \
  --server-log /tmp/mo.log --out /tmp/restore-sparse.json
MO_PORT=16001 python -m scripts.profile_matrixone_operations --tables 1 10 \
  --rows 1000 --changed 100 --reps 3 --modes database \
  --server-log /tmp/mo.log --out /tmp/restore-adapter.json
```

The final server was built from its recorded commit. Go branch, snapshot,
temporary-table, and SQL integration regressions plus 193 Python
adapter/profile/live tests passed. New cases cover
re-evaluation after UPDATE, missing or resurrected current rows, preserving
later deletions of unchanged rows, NULLs, parent/child cascades, repeated calls,
explicit rollback, and failure/cancellation/panic without stage cleanup.

Local MO commit `7a48eccfd` on
[branchbench-opt](https://github.com/zuyu/matrixone/tree/branchbench-opt)
fixes the planning mode behind the INSERT regression above. The background
executor inferred bulk recovery from an INSERT containing `MO_TS`. Native
RESET/REVERT use that annotation to read resolved snapshot timestamps, so
their ordinary INSERTs inherited bulk recovery's `DefaultHugeStats()` estimates
(100 million rows per scan) and skipped normal duplicate-check planning.

Branch restore now disables that implicit classification on its own executor.
The option applies to both private transactions and the shared executor used
inside `BEGIN`. Explicit `ExecRestore` calls and other background callers keep
their existing recovery behavior. Generated DML, snapshot endpoints, SQL
counts, and stage eligibility are unchanged: ordinary keyed tables still use
three internal statements per table and no staging; FK-related RESET tables
retain pre-cascade staging.

Regression coverage includes timestamp and comment classification, SQL modes,
explicit recovery after ordinary DML, and executor isolation. End-to-end cases
exercise an INSERT that conflicts with a unique index added since the snapshot:
the restore statement must reject it even inside `BEGIN`, and rollback must
restore the earlier table's changes. Successful restores after removing that
index still support explicit rollback and repeated execution.

A fresh comparison against `208cd3224` used the same commands above, extending
the 100,000-row run to `--mutations delete update mixed`. Each build ran 108
native restore calls and six full adapter workflows. All 216 native calls and
twelve adapter workflows passed their data checks; every fixture retained
`staging: "none"` and three internal DML calls per table. Each native-only
measurement still used one client request. INFO logs and JSON identify the
server commit and each restore path.
Builds ran in separate blocks, baseline first, without concurrent builds or
tests; the tables below report three-repetition medians.

| Changes undone, 10 tables × 1,000 rows, 100 changed per category | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| No changes | 77.3 / 52.6 | 94.7 / 62.2 |
| Insert only | 93.6 / 46.9 | 101.0 / 64.8 |
| Delete only | 227.4 / 71.7 | 202.8 / 49.8 |
| Update only | 154.5 / 131.2 | 157.9 / 125.6 |
| Mixed | 334.8 / 134.6 | 277.7 / 152.4 |

The targeted delete-only INSERT phase fell from 173.2 to 16.1 ms for RESET and
133.2 to 14.5 ms for REVERT: approximately 10.8x and 9.2x faster for that phase,
versus 3.17x and 4.07x for the complete native requests. An isolated five-repeat
single-table check inside `BEGIN` also reduced native RESET from 23.5 to 8.5 ms;
the equivalent three SQL statements were already about 7–8 ms in total.
Their ordinary EXPLAIN plans used an anti-join and estimates near the actual
table size, helping separate SQL shape from the background annotation issue.

| Changes undone, 10 tables × 1,000 rows, 300 changed per category | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| No changes | 80.9 / 64.5 | 85.2 / 43.9 |
| Insert only | 93.1 / 65.0 | 118.2 / 86.7 |
| Delete only | 241.3 / 68.3 | 192.4 / 80.5 |
| Update only | 286.3 / 242.5 | 288.1 / 266.3 |
| Mixed | 544.5 / 361.9 | 495.7 / 398.9 |

| One table × 100,000 rows, 100 changed per category | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| Delete only | 126.7 / 139.6 | 152.3 / 133.9 |
| Update only | 139.1 / 131.1 | 166.6 / 122.4 |
| Mixed | 188.1 / 165.0 | 172.3 / 176.4 |

Larger-table results remain mixed. Delete-only RESET was 10.2% slower and mixed
REVERT 2.4% slower in this sample. Normal DML also plans duplicate checks and
still reads historical/current rows; the small-table INSERT improvement does
not establish a general large-table or tail-latency speedup.

| Full adapter workflow, 1,000 rows, 100 updates | Before / after median (ms) |
| --- | ---: |
| 1 table RESET | 55.6 / 51.3 |
| 1 table REVERT | 93.8 / 110.2 |
| 10 tables RESET | 155.8 / 138.1 |
| 10 tables REVERT | 253.1 / 201.8 |

The full ten-table adapter improved 11.4% for RESET and 20.3% for REVERT, with
the same seven and eleven client SQL statements. One-table adapter REVERT did
not improve, despite its INSERT phase falling from 4.2 to 1.3 ms. Snapshot and
adapter bookkeeping remain part of the end-to-end cost.

The recorded server commit was built and passed Go branch, background restore,
clone, snapshot, temporary-table, planner, and SQL integration regressions plus
193 Python adapter/profile/live tests.

Local MO commit `d2549de4f` on
[branchbench-opt](https://github.com/zuyu/matrixone/tree/branchbench-opt)
reduces UPDATE work within stage-free RESET/REVERT. RESET now joins the current
target directly to the historical rows and tests their differences in that
join. The equivalent SQL's EXPLAIN shows two table scans instead of three:
one current scan and one historical scan. It retains the original null-safe
comparison and operand order. FK-related RESET retains its pre-cascade stages.

REVERT still derives its immutable before/after delta, but can filter candidates
already equal to their current rows before the UPDATE pipeline. This is enabled
only when all writable non-key columns have matching historical types and exact
equality semantics: booleans, integers, decimals, date/time types, and UUIDs.
Strings, floating point, complex types, and historical type changes retain the
original REVERT UPDATE. For example, a collated current value `alpha` must still
be assigned its exact before-image `Alpha`. A repeated old UPDATE already reports
zero affected rows; the measured saving is in candidate processing downstream.

`scripts.profile_matrixone_restore --already-restored` now performs and validates
one untimed restore before measuring a repeat. Each sample records
`already_restored` in JSON; validation and that first call are outside the timed
request. REVERT validation also checks that unrelated later changes survive.

```bash
MO_PORT=16001 python -m scripts.profile_matrixone_restore --tables 1 10 \
  --rows 1000 --changed 100 --mutations update mixed --already-restored --reps 3 \
  --server-log /tmp/mo.log --out /tmp/restore-repeat.json
```

A fresh comparison against `7a48eccfd` used the previous first-call commands
(including delete/update/mixed for 100,000 rows) plus this repeated-call command.
Each build ran 132 timed native calls, 24 untimed validated first calls, and six
full adapter workflows. All 264 timed native calls, 48 untimed calls, and twelve
adapter workflows passed their data checks. The integer fixtures still used one
client request, three internal DML calls per table, and zero stages. Builds ran
in separate blocks, baseline first, without concurrent builds or tests. Values
below are three-repetition medians; before/after always refers to these builds,
not to the first and repeated call.

| First call: 10 tables × 1,000 rows, 100 changed per category | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| No changes | 38.7 / 62.5 | 52.1 / 71.2 |
| Insert only | 76.7 / 59.2 | 96.2 / 68.9 |
| Delete only | 66.5 / 65.2 | 90.1 / 72.2 |
| Update only | 131.8 / 134.8 | 137.7 / 119.1 |
| Mixed | 124.8 / 147.5 | 180.8 / 170.8 |

| Further cases | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| First: 10 × 1,000 rows, 300 updates | 267.9 / 252.6 | 290.9 / 283.6 |
| First: 10 × 1,000 rows, 300 changes/category, mixed | 345.8 / 374.6 | 375.5 / 394.8 |
| First: 1 × 100,000 rows, 100 deletes | 131.0 / 150.2 | 128.5 / 179.3 |
| First: 1 × 100,000 rows, 100 updates | 169.8 / 97.4 | 134.3 / 140.7 |
| First: 1 × 100,000 rows, 100 changes/category, mixed | 178.1 / 132.1 | 154.7 / 166.4 |
| Repeat: 1 × 1,000 rows, 100 updates already undone | 13.5 / 13.8 | 23.8 / 16.9 |
| Repeat: 1 × 1,000 rows, mixed changes already undone | 14.9 / 8.9 | 29.7 / 11.3 |
| Repeat: 10 × 1,000 rows, 100 updates/table already undone | 37.1 / 46.0 | 164.5 / 72.7 |
| Repeat: 10 × 1,000 rows, mixed changes already undone | 48.8 / 51.5 | 223.9 / 75.8 |

| UPDATE phase only | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| First: 10 × 1,000 rows, 100 updates/table | 85.7 / 83.5 | 76.6 / 61.4 |
| First: 1 × 100,000 rows, 100 updates | 74.6 / 37.4 | 52.2 / 51.6 |
| Repeat: 10 × 1,000 rows, 100 updates/table already undone | 10.5 / 10.9 | 94.2 / 19.8 |

| Full adapter workflow, 1,000 rows/table, 100 updates/table | Before / after median (ms) |
| --- | ---: |
| 1 table RESET | 87.9 / 54.0 |
| 1 table REVERT | 134.5 / 104.1 |
| 10 tables RESET | 133.1 / 117.3 |
| 10 tables REVERT | 217.8 / 202.5 |

The full adapter retains seven RESET and eleven REVERT client statements.
The largest gains are in large-table RESET and repeated REVERT. First-call
results are mixed: ten-table mixed RESET was 18.2% slower, and 100,000-row
delete-only REVERT was 39.6% slower in the native-request comparison.

A follow-up control alternated the old/new SQL shapes on identical fixtures
inside `BEGIN`/`ROLLBACK`, seven repetitions on the final build. For 100,000 rows
and 100 updates, RESET's UPDATE fell from 54.0 to 34.6 ms. For 1,000-row mixed
changes, repeated REVERT's UPDATE fell from 8.1 to 1.8 ms. The control also
retained first-call REVERT regressions: delete-only UPDATE was 47.5 / 60.0 ms
at 100,000 rows, and an empty-delta UPDATE was 1.5 / 2.6 ms at 1,000 rows.
REVERT's plan still has three scans and now has an extra filter. This filter
benefits already-restored candidates but does not guarantee a first-call gain.

These local medians distinguish phase gains from end-to-end latency and repeated
calls from first calls; they do not establish a general or tail-latency speedup.
The recorded final server was built and passed Go branch, snapshot, planner,
background executor, and SQL integration regressions plus 193 Python
adapter/profile/live tests. New restore cases cover composite keys, both NULL
transition directions, already-restored candidates, unrelated later changes,
exact collated before-images, repeated execution, and explicit rollback.

Local MO commit `2bb7b1e42` on
[branchbench-opt](https://github.com/zuyu/matrixone/tree/branchbench-opt)
removes unnecessary target-row deduplication from eligible joined UPDATE plans.
The previous plan partitioned/sorted rows by physical row ID and evaluated
`row_number()` even when the joins could not duplicate a target row. This cost
remained after the earlier scan and equality-filter improvements.

The planner now traces physical row IDs and SQL-equality-compatible primary
keys through direct projections, filters, sorts, and key-preserving INNER/LEFT
joins. It skips the deduplication window only after proving that each updated
target row can occur at most once. The proof uses binding/column identities and
catalog key/type metadata, independently of estimated cardinalities. It covers
eligible ordinary single-target joined UPDATEs as well as branch restore SQL.
Unsupported projections or operators, nonunique sources, incomplete composite
keys, incompatible equality domains, nullable-side targets, and multi-target
updates retain their existing handling. Locks, NULL-target protection, affected
row accounting, uniqueness checks, and foreign-key behavior stay in place.

The generated branch DML and the earlier REVERT equality filter are unchanged.
Sixteen EXPLAIN ANALYZE checks (four old/current restore SQL shapes across four
fixtures) found no deduplication `Window` or `Partition` operators in the new
plans. Seven alternating repetitions per fixture also validated row counts and
values after the first and repeated UPDATE. The small mixed-change REVERT
UPDATE took about 1.8 ms; larger-table statements still pay for their scans and
joins, so this is not the complete native request's latency.

A fresh `d2549de4f` / `2bb7b1e42` comparison repeated the native and adapter
commands above, including `--already-restored` and the 100,000-row
delete/update/mixed cases. Per build: 132 timed native calls, 24 untimed validated
first calls, and six adapter workflows. All row/path checks passed. Native
fixtures retained one client request, three internal DML calls per table and
zero stages; adapter RESET/REVERT retained seven/eleven client statements.
Builds ran in separate baseline/after blocks without concurrent builds or tests.
These tables report three-repetition medians.

| First call: 10 tables × 1,000 rows, 100 changed per category | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| No changes | 61.8 / 61.3 | 74.5 / 73.8 |
| Insert only | 54.1 / 66.7 | 51.2 / 77.8 |
| Delete only | 55.3 / 53.5 | 77.9 / 50.7 |
| Update only | 135.3 / 43.5 | 113.8 / 78.8 |
| Mixed | 142.7 / 79.0 | 155.6 / 92.2 |

| Further cases | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| First: 10 × 1,000 rows, 300 updates/table | 237.1 / 54.3 | 261.6 / 58.1 |
| First: 10 × 1,000 rows, 300 changes/category, mixed | 405.6 / 196.1 | 358.1 / 161.8 |
| First: 1 × 100,000 rows, 100 deletes | 138.5 / 129.6 | 166.8 / 140.8 |
| First: 1 × 100,000 rows, 100 updates | 119.4 / 92.9 | 130.6 / 145.2 |
| First: 1 × 100,000 rows, 100 changes/category, mixed | 167.9 / 142.9 | 199.4 / 144.5 |
| Repeat: 10 × 1,000 rows, 100 updates/table already undone | 45.7 / 33.3 | 72.2 / 67.2 |
| Repeat: 10 × 1,000 rows, mixed changes already undone | 62.4 / 34.0 | 70.9 / 46.9 |

| UPDATE phase only: 10 tables × 1,000 rows | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| First: 100 updates/table | 81.1 / 10.2 | 64.0 / 20.8 |
| First: 300 updates/table | 180.3 / 12.0 | 197.1 / 15.5 |
| Repeat: 100 updates/table already undone | 9.7 / 7.8 | 18.7 / 16.6 |

| Full adapter workflow, 1,000 rows/table, 100 updates/table | Before / after median (ms) |
| --- | ---: |
| 1 table RESET | 66.1 / 52.3 |
| 1 table REVERT | 129.1 / 113.8 |
| 10 tables RESET | 122.6 / 119.2 |
| 10 tables REVERT | 217.1 / 176.5 |

With 300 updates per table, the complete ten-table native requests improved
4.37x for RESET and 4.50x for REVERT; their UPDATE phases improved about 15.0x
and 12.7x. The full adapter's ten-table REVERT improved 18.7%, while RESET
improved only 2.8% despite its UPDATE phase falling from 45.0 to 17.5 ms.
Snapshot and adapter bookkeeping remain substantial parts of the total.

The gains are workload-dependent. First-call REVERT with 100 updates in a
100,000-row table was 11.2% slower, and ten-table insert-only REVERT with 100
changes per table was 52.0% slower in this sample. Large-table scans/joins and
other restore phases remain; these three-repetition local medians do not
establish a universal or tail-latency improvement.

The recorded final server was built and passed the complete Go planner package,
relevant UPDATE/branch/snapshot/constraint/executor integrations, and 193 Python
adapter/profile/live tests. New tests verify actual fast/fallback plan shapes,
composite keys, duplicate-source row counts, NULL transitions, repeat and
rollback, unique-constraint failures, and protection for nullable outer targets.

Local MO commit `ac78f41f1` on
[branchbench-opt](https://github.com/zuyu/matrixone/tree/branchbench-opt)
admits selective runtime filters when a newly loaded table lacks useful
primary-key distinct-value statistics. Previously the planner substituted 100
for missing/degenerate NDV. In a 100,000-row restore, an estimated 5,010 delta
rows divided by that fallback rejected the filter, even when the actual delta
contained only 100 keys.

The fallback now uses the table row-count estimate for a complete,
SQL-equality-compatible single-column primary key on an ordinary INNER join's
table-scan probe. It requires a finite, nondefault row-count estimate above 100,
exact column/binding/type identity, and safe scan predicates. Known NDV retains
the existing cost gate. Other join types, partial composite keys, unsupported
key domains, unsafe predicates, and unavailable table statistics retain the
old fallback. Runtime key encoding, payload limits, the disable hint, and
fallback when the actual key set exceeds the limit are unchanged.

Branch SQL is unchanged: the restore queries still use subqueries. The engine
builds the optional filter from their results. EXPLAIN ANALYZE on the final
build showed the current-table scan for a 100,000-row/100-update REVERT reading
100 rows instead of 100,000, and the outer join consuming 200 rows instead of
100,100. The two historical scans still read 100,000 rows each. This is a 1,000x
reduction in one scan's input rows, not a 1,000x latency improvement.

A fresh `2bb7b1e42` / `ac78f41f1` comparison used the same native and adapter
commands above: per build, 132 timed native calls, 24 untimed validated first
calls, and six adapter workflows. All result/path checks passed. The fixtures
retained one native client request, three internal DML calls per table and no
stages; full adapter RESET/REVERT retained seven/eleven client statements.
Builds ran in separate baseline/after blocks without concurrent builds or
tests. The following numbers are three-repetition medians.

| First call: 1 table × 100,000 rows, 100 changed per category | RESET before / after (ms) | REVERT before / after (ms) | REVERT UPDATE phase before / after (ms) |
| --- | ---: | ---: | ---: |
| Delete only | 129.3 / 126.1 | 154.5 / 143.8 | 68.8 / 36.4 |
| Update only | 112.4 / 111.2 | 124.4 / 109.9 | 44.3 / 36.1 |
| Mixed | 118.6 / 148.3 | 163.4 / 142.8 | 53.5 / 32.6 |

| First call: 10 tables × 1,000 rows, 100 changed per category | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| No changes | 36.7 / 49.2 | 44.2 / 67.6 |
| Insert only | 42.8 / 64.2 | 49.9 / 77.3 |
| Delete only | 48.6 / 38.7 | 77.8 / 75.6 |
| Update only | 58.0 / 68.5 | 49.3 / 78.5 |
| Mixed | 68.8 / 53.3 | 76.4 / 75.0 |

| Further cases: 10 tables × 1,000 rows | RESET before / after (ms) | REVERT before / after (ms) |
| --- | ---: | ---: |
| First: 300 deletes/table | 41.6 / 69.7 | 85.6 / 84.7 |
| First: 300 updates/table | 59.1 / 73.6 | 84.8 / 58.5 |
| First: 300 changes/category, mixed | 157.5 / 174.5 | 179.1 / 175.6 |
| Repeat: 100 updates/table already undone | 60.9 / 67.7 | 70.6 / 46.4 |
| Repeat: mixed changes already undone | 41.3 / 33.7 | 65.6 / 45.1 |

| Full adapter workflow, 1,000 rows/table, 100 updates/table | Before / after median (ms) |
| --- | ---: |
| 1 table RESET | 59.8 / 46.1 |
| 1 table REVERT | 128.4 / 133.0 |
| 10 tables RESET | 93.5 / 102.8 |
| 10 tables REVERT | 151.4 / 172.6 |

Large-table native REVERT improved about 7–13% overall in this sample; its
UPDATE phase improved about 18–47%. These gains do not extend consistently to
small tables or the full adapter. Ten-table adapter REVERT was 14.0% slower,
and 100,000-row mixed RESET was 25.0% slower. These results remain part of the
comparison rather than being inferred from the reduced scan count.

A follow-up control alternated `optimizer_hints='runtimeFilter=1'` (disabled)
and `runtimeFilter=0` on the final isolated server, restoring the original hint
afterward. It used identical data and SQL inside `BEGIN`/`ROLLBACK`, seven
repetitions, and checked affected rows and values after first/repeated UPDATEs.
Plans verified the enabled/disabled filter. These are UPDATE-statement times,
not complete native requests:

| REVERT UPDATE: 100,000 rows, 100 changed per category | First, disabled / enabled (ms) | Repeat, disabled / enabled (ms) |
| --- | ---: | ---: |
| Delete only | 59.7 / 50.5 | 56.4 / 49.7 |
| Update only | 54.5 / 53.1 | 55.7 / 34.2 |
| Mixed | 62.2 / 52.8 | 55.0 / 34.1 |

Small-table controls did not show a consistent gain: 1,000-row update-only
REVERT was 3.08 / 3.10 ms on the first call; repeated mixed REVERT was
1.72 / 2.86 ms. RESET plans without a runtime filter also varied in latency.
The control supports reduced work for sparse large-table REVERT, but these
local medians do not establish a general or tail-latency speedup.

The final committed server was built and passed the complete Go planner and
hash-build packages, relevant frontend/branch/snapshot/UPDATE/constraint/compile
and SQL integration regressions, and 193 Python adapter/profile/live tests.
New cases cover actual filter admission and rejection, missing/known/default
statistics, key/type/binding guards, disabled hints, nonselective builds,
first/repeated restore, unrelated later changes, empty build input, exact
affected counts, and rollback. Existing hash-build tests cover payload limits
and fallback when actual key counts exceed them.

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

