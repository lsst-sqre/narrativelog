import collections.abc
import contextlib
import os
import subprocess
import typing
import unittest
import uuid

import sqlalchemy as sa
import sqlalchemy.engine
import sqlalchemy.types as saty
import testing.postgresql
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio.engine import AsyncConnection, AsyncEngine
from sqlalchemy.future.engine import Connection

from narrativelog.testutils import db_config_from_dsn, modify_environ

# Length of the site_id field.
SITE_ID_LEN = 16


@contextlib.asynccontextmanager
async def create_database() -> collections.abc.AsyncGenerator[
    AsyncEngine, None
]:
    """Create an empty database and set env vars to point to it.

    Returns
    -------
    url
        URL to database
    """
    with testing.postgresql.Postgresql() as postgresql:
        postgres_url = postgresql.url()
        async_url = sqlalchemy.engine.make_url(postgres_url)
        async_url = async_url.set(drivername="postgresql+asyncpg")

        db_config = db_config_from_dsn(postgresql.dsn())
        with modify_environ(**db_config):
            engine = create_async_engine(async_url)
            yield engine


async def get_column_info(
    connection: AsyncConnection, table: str
) -> list[dict[str, typing.Any]]:
    """Get column info for a specified table.

    Parameters
    ----------
    connection
        Async connection
    table
        Table name

    Returns
    -------
    info
        A list of dicts, with one entry per column.
        Each dict includes the following keys:
        "name", "type", "nullable", "default", and "autoincrement"
    """

    def _impl(connection: Connection) -> list[str]:
        """Synchronous implementation.

        Inspect does not work with an async connection
        """
        inspector = inspect(connection)
        return inspector.get_columns(table)

    return await connection.run_sync(_impl)


async def get_column_names(
    connection: AsyncConnection, table: str
) -> list[str]:
    """A simplified version of get_column_info that just returns column names.

    Parameters
    ----------
    connection
        Async connection
    table
        Table name

    Returns
    -------
    column_names
        A list of column names.
    """
    column_info = await get_column_info(connection=connection, table=table)
    return [item["name"] for item in column_info]


async def get_table_names(connection: AsyncConnection) -> list[str]:
    """Get the names of tables in the narrativelog database.

    Parameters
    ----------
    connection
        Async connection

    Returns
    -------
    table_names
        A list of table names.
    """

    def _impl(connection: Connection) -> list[str]:
        """Synchronous implementation.

        Inspect does not work with an async connection
        """
        inspector = inspect(connection)
        return inspector.get_table_names()

    return await connection.run_sync(_impl)


def create_old_message_table() -> sa.Table:
    """Make a model of the oldest message table supported by alembic.

    This is the table in narrativelog version 0.2.
    """
    table = sa.Table(
        "message",
        sa.MetaData(),
        # See https://stackoverflow.com/a/49398042 for UUID:
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
        ),
        sa.Column("site_id", saty.String(length=SITE_ID_LEN)),
        sa.Column("message_text", saty.Text(), nullable=False),
        sa.Column("level", saty.Integer(), nullable=False),
        sa.Column("tags", saty.ARRAY(sa.Text), nullable=False),
        sa.Column("urls", saty.ARRAY(sa.Text), nullable=False),
        sa.Column("time_lost", saty.Interval(), nullable=False),
        sa.Column("date_user_specified", saty.DateTime(), nullable=True),
        sa.Column("user_id", saty.String(), nullable=False),
        sa.Column("user_agent", saty.String(), nullable=False),
        sa.Column("is_human", saty.Boolean(), nullable=False),
        sa.Column(
            "is_valid",
            saty.Boolean(),
            sa.Computed("date_invalidated is null"),
            nullable=False,
        ),
        sa.Column("date_added", saty.DateTime(), nullable=False),
        sa.Column("date_invalidated", saty.DateTime(), nullable=True),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["message.id"]),
    )

    for name in (
        "level",
        "tags",
        "time_lost",
        "user_id",
        "is_valid",
        "date_added",
    ):
        sa.Index(f"idx_{name}", table.columns[name])

    return table


class AlembicMigrationTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_no_message_table(self) -> None:
        async with create_database() as engine:
            async with engine.connect() as connection:
                table_names = await get_table_names(connection)
                assert table_names == []

            subprocess.run(
                ["alembic", "upgrade", "head"], env=os.environ.copy()
            )

            async with engine.connect() as connection:
                table_names = await get_table_names(connection)
                assert set(table_names) == {"alembic_version"}

    async def test_old_message_table(self) -> None:
        new_columns = {
            "systems",
            "subsystems",
            "cscs",
            "date_begin",
            "date_end",
        }
        async with create_database() as engine:
            old_message_table = create_old_message_table()
            async with engine.begin() as connection:
                await connection.run_sync(
                    old_message_table.metadata.create_all
                )

                table_names = await get_table_names(connection)
                assert table_names == ["message"]

                column_names = await get_column_names(
                    connection, table="message"
                )
                assert new_columns & set(column_names) == set()

            subprocess.run(
                ["alembic", "upgrade", "head"], env=os.environ.copy()
            )

            async with engine.connect() as connection:
                table_names = await get_table_names(connection)
                assert set(table_names) == {"alembic_version", "message"}

                column_names = await get_column_names(
                    connection, table="message"
                )
                assert new_columns < set(column_names)
