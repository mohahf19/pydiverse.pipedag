from __future__ import annotations

import pandas as pd
import pytest
import sqlalchemy as sa

from pydiverse.pipedag import Blob, Flow, Stage, Table
from pydiverse.pipedag.context import StageLockContext
from pydiverse.pipedag.materialize.container import RawSql
from pydiverse.pipedag.materialize.core import materialize

from tests.util import select_as, tasks_library as m
from tests.util.spy import spy_task

# Test that running a flow that contains a task with an invalid cache function
# doesn't trigger that task when run with ignore_fresh_input=True, and it is
# otherwise cache valid.


def test_literal(mocker):
    cache_value = 0

    def cache():
        return cache_value

    @materialize(cache=cache)
    def return_cache_value():
        return cache_value

    with Flow() as flow:
        with Stage("stage_1"):
            out = return_cache_value()
            child = m.noop(out)

    # Initial Call
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 0
        assert result.get(child) == 0

    # Calling flow.run again shouldn't call the task
    out_spy = spy_task(mocker, out)
    child_spy = spy_task(mocker, child)
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 0
        assert result.get(child) == 0
        out_spy.assert_not_called()
        child_spy.assert_not_called()

    # Changing the cache value while setting ignore_fresh_input=True should
    # ignore the cache function.
    cache_value = 1
    for _ in range(3):
        with StageLockContext():
            result = flow.run(ignore_fresh_input=True)
            assert result.get(out) == 0
            assert result.get(child) == 0
            out_spy.assert_not_called()
            child_spy.assert_not_called()

    # Changing the cache value should cause it to get called again
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 1
        assert result.get(child) == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()


def test_table(mocker):
    cache_value = 0

    def cache():
        return cache_value

    @materialize(cache=cache)
    def return_cache_table():
        return Table(select_as(cache_value, "x"))

    @materialize(input_type=pd.DataFrame)
    def get_first(table, col):
        return int(table[col][0])

    with Flow() as flow:
        with Stage("stage_1"):
            out = return_cache_table()
            child = get_first(out, "x")

    # Initial Call
    with StageLockContext():
        result = flow.run()
        assert result.get(child) == 0

    # Calling flow.run again shouldn't call the task
    out_spy = spy_task(mocker, out)
    child_spy = spy_task(mocker, child)
    with StageLockContext():
        result = flow.run()
        assert result.get(child) == 0
        out_spy.assert_not_called()
        child_spy.assert_not_called()

    # Changing the cache value while setting ignore_fresh_input=True should
    # ignore the cache function.
    cache_value = 1
    for _ in range(3):
        with StageLockContext():
            result = flow.run(ignore_fresh_input=True)
            assert result.get(child) == 0
            out_spy.assert_not_called()
            child_spy.assert_not_called()

    # Changing the cache value should cause it to get called again
    with StageLockContext():
        result = flow.run()
        assert result.get(child) == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()


def test_lazy_table(mocker):
    cache_value = 0
    lazy_value = 0

    def cache():
        return cache_value

    @materialize(cache=cache, lazy=True, nout=2)
    def input_task():
        return Table(select_as(lazy_value, "x")), cache_value

    @materialize(input_type=pd.DataFrame)
    def get_first(table, col):
        return int(table[col][0])

    with Flow() as flow:
        with Stage("stage_1"):
            out, out_cache = input_task()
            child = get_first(out, "x")
            cache_child = m.noop(out_cache)

    # Initial Call
    with StageLockContext():
        result = flow.run()
        assert result.get(child) == 0

    # Calling flow.run again should call the lazy task but not the child task
    out_spy = spy_task(mocker, out)
    child_spy = spy_task(mocker, child)
    cache_spy = spy_task(mocker, cache_child)
    for _ in range(3):
        with StageLockContext():
            result = flow.run()
            assert result.get(child) == 0
            assert result.get(cache_child) == 0
            out_spy.assert_called_once()
            child_spy.assert_not_called()
            cache_spy.assert_not_called()

    # Changing the cache value while setting ignore_fresh_input=True should
    # still call the lazy task.
    cache_value = 1
    with StageLockContext():
        result = flow.run(ignore_fresh_input=True)
        assert result.get(child) == 0
        assert result.get(cache_child) == 1
        out_spy.assert_called_once()
        child_spy.assert_not_called()
        cache_spy.assert_called_once()

    # Only changing the lazy_value should cause the child task to get called
    lazy_value = 1
    with StageLockContext():
        result = flow.run(ignore_fresh_input=True)
        assert result.get(child) == 1
        assert result.get(cache_child) == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()
        cache_spy.assert_not_called()

    # We can't avoid the cache invalidation of get_first here since we do
    # ignore_fresh_input based cache_fn_hash filtering on task level and
    # not on lazy table level.
    with StageLockContext():
        result = flow.run()
        assert result.get(child) == 1
        assert result.get(cache_child) == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()
        cache_spy.assert_not_called()

    # The child task shouldn't get called again, because the lazy sql didn't change
    for _ in range(3):
        with StageLockContext():
            result = flow.run()
            assert result.get(child) == 1
            assert result.get(cache_child) == 1
            out_spy.assert_called_once()
            child_spy.assert_not_called()
            cache_spy.assert_not_called()


def test_blob(mocker):
    cache_value = 0

    def cache():
        return cache_value

    @materialize(cache=cache)
    def return_cache_blob():
        return Blob(cache_value)

    with Flow() as flow:
        with Stage("stage_1"):
            out = return_cache_blob()
            child = m.noop(out)

    # Initial Call
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 0
        assert result.get(child) == 0

    # Calling flow.run again shouldn't call the task
    out_spy = spy_task(mocker, out)
    child_spy = spy_task(mocker, child)
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 0
        assert result.get(child) == 0
        out_spy.assert_not_called()
        child_spy.assert_not_called()

    # Changing the cache value while setting ignore_fresh_input=True should
    # ignore the cache function.
    cache_value = 1
    for _ in range(3):
        with StageLockContext():
            result = flow.run(ignore_fresh_input=True)
            assert result.get(out) == 0
            assert result.get(child) == 0
            out_spy.assert_not_called()
            child_spy.assert_not_called()

    # Changing the cache value should cause it to get called again
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 1
        assert result.get(child) == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()


@pytest.mark.skip_instance("mssql", "mssql_pytsql", "ibm_db2", "ibm_db2_avoid_schema")
def test_raw_sql(mocker):
    cache_value = 0
    raw_value = 0

    def cache(*args, **kwargs):
        return cache_value

    @materialize(lazy=True, cache=cache)
    def raw_sql_task(stage):
        return RawSql(
            f"""
            CREATE TABLE {stage.transaction_name}.raw_table AS 
            SELECT {raw_value} as x
            """,
            "raw_table",
            stage,
        )

    @materialize
    def child_task(input):
        return Table(sa.text(f"SELECT * FROM {input.stage.transaction_name}.raw_table"))

    with Flow() as flow:
        with Stage("raw_sql_stage") as stage:
            out = raw_sql_task(stage)
            child = child_task(out)

    # Initial Run
    with StageLockContext():
        result = flow.run()
        assert result.get(child, as_type=pd.DataFrame)["x"][0] == 0

    # Calling flow.run again should call the raw sql task but not the child task
    out_spy = spy_task(mocker, out)
    child_spy = spy_task(mocker, child)
    with StageLockContext():
        result = flow.run()
        assert result.get(child, as_type=pd.DataFrame)["x"][0] == 0
        out_spy.assert_called_once()
        child_spy.assert_not_called()

    # Changing the cache value while setting ignore_fresh_input=True should
    # still call the raw sql task.
    cache_value = 1
    for _ in range(3):
        with StageLockContext():
            result = flow.run(ignore_fresh_input=True)
            assert result.get(child, as_type=pd.DataFrame)["x"][0] == 0
            out_spy.assert_called_once()
            child_spy.assert_not_called()

    # Only changing the raw_value should cause the child task to get called
    raw_value = 1
    with StageLockContext():
        result = flow.run(ignore_fresh_input=True)
        assert result.get(child, as_type=pd.DataFrame)["x"][0] == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()

    # We can't avoid the cache invalidation of get_first here since we do
    # ignore_fresh_input based cache_fn_hash filtering on task level and
    # not on lazy table level.
    with StageLockContext():
        result = flow.run()
        assert result.get(child, as_type=pd.DataFrame)["x"][0] == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()

    # The child task shouldn't get called again, because the raw sql didn't change
    for _ in range(3):
        with StageLockContext():
            result = flow.run()
            assert result.get(child, as_type=pd.DataFrame)["x"][0] == 1
            out_spy.assert_called_once()
            child_spy.assert_not_called()


# Some more complicated tests that validate the behaviour of
# ignore_fresh_input=True


def test_input_invalid(mocker):
    # Test that it does get run if it is otherwise cache invalid
    lazy_value = 0

    @materialize(lazy=True)
    def input_task():
        return lazy_value

    with Flow() as flow:
        with Stage("stage_1"):
            out = input_task()
            child = m.noop(out)

    with StageLockContext():
        result = flow.run()
        assert result.successful
        assert result.get(out) == 0
        assert result.get(child) == 0

    # Setting ignore_fresh_input shouldn't have an influence on a lazy task
    out_spy = spy_task(mocker, out)
    child_spy = spy_task(mocker, child)
    with StageLockContext():
        result = flow.run(ignore_fresh_input=True)
        assert result.get(out) == 0
        assert result.get(child) == 0
        out_spy.assert_called_once()
        child_spy.assert_not_called()

    # Despite ignore_fresh_input=True, the child tasks should still run
    # because its inputs changed
    lazy_value = 1
    with StageLockContext():
        result = flow.run(ignore_fresh_input=True)
        assert result.get(out) == 1
        assert result.get(child) == 1
        out_spy.assert_called_once()
        child_spy.assert_called_once()


def test_cache_temporarily_different(mocker):
    cache_value = 0

    def cache():
        return cache_value

    @materialize(cache=cache)
    def return_cache_value():
        return cache_value

    with Flow() as flow:
        with Stage("stage_1"):
            out = return_cache_value()
            child = m.noop(out)

    # Initial Call
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 0
        assert result.get(child) == 0

    # Calling flow.run again shouldn't call the task
    out_spy = spy_task(mocker, out)
    child_spy = spy_task(mocker, child)
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 0
        assert result.get(child) == 0
        out_spy.assert_not_called()
        child_spy.assert_not_called()

    # Changing the cache value while setting ignore_fresh_input=True should
    # ignore the cache function.
    cache_value = 1
    for _ in range(3):
        with StageLockContext():
            result = flow.run(ignore_fresh_input=True)
            assert result.get(out) == 0
            assert result.get(child) == 0
            out_spy.assert_not_called()
            child_spy.assert_not_called()

    # Changing the cache value back to the original shouldn't trigger
    # any tasks to run
    cache_value = 0
    with StageLockContext():
        result = flow.run()
        assert result.get(out) == 0
        assert result.get(child) == 0
        out_spy.assert_not_called()
        child_spy.assert_not_called()
