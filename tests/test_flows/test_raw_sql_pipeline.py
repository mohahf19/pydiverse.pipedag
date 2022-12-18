from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

from pydiverse.pipedag import Flow, Stage, Table, materialize
from pydiverse.pipedag.context import ConfigContext
from pydiverse.pipedag.materialize.container import RawSql
from pydiverse.pipedag.util.config import PipedagConfig

"""
Attention: Wrapping Raw SQL statements should always be just the first step of pipedag adoption.
Ideally the next step is to extract individual transformations (SELECT statements) so they can 
be gradually converted from text SQL to programmatically created SQL (python)
"""


@materialize(input_type=sa.Table, lazy=True)
def tsql(
    name: str,
    script_directory: Path,
    *,
    out_stage: Stage | None = None,
    in_sql=None,
    helper_sql=None,
    depend=None,
):
    _ = depend  # only relevant for adding additional task dependency
    script_path = script_directory / name
    sql = Path(script_path).read_text()
    sql = raw_sql_bind_schema(sql, "out_", out_stage, transaction=True)
    sql = raw_sql_bind_schema(sql, "in_", in_sql)
    sql = raw_sql_bind_schema(sql, "helper_", helper_sql)
    return RawSql(sql, Path(script_path).name, out_stage)


def raw_sql_bind_schema(
    sql, prefix: str, stage: Stage | RawSql | None, *, transaction=False
):
    if isinstance(stage, RawSql):
        stage = stage.stage
    config = ConfigContext.get()
    store = config.store.table_store
    if stage is not None:
        stage_name = stage.transaction_name if transaction else stage.name
        schema = store.get_schema(stage_name).get()
        database, schema_only = schema.split(".")
        sql = sql.replace("{{%sschema}}" % prefix, schema)
        sql = sql.replace("{{%sdatabase}}" % prefix, database)
        sql = sql.replace("{{%sschema_only}}" % prefix, schema_only)
    return sql


def _test_raw_sql(instance):
    cfg = PipedagConfig.default.get(instance=instance)
    parent_dir = Path(__file__).parent / "raw_sql_scripts" / instance
    with Flow() as flow:
        with Stage("helper") as out_stage:
            helper = tsql("create_db_helpers.sql", parent_dir, out_stage=out_stage)
        with Stage("raw") as out_stage:
            _dir = parent_dir / "raw"
            raw = tsql("raw_views.sql", _dir, out_stage=out_stage, helper_sql=helper)
        # with Stage("ref") as out_stage:
        #     _dir = parent_dir / "ref"
        #     ref = tsql("reference_claim_statistics.sql", _dir, in_sql=raw, out_stage=out_stage)
        #     ref = tsql("reference_tables.sql", _dir, in_sql=raw, out_stage=out_stage, depend=ref)
        with Stage("prep") as out_stage:
            _dir = parent_dir / "prep"
            prep = tsql(
                "entity_checks.sql", _dir, in_sql=raw, out_stage=out_stage, depend=raw
            )
            # prep = tsql("entity_checks.sql", _dir, in_sql=raw, out_stage=out_stage, depend=ref)
            prep = tsql(
                "more_tables.sql", _dir, in_sql=raw, out_stage=out_stage, depend=prep
            )
            _ = prep
    flow_result = flow.run(cfg)
    assert flow_result.successful


@pytest.mark.mssql
# pytsql is currently not ready for this code
# @pytest.mark.parametrize("instance", ["mssql", "mssql_pytsql", "mssql_pytsql_isolate"])
@pytest.mark.parametrize("instance", ["mssql"])
def test_raw_sql_mssql(instance):
    _test_raw_sql(instance)


# @pytest.mark.ibm_db2
# def test_raw_sql_ibm_db2():
#     _test_raw_sql("ibm_db2")
#
#
# def test_raw_sql_mssql_postgres():
#     _test_raw_sql("postgres")