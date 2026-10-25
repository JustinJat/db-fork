"""Observed paths survive operation boundaries, failures, and result export."""

import pyarrow.parquet as pq
import pytest

from dblib import result_pb2 as rslt
from dblib.result_collector import ResultCollector
from tests.fake_backend import FakeSuite


def test_paths_are_scoped_to_each_verb_and_keep_failed_attempts(tmp_path):
    collector = ResultCollector(output_dir=str(tmp_path), spill_rows=2)
    suite = FakeSuite(collector)

    def merge(*args, **kwargs):
        suite._record_execution("legacy_table_merge", fallback_reason="table_merge_summary: rejected")
        suite._record_execution("legacy_table_merge")
        # A nested operation must not overwrite its parent's details.
        assert suite.commit("main", "nested", timed=False).execution_paths == []
        raise RuntimeError("apply failed")

    suite._merge_impl = merge
    failed = suite.merge("main", "other")
    assert failed.failed
    assert failed.execution_paths == ["legacy_table_merge"]
    assert failed.fallback_reasons == ["table_merge_summary: rejected"]
    assert suite.commit("main", "next").execution_paths == []
    # Spilled rows still contribute to the report, with failure distinguished.
    expected = {"MERGE": {"legacy_table_merge": {"ok": 0, "failed": 1, "unsupported": 0}}}
    assert collector.support_summary()["execution_path_counts"] == expected
    collector.write_to_parquet("paths.parquet", append=False)
    rows = pq.read_table(tmp_path / "paths.parquet").to_pylist()
    assert rows[0]["execution_paths"] == ["legacy_table_merge"]
    assert rows[0]["fallback_reasons"] == ["table_merge_summary: rejected"]
    assert rows[1]["execution_paths"] == []
    assert collector.execution_summary()["execution_path_counts"] == expected
    collector.reset()
    assert collector.execution_summary() == {"execution_path_counts": {}, "fallback_reason_counts": {}}
    suite.close_connection()


def test_untimed_and_warmup_operations_do_not_inflate_path_counts(tmp_path):
    collector = ResultCollector(output_dir=str(tmp_path))
    suite = FakeSuite(collector)
    suite._merge_impl = lambda *args, **kw: suite._record_execution("legacy_table_merge")
    assert suite.merge("main", "other", timed=False).execution_paths == ["legacy_table_merge"]
    collector.set_recording(False)
    suite.merge("main", "other")
    collector.set_recording(True)
    assert collector.execution_summary()["execution_path_counts"] == {}
    suite.merge("main", "other")
    assert collector.execution_summary()["execution_path_counts"]["MERGE"]["legacy_table_merge"]["ok"] == 1
    suite.close_connection()


@pytest.mark.parametrize("spill_rows", [0, 1])
def test_append_to_older_parquet_without_path_columns(tmp_path, spill_rows):
    collector = ResultCollector(output_dir=str(tmp_path), spill_rows=spill_rows)
    collector.emit(rslt.MERGE)
    collector.write_to_parquet("old.parquet", append=False)
    old = pq.read_table(tmp_path / "old.parquet").drop(["execution_paths", "fallback_reasons"])
    pq.write_table(old, tmp_path / "old.parquet")
    collector.emit(rslt.MERGE, execution_paths=["table_merge_summary"])
    collector.write_to_parquet("old.parquet")
    rows = pq.read_table(tmp_path / "old.parquet").to_pylist()
    assert len(rows) == 2
    assert rows[0]["execution_paths"] is None
    assert rows[1]["execution_paths"] == ["table_merge_summary"]
