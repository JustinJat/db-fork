"""Negotiation must never replay a merge after an ambiguous failure."""

from unittest.mock import Mock

import pymysql
import pytest

from dblib.matrixone import MatrixOneToolSuite


def suite():
    s = object.__new__(MatrixOneToolSuite)
    s._merge_summary_supported = None
    return s


def test_native_merge_returns_counts_in_one_statement():
    s = suite()
    s._execute = Mock(return_value=[(2, 1, 3, 4, 5, 6, 2)])
    assert s._merge_with_summary('`s`.`t`', '`d`.`t`', 'SKIP') == ((2, 1, 3), (4, 5, 6), 2)
    assert s._merge_summary_supported is True
    s._execute.assert_called_once_with(
        'DATA BRANCH MERGE `s`.`t` INTO `d`.`t` WHEN CONFLICT SKIP OUTPUT SUMMARY')


def test_parser_rejection_disables_probe_for_connection():
    s = suite()
    s._execute = Mock(side_effect=pymysql.ProgrammingError(
        1064, 'syntax error at line 1 column 50 near " OUTPUT SUMMARY";'))
    assert s._merge_with_summary('s', 'd', 'SKIP') is None
    assert s._merge_with_summary('s', 'd', 'SKIP') is None
    assert s._execute.call_count == 1
    assert s._merge_summary_supported is False


@pytest.mark.parametrize('error', [
    pymysql.OperationalError(1064, 'table d does not exist'),
    pymysql.OperationalError(2013, 'Lost connection during query'),
    pymysql.ProgrammingError(1064, 'syntax error at line 1 column 20 near "bad";'),
    pymysql.OperationalError(1105, 'apply failed'),
])
def test_execution_errors_propagate_without_retry(error):
    s = suite()
    s._execute = Mock(side_effect=error)
    with pytest.raises(pymysql.MySQLError):
        s._merge_with_summary('s', 'd', 'ACCEPT')
    assert s._execute.call_count == 1
    assert s._merge_summary_supported is None


@pytest.mark.parametrize('result', [None, [], [(1,)], [(0,) * 7] * 2, [(-1,) * 7]])
def test_invalid_results_never_select_legacy_replay(result):
    s = suite()
    s._execute = Mock(return_value=result)
    with pytest.raises(RuntimeError, match='not retried'):
        s._merge_with_summary('s', 'd', 'SKIP')
    assert s._execute.call_count == 1


def test_known_support_never_falls_back_after_later_parse_error():
    s = suite()
    s._merge_summary_supported = True
    s._execute = Mock(side_effect=pymysql.ProgrammingError(
        1064, 'syntax error at line 1 column 50 near "OUTPUT SUMMARY";'))
    with pytest.raises(pymysql.MySQLError):
        s._merge_with_summary('s', 'd', 'SKIP')
    assert s._merge_summary_supported is True
