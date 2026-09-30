"""Async PostgreSQL via SQLAlchemy 2.0, as one database or as several named ones.

A read replica, a per-tenant database and a shard are not three features but
one structure: a database the service can *name*. A single DSN field has no
room for any of them.

    [plugin.database]
    dsn_env = "JFAST_DB_DSN"          # the unnamed case, unchanged

    [plugin.database.connections.primary]
    dsn_env = "JFAST_DB_DSN"

    [plugin.database.connections.replica]
    dsn_env = "JFAST_DB_REPLICA_DSN"
    read_only = true

Leaving ``connections`` out is an alias for one instance called ``default``, so
``JFAST_DB_DSN`` and ``ctx.require("db.engine")`` mean the one database. Every
DSN is a ``SecretStr`` or an environment variable and is never echoed by
``jfast describe`` or by ``/info``.

Requires: ``pip install jfastframework[db]``
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jfastframework.errors import PluginError, ServiceUnavailableError
from jfastframework.http.resilience import BreakerPolicy, CircuitBreaker
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)
from jfastframework.resources import POSTGRES_SHM_SIZE

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.database")

# The instance a configuration without a `connections` block describes.
DEFAULT_CONNECTION = "default"

# A service owns ten ports and its plugins claim offsets inside that block:
# cache takes +3, mongo +4, qdrant +7 and +8, gRPC +9, and +0 is the service
# itself. These are what a database can have, with +1 first because that is
# where the single PostgreSQL has always been.
DATABASE_PORT_OFFSETS = (1, 2, 5, 6)
PORT_BLOCK_SIZE = 10

# Carried by the client, so the pin survives the redirect that follows a write
# and works the same on every process behind the load balancer.
PIN_HEADER = "X-JFast-Read-Pin"

# Methods that pin their client to the primary. A GET that writes has to say so
# with `mark_write()`; there is no way to detect it in time.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# The DSN prefix whose driver takes startup parameters. Every server setting
# below is an asyncpg feature; another driver silently ignores the argument, so
# it is not passed to one.
ASYNCPG_PREFIX = "postgresql+asyncpg"


class ReadOnlySessionError(RuntimeError):
    """A write reached a session that was checked out of a replica."""


class TenantPoolExhausted(ServiceUnavailableError, RuntimeError):
    """A new tenant would take the engine map past its ceiling.

    503, not 500. Every engine being busy is backpressure: the service is
    healthy, it is at capacity, and the request can succeed if it arrives
    again in a moment. As a bare ``RuntimeError`` it would reach the unhandled
    handler and come back as ``500 "An unexpected error occurred"`` -- which
    tells a client to stop and a reader to look for a bug, and hides the one
    signal that says raise ``tenant_max_engines`` or lower the concurrency.

    ``RuntimeError`` is kept in the bases so existing ``except RuntimeError``
    around a lease still catches it.
    """


class DatabaseUnavailableError(ServiceUnavailableError):
    """The database did not accept a connection, or dropped the one in use.

    503, because nothing about the request is wrong and the same request can
    succeed in a moment -- which is exactly what a client, a load balancer and
    a retrying proxy each need to be told. As a bare ``TimeoutError`` from the
    driver it reached the unhandled handler and answered 500, and a 500 reads
    as a bug in this service rather than an outage of the one behind it.
    """

    title = "Database Unavailable"


class ConnectionSettings(BaseModel):
    """One database instance. Anything left unset falls back to the plugin's value."""

    # Forbidden rather than ignored: `read_onlyy = true` on a replica is a
    # silent write to the standby, and there is no runtime symptom to find.
    model_config = ConfigDict(extra="forbid")

    dsn: SecretStr | None = None
    dsn_env: str = ""
    read_only: bool = False
    echo: bool | None = None
    pool_size: int | None = None
    max_overflow: int | None = None
    pool_pre_ping: bool | None = None
    pool_recycle: int | None = None
    pool_timeout: float | None = None
    # Per instance, because a replica may sit behind a pooler the primary
    # does not. None takes the plugin's `pgbouncer`.
    pgbouncer: bool | None = None
    connect_timeout: float | None = None
    ping_timeout: float | None = None
    command_timeout: float | None = None

    # Deploy generation
    include_infra: bool | None = None
    image: str = ""
    port_offset: int | None = None
    database: str = ""
    user: str = ""


class DatabaseSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_DB_", env_file=".env", extra="ignore")

    dsn: SecretStr = SecretStr("postgresql+asyncpg://postgres:postgres@localhost:5432/postgres")
    echo: bool = False
    # Ten connections per process. Pools are per *worker*, and the generated
    # image starts one per CPU up to eight: 30 connections x 8 workers is 240
    # from a single service against a PostgreSQL that accepts 100 by default,
    # and the service that falls over is whichever one connects next. Ten
    # leaves the default deployment at 80 with room beside it.
    #
    # Ten is not small for an async service either: a connection is held while
    # a query runs, not for the length of a request, so ten in flight per
    # worker is a lot of concurrent SQL. Raise it against a server that was
    # sized for it, and raise `server_max_connections` to say so.
    pool_size: int = 5
    max_overflow: int = 5
    pool_pre_ping: bool = True
    pool_recycle: int = 1800
    # Seconds a request waits for a pooled connection before giving up. A wait
    # with no ceiling turns a saturated pool into a queue that grows until the
    # load balancer times out, which reads as a hung service rather than a
    # busy one.
    pool_timeout: float = 30.0

    # -- deadlines ---------------------------------------------------
    # Seconds to open a connection: TCP, TLS and authentication together.
    # asyncpg's own default is 60, which is two request timeouts: a database
    # that accepts the socket and never answers -- a paused container, a
    # frozen VM, a full accept queue -- held every request that needed a new
    # connection for a minute and answered 504 from the edge. Ten covers SCRAM
    # on a busy server (measured at up to 4.4 s on a loaded laptop) and still
    # leaves a request time to answer 503 itself.
    connect_timeout: float = 10.0
    # Seconds the liveness check on checkout may take (`pool_pre_ping`).
    # SQLAlchemy's own ping has no deadline of its own, so a connection to a
    # database that stopped answering hung its request until the socket died
    # -- measured: never, before the request timeout. The framework runs a
    # bounded ping instead: past this, the connection is discarded and a new
    # one is tried under `connect_timeout`. A healthy server answers in
    # milliseconds; two seconds is headroom, not an estimate.
    ping_timeout: float = 2.0
    # Seconds one statement may run, client side. Off by default: migrations
    # and reports legitimately run for minutes, a request is already bounded
    # by `[app] request_timeout` and a job by the worker's `job_timeout`, and a
    # database that stops answering is caught by the ping and the connect
    # timeout above. Set it on a service whose every query should be fast.
    command_timeout: float = 0.0
    # Failed connection attempts in a row that open a breaker on connecting,
    # and how long it stays open. Without it every request during an outage
    # waits out `connect_timeout` -- ten seconds of a worker per request, the
    # queue behind them growing -- to learn what the previous request already
    # learned. Open, a request that needs a new connection answers 503 at once
    # and one probe per cool-down finds out whether the server is back.
    # Connections already in the pool are unaffected. 0 turns it off.
    breaker_failures: int = 2
    breaker_cool_down: float = 5.0

    # The zone every session computes in, whatever the server is configured
    # with. `date_trunc('day', ...)`, `CURRENT_DATE`, `now()::date` and any
    # `AT TIME ZONE` without an explicit zone all read the session's TimeZone,
    # which defaults to the server's -- so two replicas in two regions produce
    # two different daily reports from byte-identical rows, and nothing fails.
    # Pinning it here makes the answer a property of the query rather than of
    # where the container runs. Local days are a separate question, answered by
    # `[app] timezone` and `jfastframework.time.day_bounds`.
    #
    # Empty leaves the server's setting alone, which is only right when
    # something outside this service already guarantees it.
    session_timezone: str = "UTC"

    # Row-level security. Every transaction tells PostgreSQL its tenant, so a
    # table put under `enable_tenant_rls` in a migration returns only that
    # tenant's rows whatever the query says. See jfastframework.db.rls -- and
    # connect as a role that is neither a superuser nor BYPASSRLS, or the
    # policies do not apply: production refuses to start otherwise.
    rls: bool = False

    # The DSN points at PgBouncer (or another pooler) in *transaction* mode.
    # Each transaction may then run on a different server connection, and a
    # prepared statement asyncpg cached on one does not exist on the next:
    # `prepared statement "__asyncpg_stmt_7__" does not exist`, under load
    # and never on a laptop. True turns both statement caches off and gives
    # every statement a unique name, so two processes sharing a server
    # connection cannot collide either. Not needed on PgBouncer 1.21+ with
    # `max_prepared_statements` > 0, which tracks them itself -- but harmless
    # there, and correct when that setting is 0. Row-level security needs
    # nothing: the tenant is set transaction-locally. See docs/multitenancy.md.
    pgbouncer: bool = False

    # Named instances. Empty means one instance called `default`, configured by
    # the fields above.
    connections: dict[str, ConnectionSettings] = Field(default_factory=dict)
    default_connection: str = ""

    # -- read/write split ------------------------------------------
    read_write_split: bool = False
    # How long a client that wrote keeps reading from the primary. It has to
    # exceed real replication lag: a healthy standby on the same network is
    # milliseconds behind, and five seconds still covers a checkpoint spike or
    # a stalled WAL sender. Longer costs primary capacity; shorter reintroduces
    # exactly the bug the pin exists to remove.
    pin_window: float = 5.0
    pin_cookie: str = "jfast_rw"
    pin_on_unsafe_methods: bool = True

    # -- a database per tenant -------------------------------------
    tenant_dsn_template: str = ""
    tenant_dsn_env_template: str = ""
    # The ceiling that keeps a tenant map from becoming a connection storm.
    tenant_max_engines: int = 25
    tenant_pool_size: int = 2
    tenant_max_overflow: int = 2

    # What the server on the other end will accept, so the arithmetic below has
    # something to compare against. 100 is PostgreSQL's own default; a managed
    # instance publishes its own number and it is usually derived from RAM. 0
    # turns the check off for a server nobody here can know the size of.
    #
    # It exists because every pool number in this file is *per process*, and
    # the generated image runs one worker per CPU: 30 connections per worker
    # is 240 on an eight-core host, from one service, against a server that
    # accepts 100. Unless something multiplies those two numbers, the first
    # sign is `FATAL: sorry, too many clients already` -- in production, from
    # whichever service happens to connect last.
    server_max_connections: int = 100

    # Deploy generation
    include_infra: bool = True
    image: str = "pgvector/pgvector:pg16"
    port_offset: int = 1
    database: str = "app"
    user: str = "app"

    # -- derived ---------------------------------------------------

    def resolved_connections(self) -> dict[str, ConnectionSettings]:
        if self.connections:
            return dict(self.connections)
        return {DEFAULT_CONNECTION: ConnectionSettings(dsn=self.dsn)}

    def default_name(self) -> str:
        """The instance ``db.engine`` means: the one that may be written to."""
        connections = self.resolved_connections()
        if self.default_connection:
            if self.default_connection not in connections:
                known = ", ".join(sorted(connections))
                raise PluginError(
                    f"[plugin.database] default_connection = "
                    f"{self.default_connection!r} is not a declared connection. "
                    f"Known: {known}."
                )
            return self.default_connection
        for candidate in (DEFAULT_CONNECTION, "primary", "write"):
            if candidate in connections and not connections[candidate].read_only:
                return candidate
        for name, connection in connections.items():
            if not connection.read_only:
                return name
        raise PluginError(
            "every [plugin.database.connections] entry is read_only, so nothing "
            "in this service can write. Drop read_only from the primary."
        )

    def env_var_for(self, name: str) -> str:
        if name == DEFAULT_CONNECTION:
            return "JFAST_DB_DSN"
        slug = name.upper().replace("-", "_").replace(".", "_")
        return f"JFAST_DB_{slug}_DSN"

    def dsn_for(self, name: str) -> str:
        connection = self.resolved_connections()[name]
        if connection.dsn is not None:
            return connection.dsn.get_secret_value()

        variable = connection.dsn_env or self.env_var_for(name)
        value = os.environ.get(variable, "")
        if value:
            return value
        if name == self.default_name():
            # `dsn` already read JFAST_DB_DSN, and it carries the default that
            # `jfast start` on a laptop relies on.
            return self.dsn.get_secret_value()
        raise PluginError(
            f"database connection {name!r} has no DSN: {variable} is not set in "
            f"the environment. Set it, or give the connection an explicit "
            f"dsn_env in [plugin.database.connections.{name}]."
        )

    def engine_options(self, name: str) -> dict[str, Any]:
        """Pools are per instance; a global number is only the fallback."""
        connection = self.resolved_connections()[name]

        def pick(override: Any, fallback: Any) -> Any:
            return fallback if override is None else override

        return {
            "echo": pick(connection.echo, self.echo),
            "pool_size": pick(connection.pool_size, self.pool_size),
            "max_overflow": pick(connection.max_overflow, self.max_overflow),
            "pool_pre_ping": pick(connection.pool_pre_ping, self.pool_pre_ping),
            "pool_recycle": pick(connection.pool_recycle, self.pool_recycle),
            "pool_timeout": pick(connection.pool_timeout, self.pool_timeout),
        }

    def behind_pgbouncer(self, name: str) -> bool:
        connection = self.resolved_connections()[name]
        return self.pgbouncer if connection.pgbouncer is None else connection.pgbouncer

    def deadlines(self, name: str) -> tuple[float, float, float]:
        """``(connect, ping, command)`` seconds for one instance."""
        connection = self.resolved_connections()[name]

        def pick(override: float | None, fallback: float) -> float:
            return fallback if override is None else override

        return (
            pick(connection.connect_timeout, self.connect_timeout),
            pick(connection.ping_timeout, self.ping_timeout),
            pick(connection.command_timeout, self.command_timeout),
        )

    def validate_for_boot(self) -> None:
        """Every value that would otherwise fail on the first query, refused now."""
        import zoneinfo

        if self.session_timezone:
            try:
                zoneinfo.ZoneInfo(self.session_timezone)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                raise PluginError(
                    f"[plugin.database] session_timezone = {self.session_timezone!r} is "
                    f"not an IANA zone, so every connection would be refused by the "
                    f'server. Use a name like "UTC" or "America/Mexico_City".'
                ) from None
        for name in self.resolved_connections():
            options = self.engine_options(name)
            where = (
                "[plugin.database]"
                if name == DEFAULT_CONNECTION and not self.connections
                else f"[plugin.database.connections.{name}]"
            )
            if int(options["pool_size"]) < 1:
                # SQLAlchemy reads 0 as "no limit", the opposite of what it says.
                raise PluginError(
                    f"{where} pool_size must be at least 1; SQLAlchemy reads 0 as "
                    f"unlimited, which is the connection storm the pool exists to stop."
                )
            if int(options["max_overflow"]) < 0:
                raise PluginError(f"{where} max_overflow cannot be negative; 0 means no overflow.")
            if float(options["pool_timeout"]) <= 0:
                raise PluginError(
                    f"{where} pool_timeout must be positive: it is how long a request "
                    f"waits for a free connection before answering 503."
                )
            connect, ping, command = self.deadlines(name)
            if connect <= 0 or ping <= 0:
                raise PluginError(
                    f"{where} connect_timeout and ping_timeout must be positive; "
                    f"without them a database that stops answering hangs every request."
                )
            if command < 0:
                raise PluginError(f"{where} command_timeout cannot be negative; 0 turns it off.")
        if self.breaker_failures < 0 or self.breaker_cool_down <= 0:
            raise PluginError(
                "[plugin.database] breaker_failures cannot be negative (0 turns it off) "
                "and breaker_cool_down must be positive."
            )
        if self.read_write_split and self.pin_window <= 0:
            raise PluginError(
                "[plugin.database] pin_window must be positive while read_write_split "
                "is on, or a client reads its own write from a replica that has not "
                "replayed it."
            )
        for field in ("tenant_dsn_template", "tenant_dsn_env_template"):
            template = str(getattr(self, field))
            if template and "{tenant}" not in template:
                raise PluginError(
                    f"[plugin.database] {field} = {template!r} has no {{tenant}} "
                    f"placeholder, so every tenant would share one database."
                )
        if self.tenant_max_engines < 1 or self.tenant_pool_size < 1:
            raise PluginError(
                "[plugin.database] tenant_max_engines and tenant_pool_size must be at least 1."
            )

    def max_connections(self) -> int:
        """What one process can open across every named instance."""
        total = 0
        for name in self.resolved_connections():
            options = self.engine_options(name)
            total += int(options["pool_size"]) + int(options["max_overflow"])
        return total

    def describe_connections(self) -> list[dict[str, Any]]:
        """One entry per instance, carrying variable names and never values."""
        default = self.default_name()
        return [
            {
                "name": name,
                "role": "replica" if connection.read_only else "primary",
                "default": name == default,
                "dsn_env": connection.dsn_env or self.env_var_for(name),
                "pool_size": self.engine_options(name)["pool_size"],
                "max_overflow": self.engine_options(name)["max_overflow"],
            }
            for name, connection in self.resolved_connections().items()
        ]


def _unique_statement_name() -> str:
    """A prepared statement name no other process will choose.

    asyncpg numbers its statements with a per-process counter, so two workers
    sharing one pooled server connection would both prepare
    ``__asyncpg_stmt_1__`` and the second would fail with ``already exists``.
    """
    import uuid

    return f"__asyncpg_{uuid.uuid4().hex}__"


def connect_args_for(
    dsn: str, *, session_timezone: str, read_only: bool = False, pgbouncer: bool = False
) -> dict[str, Any]:
    """asyncpg startup parameters for one engine, or ``{}`` for another driver.

    Sent in the startup packet rather than as a ``SET`` after connect, and that
    is the point: a pooled connection is checked out mid-life, so a statement
    that ran once when the socket opened is one ``DISCARD ALL``, one
    ``RESET ALL`` or one pgbouncer server-reset away from being gone, and the
    session silently falls back to the server's zone. A startup parameter is
    part of what the connection *is*, and asyncpg replays it on reconnect.
    """
    server_settings: dict[str, str] = {}
    if session_timezone:
        server_settings["timezone"] = session_timezone
    if read_only:
        # The ORM guard in `read_session_dependency` cannot see raw SQL. This
        # one is the server's, so an INSERT smuggled through
        # `session.execute(text(...))` fails on the replica instead of
        # succeeding on a database that is about to be overwritten by WAL.
        server_settings["default_transaction_read_only"] = "on"
    if not dsn.startswith(ASYNCPG_PREFIX):
        return {}
    args: dict[str, Any] = {}
    if server_settings:
        # Through PgBouncer these are startup parameters it has to forward:
        # `timezone` is one it tracks natively; `default_transaction_read_only`
        # needs `track_extra_parameters` (1.20+), or the connection is refused
        # with "unsupported startup parameter" -- loudly, which is the right
        # failure for a replica guard.
        args["server_settings"] = server_settings
    if pgbouncer:
        # Transaction pooling: the next transaction may run on another server
        # connection, where a statement cached on this one does not exist.
        args["statement_cache_size"] = 0
        args["prepared_statement_cache_size"] = 0
        args["prepared_statement_name_func"] = _unique_statement_name
    return args


_FRESH = "jfast_fresh"


def guarded_connect(
    name: str, timeout: float, breaker: CircuitBreaker | None = None
) -> Callable[..., Any]:
    """``asyncpg.connect`` that fails as a 503 when the server is not there.

    The driver raises a bare ``TimeoutError`` or ``OSError`` for a server that
    is down, and SQLAlchemy passes both through untranslated, so they reached
    the unhandled handler as 500s. Translated here, where it is certain the
    error came from connecting to *this* database and not from anything else
    the request did.
    """

    async def connect(*args: Any, **kwargs: Any) -> Any:
        import asyncpg  # type: ignore[import-untyped]

        from jfastframework.http.errors import CircuitOpenError

        permit = None
        if breaker is not None:
            try:
                permit = breaker.acquire()
            except CircuitOpenError as exc:
                raise DatabaseUnavailableError(
                    f"database {name!r} failed its last connection attempts; the next "
                    f"is in {exc.retry_after:.1f}s",
                    retry_after=round(exc.retry_after, 3),
                ) from None
        try:
            connection = await asyncpg.connect(*args, **kwargs)
        except asyncio.CancelledError:
            if breaker is not None and permit is not None:
                breaker.release(permit)
            raise
        except (
            TimeoutError,
            OSError,
            asyncpg.exceptions.CannotConnectNowError,
            asyncpg.exceptions.TooManyConnectionsError,
            asyncpg.exceptions.ConnectionDoesNotExistError,
        ) as exc:
            if breaker is not None and permit is not None:
                breaker.record(permit, failed=True)
            detail = str(exc) or f"no answer within {timeout}s"
            raise DatabaseUnavailableError(
                f"database {name!r} is not accepting connections ({type(exc).__name__}: {detail})"
            ) from exc
        except Exception:
            # A wrong password or a missing database is an answer: the server
            # is there. The breaker is for servers that are not.
            if breaker is not None and permit is not None:
                breaker.record(permit, failed=False)
            raise
        if breaker is not None and permit is not None:
            breaker.record(permit, failed=False)
        return connection

    return connect


def install_bounded_ping(engine: Any, timeout: float) -> None:
    """``pool_pre_ping`` with a deadline.

    SQLAlchemy's pre-ping has none: it runs ``BEGIN``/``;``/``ROLLBACK`` under
    whatever ``command_timeout`` the connection has -- none, by default -- so a
    pooled connection to a database that stopped answering hung the request
    that borrowed it for as long as the socket lived. This ping answers within
    ``timeout`` or the connection is terminated and SQLAlchemy opens a new one,
    which ``connect_timeout`` bounds.

    A simple-protocol ``SELECT 1``: no prepared statement, so it works behind
    PgBouncer in transaction mode, and outside a transaction for the same
    reason. A connection that was just opened is not pinged -- it has just
    proved itself.
    """
    from sqlalchemy import event
    from sqlalchemy import exc as sa_exc
    from sqlalchemy.util import await_only

    pool = engine.sync_engine.pool

    @event.listens_for(pool, "connect")
    def _mark_fresh(dbapi_connection: Any, record: Any) -> None:
        record.info[_FRESH] = True

    @event.listens_for(pool, "checkout")
    def _ping(dbapi_connection: Any, record: Any, proxy: Any) -> None:
        if record.info.pop(_FRESH, False):
            return
        driver = dbapi_connection.driver_connection
        try:
            await_only(asyncio.wait_for(driver.execute("SELECT 1"), timeout))
        except Exception as exc:
            # Terminated, not closed: a graceful close is a round trip to the
            # server that just failed to answer one.
            driver.terminate()
            raise sa_exc.DisconnectionError(
                f"pool ping got no answer within {timeout}s: {exc!r}"
            ) from exc


def build_engine(
    dsn: str,
    *,
    name: str,
    options: dict[str, Any],
    connect_args: dict[str, Any],
    connect_timeout: float,
    ping_timeout: float,
    command_timeout: float = 0.0,
    breaker_failures: int = 0,
    breaker_cool_down: float = 5.0,
) -> Any:
    """One engine with the framework's deadlines, for asyncpg; plain for any other driver."""
    from sqlalchemy.ext.asyncio import create_async_engine

    options = dict(options)
    args = dict(connect_args)
    bounded_ping = False
    if dsn.startswith(ASYNCPG_PREFIX):
        args["timeout"] = connect_timeout
        if command_timeout:
            args["command_timeout"] = command_timeout
        # Read by SQLAlchemy's asyncpg adapter in place of `asyncpg.connect`.
        breaker = (
            CircuitBreaker(
                f"database {name}",
                BreakerPolicy(failure_threshold=breaker_failures, cool_down=breaker_cool_down),
            )
            if breaker_failures > 0
            else None
        )
        args["async_creator_fn"] = guarded_connect(name, connect_timeout, breaker)
        bounded_ping = bool(options.get("pool_pre_ping"))
        if bounded_ping:
            options["pool_pre_ping"] = False
    if args:
        options["connect_args"] = args
    engine = create_async_engine(dsn, **options)
    if bounded_ping:
        install_bounded_ping(engine, ping_timeout)
    return engine


def is_unavailable(exc: BaseException) -> bool:
    """Whether a SQLAlchemy error means "the database is not there right now"."""
    from sqlalchemy import exc as sa_exc

    if isinstance(exc, DatabaseUnavailableError | sa_exc.TimeoutError):
        return True
    if isinstance(exc, sa_exc.DBAPIError):
        return bool(exc.connection_invalidated) or isinstance(exc, sa_exc.InterfaceError)
    return False


async def _unavailable_handler(request: Request, exc: Exception) -> Response:
    """503 for a lost connection or a full pool; anything else stays a 500."""
    from jfastframework.errors import problem_response

    if isinstance(exc, DatabaseUnavailableError):
        return problem_response(exc, request)
    if not is_unavailable(exc):
        # Re-raised to the outermost handler, which renders the 500 exactly as
        # it would have without this one.
        raise exc
    logger.warning("database unavailable: %s", exc)
    return problem_response(
        DatabaseUnavailableError(
            "the database is not answering; retry shortly"
            if not isinstance(exc, _pool_timeout_class())
            else "every database connection is in use; retry shortly"
        ),
        request,
    )


def _pool_timeout_class() -> type[Exception]:
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    return PoolTimeout


async def server_timezone(dsn: str, *, session_timezone: str = "UTC") -> tuple[str, str]:
    """``(what an unpinned client computes in, what this service computes in)``.

    Written for ``jfast doctor``, and it takes a DSN rather than an engine
    because the interesting value cannot be read through a pinned connection:
    a startup parameter *becomes* the session's reset value, so
    ``pg_settings.reset_val`` reports ``UTC`` on a server configured with
    anything. The only honest way to learn what the server hands out is to
    connect the way everything else does -- psql, a BI tool, a migration run by
    hand -- and ask.

    The pair is the diagnosis. Equal and ``UTC``: nothing to say. Different:
    this service is right and every other client of that database is answering
    a different day, which is worth a warning even though nothing is broken
    here.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    query = text("SELECT current_setting('TimeZone')")
    answers: list[str] = []
    for connect_args in ({}, connect_args_for(dsn, session_timezone=session_timezone)):
        engine = create_async_engine(dsn, connect_args=connect_args)
        try:
            async with engine.connect() as conn:
                answers.append(str(await conn.scalar(query)))
        finally:
            await engine.dispose()
    return answers[0], answers[1]


class DatabaseRegistry:
    """Every database instance this service can reach, by name."""

    def __init__(
        self,
        *,
        engines: dict[str, Any],
        sessionmakers: dict[str, Any],
        default: str,
        read_only: tuple[str, ...],
        settings: DatabaseSettings,
    ) -> None:
        self._engines = engines
        self._sessionmakers = sessionmakers
        self._default = default
        self._read_only = read_only
        self._settings = settings
        self._next_replica = 0
        self.tenants: TenantEngines | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._engines)

    @property
    def default_name(self) -> str:
        return self._default

    @property
    def replica_names(self) -> tuple[str, ...]:
        return self._read_only

    @property
    def split_enabled(self) -> bool:
        return bool(self._settings.read_write_split and self._read_only)

    def engine(self, name: str | None = None) -> Any:
        return self._engines[self._resolve(name)]

    def sessionmaker(self, name: str | None = None) -> Any:
        return self._sessionmakers[self._resolve(name)]

    def read_name(self, *, pinned: bool) -> str:
        """Which instance serves a read. Pinned reads are writes' shadow."""
        if pinned or not self.split_enabled:
            return self._default
        index = self._next_replica % len(self._read_only)
        self._next_replica += 1
        return self._read_only[index]

    def describe(self) -> list[dict[str, Any]]:
        return self._settings.describe_connections()

    def _resolve(self, name: str | None) -> str:
        if name is None:
            return self._default
        if name not in self._engines:
            known = ", ".join(self._engines) or "none"
            raise PluginError(f"No database connection named {name!r}. Known: {known}.")
        return name

    async def dispose(self) -> None:
        for engine in self._engines.values():
            await engine.dispose()
        if self.tenants is not None:
            await self.tenants.dispose()


class TenantDatabase:
    """One tenant's engine, and the count of requests currently holding it."""

    def __init__(self, tenant: str, engine: Any) -> None:
        self.tenant = tenant
        self.engine = engine
        self.leases = 0
        self.closing = False
        self._sessionmaker: Any = None

    @property
    def sessionmaker(self) -> Any:
        if self._sessionmaker is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        return self._sessionmaker


class TenantEngines:
    """A bounded LRU of per-tenant engines.

    The obvious implementation is a dict keyed by tenant, and it takes the
    database down: 200 tenants at ``pool_size = 10`` is 2000 connections
    against a server whose default ceiling is 100. So the map has a ceiling,
    the ceiling is a number (``max_connections``) rather than a hope, and
    eviction never closes an engine a request is still holding -- an evicted
    entry leaves the map immediately, so nothing new checks it out, and is
    disposed when the last lease is released.

    When every engine in a full map is in use, a new tenant raises rather than
    opening engine ``max_engines + 1``. Going past the ceiling under load is
    the failure this class exists to prevent, and a 503 is recoverable in a way
    that a connection storm is not.
    """

    def __init__(
        self,
        *,
        resolve: Callable[[str], str],
        create: Callable[[str, str], Any] | None = None,
        max_engines: int = 25,
        pool_size: int = 2,
        max_overflow: int = 2,
        pool_recycle: int = 1800,
        pool_pre_ping: bool = True,
        echo: bool = False,
        session_timezone: str = "UTC",
        pgbouncer: bool = False,
        connect_timeout: float = 10.0,
        ping_timeout: float = 2.0,
        command_timeout: float = 0.0,
        breaker_failures: int = 2,
        breaker_cool_down: float = 5.0,
    ) -> None:
        self._resolve = resolve
        self._create = create or self._build_engine
        self._max_engines = max_engines
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_recycle = pool_recycle
        self._pool_pre_ping = pool_pre_ping
        self._echo = echo
        self._session_timezone = session_timezone
        self._pgbouncer = pgbouncer
        self._connect_timeout = connect_timeout
        self._ping_timeout = ping_timeout
        self._command_timeout = command_timeout
        self._breaker_failures = breaker_failures
        self._breaker_cool_down = breaker_cool_down
        self._entries: OrderedDict[str, TenantDatabase] = OrderedDict()
        self.evictions = 0

    # -- what the ceiling is -------------------------------------------

    @property
    def max_engines(self) -> int:
        return self._max_engines

    @property
    def max_connections(self) -> int:
        return self._max_engines * (self._pool_size + self._max_overflow)

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def tenants(self) -> tuple[str, ...]:
        """Least recently used first, which is eviction order."""
        return tuple(self._entries)

    # -- leases ---------------------------------------------------------

    @asynccontextmanager
    async def lease(self, tenant: str) -> AsyncIterator[TenantDatabase]:
        entry = await self._acquire(tenant)
        try:
            yield entry
        finally:
            await self._release(entry)

    @asynccontextmanager
    async def session(self, tenant: str) -> AsyncIterator[Any]:
        async with self.lease(tenant) as entry, entry.sessionmaker() as session:
            yield session

    async def evict(self, tenant: str) -> None:
        entry = self._entries.pop(tenant, None)
        if entry is not None:
            await self._close(entry)

    async def dispose(self) -> None:
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await self._close(entry)

    # -- internals ------------------------------------------------------

    async def _acquire(self, tenant: str) -> TenantDatabase:
        entry = self._checkout(tenant)
        if entry is not None:
            return entry

        # `_make_room` disposes an evicted engine, which suspends, so a second
        # request for the same new tenant can arrive in the gap and build the
        # engine first. Both halves of that -- the loop bailing out, and this
        # second look -- are what keep the loser from either leaking an engine
        # nothing points at or reporting a full map that is not full.
        await self._make_room(tenant)
        entry = self._checkout(tenant)
        if entry is not None:
            return entry

        entry = TenantDatabase(tenant, self._create(tenant, self._dsn_for(tenant)))
        self._entries[tenant] = entry
        entry.leases += 1
        return entry

    def _checkout(self, tenant: str) -> TenantDatabase | None:
        entry = self._entries.get(tenant)
        if entry is None:
            return None
        self._entries.move_to_end(tenant)
        entry.leases += 1
        return entry

    async def _release(self, entry: TenantDatabase) -> None:
        entry.leases -= 1
        if entry.leases <= 0 and entry.closing:
            entry.closing = False
            await entry.engine.dispose()

    async def _make_room(self, tenant: str) -> None:
        while len(self._entries) >= self._max_engines:
            # Somebody else built it while this call was disposing an engine.
            # The map is full of exactly what was wanted, so it is not full.
            if tenant in self._entries:
                return
            victim = next((e for e in self._entries.values() if e.leases == 0), None)
            if victim is None:
                raise TenantPoolExhausted(
                    f"{len(self._entries)} tenant databases are open and every one "
                    f"is serving a request, so a new tenant cannot be admitted "
                    f"without going past max_engines={self._max_engines} "
                    f"({self.max_connections} connections). Raise "
                    f"tenant_max_engines, or lower the concurrency reaching it."
                )
            del self._entries[victim.tenant]
            self.evictions += 1
            await self._close(victim)

    async def _close(self, entry: TenantDatabase) -> None:
        if entry.leases > 0:
            # Out of the map already, so nothing new checks it out. Disposing
            # now closes the connection under a query that is still running.
            entry.closing = True
            return
        await entry.engine.dispose()

    def _dsn_for(self, tenant: str) -> str:
        try:
            dsn = self._resolve(tenant)
        except Exception as exc:
            # Any resolver failure has the same answer: the service has not
            # said where this tenant's data lives.
            raise PluginError(self._unresolved(tenant, exc)) from exc
        if not dsn:
            raise PluginError(self._unresolved(tenant, None))
        return dsn

    @staticmethod
    def _unresolved(tenant: str, exc: Exception | None) -> str:
        reason = f" ({exc})" if exc is not None else ""
        return (
            f"no database DSN for tenant {tenant!r}{reason}. Set [plugin.database] "
            f"tenant_dsn_template or tenant_dsn_env_template, or publish a "
            f"resolver with DatabaseRegistry.tenants.set_resolver()."
        )

    def set_resolver(self, resolve: Callable[[str], str]) -> None:
        self._resolve = resolve

    def _build_engine(self, tenant: str, dsn: str) -> Any:
        # A tenant database is a database like any other: the report it answers
        # must not depend on which server that tenant landed on, and it gets
        # the same deadlines.
        return build_engine(
            dsn,
            name=f"tenant:{tenant}",
            options={
                "echo": self._echo,
                "pool_size": self._pool_size,
                "max_overflow": self._max_overflow,
                "pool_pre_ping": self._pool_pre_ping,
                "pool_recycle": self._pool_recycle,
            },
            connect_args=connect_args_for(
                dsn, session_timezone=self._session_timezone, pgbouncer=self._pgbouncer
            ),
            connect_timeout=self._connect_timeout,
            ping_timeout=self._ping_timeout,
            command_timeout=self._command_timeout,
            breaker_failures=self._breaker_failures,
            breaker_cool_down=self._breaker_cool_down,
        )


def tenant_resolver(settings: DatabaseSettings) -> Callable[[str], str]:
    """Tenant to DSN, from configuration. Returns "" when neither is set."""

    def resolve(tenant: str) -> str:
        slug = tenant.replace("-", "_")
        if settings.tenant_dsn_env_template:
            variable = settings.tenant_dsn_env_template.format(tenant=slug.upper())
            value = os.environ.get(variable, "")
            if value:
                return value
        if settings.tenant_dsn_template:
            return settings.tenant_dsn_template.format(tenant=slug)
        return ""

    return resolve


# -- the pin -------------------------------------------------------------
#
# Replica lag is not a flag you can turn off. Save a row, redirect, read from
# the replica, and the row is not there yet -- an intermittent 404 that appears
# under load and never reproduces on a laptop, because a laptop has no replica.
#
# So after a write, that client's reads go to the primary for a bounded window.
# The pin travels with the client, as a cookie and as a header, rather than
# living in a table in this process: a redirect can land on any replica of this
# service, and a pin the next process cannot see is a pin that silently is not
# there. The client controls the value, which is safe in the only direction it
# can push -- towards the primary, which is always correct -- and the value is
# clamped to `pin_window` from now, so nobody can pin themselves to the primary
# permanently.


def mark_write(request: Request) -> None:
    """Say this request wrote, so this client's next read skips the replica.

    Only needed for a write behind a *safe* method -- a lazy upsert inside a
    ``GET``. Unsafe methods pin on their own: the middleware decides from what
    it can see when the response comes back, and whether a session committed
    is not one of those things.
    """
    request.state.jfast_db_wrote = True


def is_pinned(request: Request) -> bool:
    return float(getattr(request.state, "jfast_db_pinned_until", 0.0)) > time.time()


def _deadline(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _claimed_deadline(request: Request, settings: DatabaseSettings) -> float:
    """The later of the two tokens, clamped so a forged one buys nothing."""
    claimed = max(
        _deadline(request.cookies.get(settings.pin_cookie, "")),
        _deadline(request.headers.get(PIN_HEADER, "")),
    )
    return min(claimed, time.time() + settings.pin_window)


class ReadWritePinMiddleware:
    """Pin a client to the primary for a window after it writes.

    Plain ASGI: the cookie and header go onto ``http.response.start`` as it
    passes, instead of buffering the response through ``BaseHTTPMiddleware``.
    """

    def __init__(self, app: ASGIApp, *, settings: DatabaseSettings, secure: bool = False) -> None:
        self.app = app
        self._settings = settings
        self._secure = secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        state = scope.setdefault("state", {})
        state["jfast_db_pinned_until"] = _claimed_deadline(request, self._settings)
        state["jfast_db_wrote"] = False

        async def send_with_pin(message: Message) -> None:
            if message["type"] == "http.response.start" and self._wrote(
                scope["method"], message["status"], state
            ):
                until = time.time() + self._settings.pin_window
                # A throwaway Response renders the Set-Cookie exactly as
                # starlette would, attributes and quoting included.
                carrier = Response()
                carrier.set_cookie(
                    self._settings.pin_cookie,
                    f"{until:.3f}",
                    max_age=int(self._settings.pin_window) + 1,
                    httponly=True,
                    # Off outside production, where `jfast start` serves plain
                    # HTTP and a secure cookie would never come back.
                    secure=self._secure,
                    samesite="lax",
                    path="/",
                )
                message["headers"] = [
                    *message.get("headers", []),
                    (PIN_HEADER.lower().encode("latin-1"), f"{until:.3f}".encode("latin-1")),
                    *[(k, v) for k, v in carrier.raw_headers if k == b"set-cookie"],
                ]
            await send(message)

        await self.app(scope, receive, send_with_pin)

    def _wrote(self, method: str, status: int, state: dict[str, Any]) -> bool:
        if state.get("jfast_db_wrote", False):
            return True
        if not self._settings.pin_on_unsafe_methods:
            return False
        # The method, not the session. The middleware cannot see whether a
        # session committed; the method is known before the handler runs and
        # covers every write a REST API makes. The cost of the approximation
        # is a POST that read nothing pinning its client for one window, which
        # is load, not incorrectness.
        return method in UNSAFE_METHODS and status < 400


class DatabasePlugin(Plugin):
    meta = PluginMeta(
        name="database",
        version="0.2.0",
        description="Named async SQLAlchemy instances, request-scoped sessions, read/write split.",
        after=("observability",),
        provides=("db.engine", "db.sessionmaker", "db.databases"),
        default_enabled=False,
        extra="jfastframework[db]",
    )
    Settings = DatabaseSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._registry: DatabaseRegistry | None = None

    def register(self, ctx: AppContext) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        settings: DatabaseSettings = self.settings
        connections = settings.resolved_connections()
        default = settings.default_name()
        settings.validate_for_boot()

        engines: dict[str, Any] = {}
        sessionmakers: dict[str, Any] = {}
        for name in connections:
            engine = self._build_engine(name)
            engines[name] = engine
            # expire_on_commit=False keeps ORM objects usable after the request
            # scope commits, which is what response serialisation needs.
            sessionmakers[name] = async_sessionmaker(
                engine, expire_on_commit=False, **self._session_options()
            )

        read_only = tuple(n for n, c in connections.items() if c.read_only)
        if settings.read_write_split and not read_only:
            raise PluginError(
                "[plugin.database] read_write_split is on but no connection is "
                "read_only, so there is nothing to read from. Mark the replica "
                "read_only = true."
            )

        registry = DatabaseRegistry(
            engines=engines,
            sessionmakers=sessionmakers,
            default=default,
            read_only=read_only,
            settings=settings,
        )
        registry.tenants = TenantEngines(
            resolve=tenant_resolver(settings),
            max_engines=settings.tenant_max_engines,
            pool_size=settings.tenant_pool_size,
            max_overflow=settings.tenant_max_overflow,
            pool_recycle=settings.pool_recycle,
            pool_pre_ping=settings.pool_pre_ping,
            echo=settings.echo,
            session_timezone=settings.session_timezone,
            pgbouncer=settings.pgbouncer,
            connect_timeout=settings.connect_timeout,
            ping_timeout=settings.ping_timeout,
            command_timeout=settings.command_timeout,
            breaker_failures=settings.breaker_failures,
            breaker_cool_down=settings.breaker_cool_down,
        )
        self._registry = registry

        ctx.provide("db.engine", engines[default])
        ctx.provide("db.sessionmaker", sessionmakers[default])
        ctx.provide("db.databases", registry)

        # A lost connection or a full pool answers 503 wherever it escapes a
        # route; every other database error keeps its 500.
        from sqlalchemy.exc import DBAPIError
        from sqlalchemy.exc import TimeoutError as PoolTimeout

        ctx.app.add_exception_handler(DBAPIError, _unavailable_handler)
        ctx.app.add_exception_handler(PoolTimeout, _unavailable_handler)

        if settings.read_write_split:
            # Appended rather than added: `add_middleware` puts a middleware
            # outermost, which would read the pin before auth has resolved a
            # principal and before tenancy has run.
            from starlette.middleware import Middleware

            ctx.app.user_middleware.append(
                Middleware(
                    ReadWritePinMiddleware,
                    settings=settings,
                    secure=ctx.settings.is_production,
                )
            )

    def _session_options(self) -> dict[str, Any]:
        settings: DatabaseSettings = self.settings
        if not settings.rls:
            return {}
        from jfastframework.db.rls import TenantScopedSession

        return {"sync_session_class": TenantScopedSession}

    async def _check_rls_role(self, ctx: AppContext) -> None:
        from jfastframework.db.rls import role_problem

        assert self._registry is not None
        try:
            problem = await role_problem(self._registry.engine())
        except Exception as exc:  # noqa: BLE001 - an unreachable database is /ready's to report
            # Refusing to boot because the database is not up yet would turn a
            # blip into a crash loop. /ready reports the database; the role is
            # checked on the next start.
            ctx.logger.warning(
                "rls is on and the database role could not be checked",
                extra={"error": str(exc)},
            )
            return
        if problem is None:
            return
        message = (
            f"[plugin.database] rls is on, but {problem}: every policy is ignored and "
            f"each tenant can read the others' rows. Connect as a role without "
            f"SUPERUSER or BYPASSRLS (docs/multitenancy.md shows the grants)."
        )
        if ctx.settings.is_production:
            raise PluginError(message)
        ctx.logger.warning(message)

    async def startup(self, ctx: AppContext) -> None:
        if self.settings.rls:
            await self._check_rls_role(ctx)
        # Here rather than in `register`: routers are mounted after plugins
        # register, and by startup every route the service serves exists.
        violations = session_scope_violations(ctx.app)
        if violations:
            raise PluginError(
                "these routes open a database session that would commit after the "
                "response is sent, so a failed commit still answers 2xx and the next "
                "request can read before the write is visible:\n  "
                + "\n  ".join(violations)
                + "\nDepend on DbSession / ReadSession / TenantSession from "
                'jfastframework.plugins.builtin.database, or pass scope="function" '
                "to Depends(...)."
            )
        suspected = suspected_scope_violations(ctx.app)
        if suspected:
            ctx.logger.warning(
                "these routes reach a dependency of this service that commits after its "
                'yield without scope="function", so the commit runs after the response '
                "is sent. Mark it @transactional and scope it, or scope it:\n  "
                + "\n  ".join(suspected)
            )

    async def shutdown(self, ctx: AppContext) -> None:
        if self._registry is not None:
            await self._registry.dispose()

    async def health(self, ctx: AppContext) -> HealthReport:
        from sqlalchemy import text

        if self._registry is None:
            return HealthReport.fail("engine not initialised")

        unreachable: list[str] = []
        for name in self._registry.names:
            try:
                async with self._registry.engine(name).connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception as exc:  # noqa: BLE001
                unreachable.append(f"{name}: {exc}")
        if unreachable:
            return HealthReport.fail("database unreachable -- " + "; ".join(unreachable))
        return HealthReport.ok("database reachable", connections=list(self._registry.names))

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        settings: DatabaseSettings = self.settings
        described["connections"] = settings.describe_connections()
        described["max_connections"] = settings.max_connections()
        described["read_write_split"] = settings.read_write_split
        described["tenant_max_connections"] = settings.tenant_max_engines * (
            settings.tenant_pool_size + settings.tenant_max_overflow
        )
        return described

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: DatabaseSettings = self.settings
        if not settings.include_infra:
            return []

        connections = settings.resolved_connections()
        default = settings.default_name()
        wanted = [
            (name, connection)
            for name, connection in connections.items()
            if connection.include_infra is not False
        ]
        offsets = self._port_offsets(wanted, default)

        services: list[InfraService] = []
        for name, connection in wanted:
            user = connection.user or settings.user
            secret = (
                "POSTGRES_PASSWORD"
                if name == default
                else f"POSTGRES_{name.upper().replace('-', '_')}_PASSWORD"
            )
            container = "postgres" if name == default else f"postgres-{name}"
            volume = "postgres_data" if name == default else f"postgres_{name}_data"
            database = connection.database or settings.database
            services.append(
                InfraService(
                    name=container,
                    image=connection.image or settings.image,
                    port_offset=offsets[name],
                    internal_port=5432,
                    environment={
                        "POSTGRES_DB": database,
                        "POSTGRES_USER": user,
                        "POSTGRES_PASSWORD": "${" + secret + ":?set " + secret + "}",
                    },
                    # The variable this very connection reads, so a named
                    # connection reaches its own container and not the default's.
                    client_env={
                        settings.env_var_for(name): (
                            f"postgresql+asyncpg://{user}:${{{secret}}}@{container}:5432/{database}"
                        )
                    },
                    volumes=[f"{volume.replace('-', '_')}:/var/lib/postgresql/data"],
                    # Docker gives a container 64 MB of /dev/shm, which is where
                    # a parallel query puts its working memory. Without this the
                    # failure is `could not resize shared memory segment`: a
                    # random 500 on exactly the queries worth parallelising,
                    # invisible until the tables are big enough for the planner
                    # to try one. The workspace generator sets the same value
                    # from `resources.POSTGRES_SHM_SIZE`; this is the
                    # single-service path through `jfast deploy compose`.
                    shm_size=POSTGRES_SHM_SIZE,
                    healthcheck={
                        "test": ["CMD-SHELL", f"pg_isready -U {user}"],
                        "interval": "5s",
                        "timeout": "3s",
                        "retries": 10,
                    },
                )
            )
        return services

    # -- internals ------------------------------------------------------

    def _build_engine(self, name: str) -> Any:
        settings: DatabaseSettings = self.settings
        dsn = settings.dsn_for(name)
        connect, ping, command = settings.deadlines(name)
        return build_engine(
            dsn,
            name=name,
            options=settings.engine_options(name),
            connect_args=connect_args_for(
                dsn,
                session_timezone=settings.session_timezone,
                read_only=settings.resolved_connections()[name].read_only,
                pgbouncer=settings.behind_pgbouncer(name),
            ),
            connect_timeout=connect,
            ping_timeout=ping,
            command_timeout=command,
            breaker_failures=settings.breaker_failures,
            breaker_cool_down=settings.breaker_cool_down,
        )

    def _port_offsets(
        self, wanted: list[tuple[str, ConnectionSettings]], default: str
    ) -> dict[str, int]:
        settings: DatabaseSettings = self.settings
        chosen: dict[str, int] = {}
        for name, connection in wanted:
            if connection.port_offset is not None:
                chosen[name] = connection.port_offset
            elif name == default:
                chosen[name] = settings.port_offset

        free = [offset for offset in DATABASE_PORT_OFFSETS if offset not in set(chosen.values())]
        for name, _ in wanted:
            if name in chosen:
                continue
            if not free:
                taken = ", ".join(str(offset) for offset in sorted(set(chosen.values())))
                raise ValueError(
                    f"database connection {name!r} has no port left in the "
                    f"service's {PORT_BLOCK_SIZE}-port block: offsets {taken} are "
                    f"already taken. Give it an explicit port_offset, or set "
                    f"include_infra = false if the instance is managed elsewhere."
                )
            chosen[name] = free.pop(0)
        return chosen


def _registry_of(request: Request) -> DatabaseRegistry:
    # get_context() rather than request.app.state.jfast: reaching into state
    # directly raises `KeyError: 'jfast'` on an app this framework did not
    # build, which tells the reader nothing.
    from jfastframework.app import get_context

    return get_context(request.app).require("db.databases", DatabaseRegistry)


async def session_dependency(request: Request) -> AsyncIterator[Any]:
    """FastAPI dependency yielding a session on the primary, committed on exit.

    Depend on it through ``DbSession`` (or ``Depends(session_dependency,
    scope="function")``), never a bare ``Depends(session_dependency)``. The
    scope decides *when* the commit below runs. FastAPI's default for a
    ``yield`` dependency is ``"request"``, which runs it after the response has
    been sent: a commit that fails -- a deferred constraint, a serialisation
    failure, a dropped connection -- has already been answered 201, and a
    client quick enough to read its own write can get there before the commit
    and see nothing. ``"function"`` commits when the endpoint returns, before
    the response exists, so a failed commit is the 500 it should be. The
    database plugin refuses to start while a route uses any other scope.

    ``request`` is annotated ``Request`` and must stay that way. FastAPI
    decides what a dependency parameter *is* from its annotation, and with
    ``Any`` it concludes the only thing left: a required query parameter.
    Every route depending on a session then answers 422 to every call,
    asking for ``?request=``. There is no runtime error to find, because
    nothing is wrong at runtime -- the schema is simply wrong.

    Usage::

        from fastapi import Depends
        from jfastframework.plugins.builtin.database import session_dependency

        @router.get("/items")
        async def list_items(session = Depends(session_dependency)):
            ...
    """
    from jfastframework.app import get_context

    ctx = get_context(request.app)
    sessionmaker: Any = ctx.require("db.sessionmaker")
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def read_session_dependency(request: Request) -> AsyncIterator[Any]:
    """A session for reads: a replica, unless this client just wrote.

    Falls back to the one database when no replica is configured, so a service
    can use it everywhere and gain the split later by adding a connection.
    """
    databases = _registry_of(request)
    name = databases.read_name(pinned=is_pinned(request))
    # autoflush would send a pending INSERT to the replica on the next query,
    # before the check below ever runs.
    async with databases.sessionmaker(name)(autoflush=False) as session:
        try:
            yield session
            pending = session.sync_session
            if pending.new or pending.dirty or pending.deleted:
                raise ReadOnlySessionError(
                    f"a write reached the read session on {name!r}. Reads and "
                    f"writes are separate sessions: depend on session_dependency "
                    f"for anything that changes a row."
                )
        finally:
            await session.rollback()


async def tenant_session_dependency(request: Request) -> AsyncIterator[Any]:
    """A session on the current tenant's own database.

    The tenant comes from ``request.state.tenant_id``, which the ``tenancy``
    plugin resolves. A request with no tenant is a configuration error here,
    not a database to guess at.
    """
    databases = _registry_of(request)
    tenants = databases.tenants
    if tenants is None:
        raise PluginError("the database plugin published no tenant engine map")

    tenant = getattr(request.state, "tenant_id", None)
    if not tenant:
        raise PluginError(
            "this request resolved to no tenant, so there is no per-tenant "
            "database to open. Enable the tenancy plugin, and set "
            "[plugin.tenancy] require_tenant = true on routes that need one."
        )

    async with tenants.session(str(tenant)) as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# -- what routes depend on ------------------------------------------------
#
# The dependencies above only commit before the response when they are
# function-scoped, and the scope is chosen where they are used, not where they
# are defined. These aliases are the one spelling that is always right:
#
#     async def create(payload: ItemCreate, session: DbSession) -> ItemRead: ...

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession as _Session
else:
    _Session = Any

DbSession = Annotated[_Session, Depends(session_dependency, scope="function")]
ReadSession = Annotated[_Session, Depends(read_session_dependency, scope="function")]
TenantSession = Annotated[_Session, Depends(tenant_session_dependency, scope="function")]

_TRANSACTIONAL = (session_dependency, read_session_dependency, tenant_session_dependency)
_MARK = "__jfast_transactional__"

F = TypeVar("F", bound=Callable[..., Any])


def transactional(dependency: F) -> F:
    """Mark a session dependency of your own, so the scope check covers it.

    A service that opens its own session -- to set extra row-level security
    values, say -- commits in its own ``yield`` dependency, and that commit
    runs after the response exactly like the framework's would. Marked, the
    database plugin holds it to the same rule: every route must depend on it
    with ``scope="function"``, or the service does not start::

        @transactional
        async def session_with_rls(request: Request) -> AsyncIterator[AsyncSession]:
            ...

        SessionRLS = Annotated[AsyncSession, Depends(session_with_rls, scope="function")]
    """
    setattr(dependency, _MARK, True)
    return dependency


def _is_transactional(call: Any) -> bool:
    return call in _TRANSACTIONAL or bool(getattr(call, _MARK, False))


def _walk(app: Any) -> Iterator[tuple[Any, Any]]:
    """Every (route, dependant) pair below every API route, depth first."""
    from fastapi.routing import APIRoute

    for route in getattr(app, "routes", ()):
        if not isinstance(route, APIRoute):
            continue
        pending = [route.dependant]
        while pending:
            dependant = pending.pop()
            for sub in dependant.dependencies:
                yield route, sub
                pending.append(sub)


def _label(route: Any, call: Any) -> str:
    methods = ",".join(sorted(route.methods or ()))
    return f"{methods} {route.path} -> {getattr(call, '__name__', repr(call))}"


def session_scope_violations(app: Any) -> list[str]:
    """Routes that reach a session dependency with any scope but ``"function"``.

    The framework's three session dependencies, and any of the service's own
    marked with :func:`transactional`. Walks every route's dependency tree, so
    a session two levels down -- inside a ``get_service`` -- is found as well
    as one on the endpoint.
    """
    return sorted(
        {
            _label(route, sub.call)
            for route, sub in _walk(app)
            if _is_transactional(sub.call) and sub.scope != "function"
        }
    )


def _commits_after_yield(call: Any) -> bool:
    """Whether a generator dependency's source commits after it yields."""
    if not (inspect.isasyncgenfunction(call) or inspect.isgeneratorfunction(call)):
        return False
    try:
        source = inspect.getsource(call)
    except (OSError, TypeError):
        return False
    _, _, after = source.partition("yield")
    return ".commit(" in after


def suspected_scope_violations(app: Any) -> list[str]:
    """Unmarked dependencies that look like they commit after the response.

    A heuristic -- a request-scoped generator dependency whose code calls
    ``.commit(`` after its ``yield`` -- so it warns rather than refusing:
    the service knows whether that write may happen after the response, and
    either marks the dependency or scopes it.
    """
    return sorted(
        {
            _label(route, sub.call)
            for route, sub in _walk(app)
            if not _is_transactional(sub.call)
            and sub.scope != "function"
            and _commits_after_yield(sub.call)
        }
    )
