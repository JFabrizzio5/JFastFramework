"""The transaction boundary: when it commits, what it fails with, and races.

Two halves. The first runs on SQLite and pins the parts that do not need a
server: that a session commits before the response exists, that the plugin
refuses a route that would commit after it, and what the repository turns a
constraint or a stale version into.

The second needs PostgreSQL, because everything it asserts is concurrency --
twenty inserts racing a unique constraint, two transactions racing a counter,
a genuine serialisation failure -- and SQLite serialises every writer, so a
race there cannot lose. Skipped without a server; CI starts one and fails if
these skip. Bring one up with::

    docker run -d --name pg -p 5499:5432 \\
      -e POSTGRES_USER=jfast -e POSTGRES_PASSWORD=jfast -e POSTGRES_DB=jfast postgres:16
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, Request
from sqlalchemy import ForeignKey, UniqueConstraint, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jfastframework.db import VersionedMixin, advisory_lock, run_in_transaction
from jfastframework.db.base import TimestampMixin
from jfastframework.db.repository import BaseRepository
from jfastframework.db.transactions import conflict_from, is_retryable, sqlstate
from jfastframework.errors import ConflictError, PluginError, PreconditionFailedError
from jfastframework.plugins.builtin.database import (
    DatabasePlugin,
    DbSession,
    ReadSession,
    session_dependency,
    session_scope_violations,
)
from jfastframework.plugins.builtin.observability import request_id_var, tenant_id_var
from jfastframework.queues.base import Job
from jfastframework.queues.worker import TaskRegistry, Worker
from jfastframework.testing import build_test_app, client_for

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
PG_DSN = f"{PG_BASE}/jfast"


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "tx_customers"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(default="t1")
    name: Mapped[str] = mapped_column(default="")


class Order(Base):
    __tablename__ = "tx_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(default="t1")
    customer_id: Mapped[int] = mapped_column(ForeignKey("tx_customers.id"))


class Account(Base, VersionedMixin):
    __tablename__ = "tx_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(default="t1")
    balance: Mapped[int] = mapped_column(default=0)


class CustomerRepository(BaseRepository[Customer]):
    model = Customer


class OrderRepository(BaseRepository[Order]):
    model = Order


class AccountRepository(BaseRepository[Account]):
    model = Account


# -- when the commit happens -----------------------------------------------


async def _app(tmp_path: Path) -> tuple[Any, DatabasePlugin]:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'tx.db'}"
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    app = build_test_app()
    plugin = DatabasePlugin({"dsn": dsn})
    plugin.register(app.state.jfast)
    return app, plugin


async def test_a_commit_that_fails_is_a_500_not_a_201(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The whole point of the scope: the client hears about the failed commit."""
    app, _ = await _app(tmp_path)

    @app.post("/customers", status_code=201)
    async def create(session: DbSession) -> dict[str, str]:
        await CustomerRepository(session).create(name="acme")
        return {"ok": "yes"}

    from sqlalchemy.ext.asyncio import AsyncSession

    async def refuse(self: AsyncSession) -> None:
        raise RuntimeError("the commit did not happen")

    monkeypatch.setattr(AsyncSession, "commit", refuse)
    # raise_app_exceptions=False: see the response a real client gets, rather
    # than the exception Starlette re-raises after sending it.
    import httpx

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/customers")
    assert response.status_code == 500


async def test_the_row_is_committed_before_the_response_is_sent(tmp_path: Path) -> None:
    """Read your own write, on a different session, the instant the 201 arrives."""
    app, _ = await _app(tmp_path)

    @app.post("/customers", status_code=201)
    async def create(session: DbSession) -> dict[str, int]:
        customer = await CustomerRepository(session).create(name="acme")
        return {"id": customer.id}

    @app.get("/customers/{customer_id}")
    async def read(customer_id: int, session: ReadSession) -> dict[str, str]:
        customer = await CustomerRepository(session).get_or_raise(customer_id)
        return {"name": customer.name}

    async with client_for(app) as client:
        created = await client.post("/customers")
        fetched = await client.get(f"/customers/{created.json()['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == {"name": "acme"}


async def test_startup_refuses_a_session_that_would_commit_after_the_response(
    tmp_path: Path,
) -> None:
    app, plugin = await _app(tmp_path)

    # Two levels down, the way every generated module wires it.
    def get_repository(session: Any = Depends(session_dependency)) -> CustomerRepository:
        return CustomerRepository(session)

    @app.post("/customers")
    async def create(repository: CustomerRepository = Depends(get_repository)) -> None:
        return None

    assert session_scope_violations(app) == ["POST /customers -> session_dependency"]
    with pytest.raises(PluginError, match="POST /customers"):
        await plugin.startup(app.state.jfast)


async def test_the_aliases_and_an_explicit_scope_both_pass(tmp_path: Path) -> None:
    app, plugin = await _app(tmp_path)

    @app.get("/a")
    async def a(session: DbSession) -> None:
        return None

    @app.get("/b")
    async def b(
        request: Request, session: Any = Depends(session_dependency, scope="function")
    ) -> None:
        return None

    assert session_scope_violations(app) == []
    await plugin.startup(app.state.jfast)


# -- what the repository fails with ------------------------------------------


@pytest.fixture
async def sqlite_session():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def test_a_duplicate_is_a_conflict_not_a_driver_error(sqlite_session) -> None:  # type: ignore[no-untyped-def]
    repository = CustomerRepository(sqlite_session)
    await repository.create(name="acme")
    with pytest.raises(ConflictError):
        await repository.create(name="acme")


async def test_a_stale_expected_version_is_a_412(sqlite_session) -> None:  # type: ignore[no-untyped-def]
    repository = AccountRepository(sqlite_session)
    account = await repository.create(balance=10)
    assert account.version == 1

    await repository.update(account, expected_version=1, balance=20)
    assert account.version == 2

    with pytest.raises(PreconditionFailedError) as raised:
        await repository.update(account, expected_version=1, balance=30)
    assert raised.value.extra["current_version"] == 2
    assert account.balance == 20


async def test_expected_version_on_an_unversioned_model_says_so(sqlite_session) -> None:  # type: ignore[no-untyped-def]
    repository = CustomerRepository(sqlite_session)
    customer = await repository.create(name="acme")
    with pytest.raises(TypeError, match="VersionedMixin"):
        await repository.update(customer, expected_version=1, name="other")


async def test_two_sessions_saving_the_same_version_lose_one_loudly(tmp_path: Path) -> None:
    """The lost update: both read version 1, the second write must not win."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as setup:
        await AccountRepository(setup).create(id=1, balance=0)
        await setup.commit()

    async with maker() as first, maker() as second:
        mine = await AccountRepository(first).get_or_raise(1)
        theirs = await AccountRepository(second).get_or_raise(1)
        await AccountRepository(first).update(mine, balance=100)
        await first.commit()
        with pytest.raises(ConflictError, match="changed by another request"):
            await AccountRepository(second).update(theirs, balance=-5)
    await engine.dispose()


def test_versioned_after_timestamps_refuses_to_build() -> None:
    with pytest.raises(TypeError, match="before TimestampMixin"):

        class Wrong(Base, TimestampMixin, VersionedMixin):  # type: ignore[misc]
            __tablename__ = "tx_wrong"
            id: Mapped[int] = mapped_column(primary_key=True)


def test_versioned_keeps_the_timestamp_mixins_eager_defaults() -> None:
    class Both(Base, VersionedMixin, TimestampMixin):
        __tablename__ = "tx_both"
        id: Mapped[int] = mapped_column(primary_key=True)

    mapper = Both.__mapper__
    assert mapper.version_id_col is Both.__table__.c.version
    assert mapper.eager_defaults is True


class _DriverError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.sqlstate = code


class _Wrapped(Exception):
    def __init__(self, orig: BaseException) -> None:
        super().__init__(str(orig))
        self.orig = orig


def test_sqlstate_is_found_through_the_wrapper() -> None:
    assert sqlstate(_Wrapped(_DriverError("23505"))) == "23505"
    assert sqlstate(ValueError("nothing")) is None


def test_only_serialisation_failures_and_deadlocks_are_retryable() -> None:
    assert is_retryable(_Wrapped(_DriverError("40001")))
    assert is_retryable(_Wrapped(_DriverError("40P01")))
    assert not is_retryable(_Wrapped(_DriverError("23505")))


def test_constraint_codes_map_to_conflicts() -> None:
    assert conflict_from(_Wrapped(_DriverError("23505"))) is not None
    assert conflict_from(_Wrapped(_DriverError("23503"))) is not None
    assert conflict_from(_Wrapped(_DriverError("23502"))) is None


async def test_run_in_transaction_retries_the_whole_unit(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    calls = 0

    async def work(session: Any) -> int:
        nonlocal calls
        calls += 1
        await CustomerRepository(session).create(name=f"attempt-{calls}")
        if calls == 1:
            raise _Wrapped(_DriverError("40001"))
        return calls

    assert await run_in_transaction(maker, work, base_delay=0) == 2
    async with maker() as session:
        names = (await session.execute(select(Customer.name))).scalars().all()
    # The first attempt rolled back entirely: no half of it survived.
    assert names == ["attempt-2"]
    await engine.dispose()


async def test_run_in_transaction_does_not_retry_a_real_error(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'n.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    calls = 0

    async def work(session: Any) -> None:
        nonlocal calls
        calls += 1
        raise _Wrapped(_DriverError("23505"))

    with pytest.raises(_Wrapped):
        await run_in_transaction(maker, work, base_delay=0)
    assert calls == 1
    await engine.dispose()


async def test_advisory_lock_is_a_no_op_on_sqlite(sqlite_session) -> None:  # type: ignore[no-untyped-def]
    await advisory_lock(sqlite_session, "anything")


# -- jobs run as the request that queued them --------------------------------


class _OneJob:
    visibility_timeout = 30.0

    def __init__(self, job: Job) -> None:
        self.job: Job | None = job
        self.acked = False

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        job, self.job = self.job, None
        return job

    async def ack(self, job: Job) -> None:
        self.acked = True

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        raise AssertionError("the job should have succeeded")


async def test_a_job_carries_the_tenant_and_request_that_queued_it() -> None:
    tenant_token = tenant_id_var.set("acme")
    request_token = request_id_var.set("req-1")
    try:
        job = Job(task="send")
    finally:
        tenant_id_var.reset(tenant_token)
        request_id_var.reset(request_token)
    assert (job.tenant_id, job.request_id) == ("acme", "req-1")

    seen: dict[str, str | None] = {}
    registry = TaskRegistry()

    @registry.task("send")
    async def send(payload: dict[str, Any]) -> None:
        seen["tenant"] = tenant_id_var.get()
        seen["request"] = request_id_var.get()

    backend = _OneJob(job)
    assert await Worker(backend, registry).run_once()  # type: ignore[arg-type]
    assert seen == {"tenant": "acme", "request": "req-1"}
    assert backend.acked
    # And nothing leaks into whatever the worker runs next.
    assert tenant_id_var.get() is None


def test_a_job_built_outside_a_request_has_no_tenant() -> None:
    assert Job(task="nightly").tenant_id is None


# -- PostgreSQL: races that SQLite cannot lose -------------------------------


@pytest.fixture
async def pg_maker():  # type: ignore[no-untyped-def]
    engine = create_async_engine(PG_DSN, pool_size=25, max_overflow=0)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await engine.dispose()
        pytest.skip(f"no PostgreSQL at {PG_DSN}")
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_twenty_racing_creates_make_one_row_and_nineteen_conflicts(pg_maker) -> None:  # type: ignore[no-untyped-def]
    """Every request passes a check-then-insert; only the constraint decides."""
    start = asyncio.Event()

    async def attempt() -> str:
        async with pg_maker() as session:
            repository = CustomerRepository(session)
            await start.wait()
            if await repository.find_one(name="acme") is not None:
                return "checked"
            try:
                await repository.create(name="acme")
                await session.commit()
                return "created"
            except ConflictError:
                await session.rollback()
                return "conflict"

    tasks = [asyncio.create_task(attempt()) for _ in range(20)]
    await asyncio.sleep(0.05)
    start.set()
    outcomes = await asyncio.gather(*tasks)

    assert outcomes.count("created") == 1
    assert outcomes.count("created") + outcomes.count("conflict") + outcomes.count("checked") == 20
    async with pg_maker() as session:
        assert len((await session.execute(select(Customer))).scalars().all()) == 1


async def test_a_missing_parent_is_a_conflict(pg_maker) -> None:  # type: ignore[no-untyped-def]
    async with pg_maker() as session:
        with pytest.raises(ConflictError, match="does not exist"):
            await OrderRepository(session).create(customer_id=404)


async def _race_the_counter(pg_maker, guard: str) -> int:  # type: ignore[no-untyped-def]
    """Ten read-modify-writes of one balance, each pausing between read and write."""
    async with pg_maker() as session:
        await AccountRepository(session).create(id=1, balance=0)
        await session.commit()

    async def deposit() -> None:
        async with pg_maker() as session:
            repository = AccountRepository(session)
            if guard == "advisory":
                await advisory_lock(session, "account:1")
                account = await repository.get_or_raise(1)
            else:
                account = await repository.get_for_update(1)
            balance = account.balance
            await asyncio.sleep(0.01)
            await repository.update(account, balance=balance + 1)
            await session.commit()

    await asyncio.gather(*(deposit() for _ in range(10)))
    async with pg_maker() as session:
        return (await AccountRepository(session).get_or_raise(1)).balance


async def test_an_advisory_lock_serialises_the_read_modify_write(pg_maker) -> None:  # type: ignore[no-untyped-def]
    assert await _race_the_counter(pg_maker, "advisory") == 10


async def test_select_for_update_serialises_the_read_modify_write(pg_maker) -> None:  # type: ignore[no-untyped-def]
    assert await _race_the_counter(pg_maker, "row") == 10


async def test_a_real_serialisation_failure_is_retried_to_success(pg_maker) -> None:  # type: ignore[no-untyped-def]
    """Two SERIALIZABLE transactions write-skew; the loser runs again and lands."""
    engine = pg_maker.kw["bind"]
    serializable = async_sessionmaker(
        engine.execution_options(isolation_level="SERIALIZABLE"), expire_on_commit=False
    )
    both_read = asyncio.Barrier(2)
    attempts = {"a": 0, "b": 0}

    def work(who: str):  # type: ignore[no-untyped-def]
        async def run(session: Any) -> None:
            attempts[who] += 1
            total = (await session.execute(text("select count(*) from tx_customers"))).scalar_one()
            if attempts[who] == 1:
                await both_read.wait()
            await session.execute(
                text("insert into tx_customers (tenant_id, name) values ('t1', :n)"),
                {"n": f"{who}-{total}"},
            )

        return run

    await asyncio.gather(
        run_in_transaction(serializable, work("a"), base_delay=0.01),
        run_in_transaction(serializable, work("b"), base_delay=0.01),
    )
    # One of them saw the other's commit only on its second attempt.
    assert sorted(attempts.values()) == [1, 2]
    async with pg_maker() as session:
        names = sorted((await session.execute(select(Customer.name))).scalars().all())
    assert names in (["a-0", "b-1"], ["a-1", "b-0"])


async def test_the_driver_reports_a_unique_violation_as_23505(pg_maker) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import IntegrityError

    async with pg_maker() as session:
        session.add_all([Customer(name="x"), Customer(name="x")])
        with pytest.raises(IntegrityError) as raised:
            await session.flush()
    assert sqlstate(raised.value) == "23505"
