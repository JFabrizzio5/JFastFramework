"""File storage: named disks, public and private.

    [plugin.storage]
    default = "public"
    serve_local = true          # development only; Caddy or a CDN in production

    [plugin.storage.disks.public]
    driver = "local"
    root = "storage/public"
    visibility = "public"

    [plugin.storage.disks.private]
    driver = "local"
    root = "storage/private"
    visibility = "private"

    [plugin.storage.disks.uploads]
    driver = "s3"
    bucket = "uploads"
    endpoint_url = "http://localhost:9006"   # MinIO
    force_path_style = true

Code writes to a *named disk*; where that disk lives is configuration. The same
handler writes to the filesystem in development and to S3 in production.

Requires: ``pip install jfastframework[storage]`` (add ``[s3]`` for S3/MinIO).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from starlette.responses import Response

from jfastframework.errors import ForbiddenError, NotFoundError, PluginError
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)
from jfastframework.storage.base import (
    DRIVER_KEYS,
    DiskConfig,
    FileNotFound,
    InvalidKey,
    StorageBackend,
    StorageError,
    StoredFile,
    UrlSigner,
    normalise_key,
    sanitised_download_headers,
)
from jfastframework.storage.local import LocalStorage
from jfastframework.storage.pipeline import STEP_FACTORIES, build_pipeline
from jfastframework.storage.resolve import DiskLedger, InMemoryLedger, KeyResolver

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.storage")

DRIVERS = ("local", "s3")


class StorageSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_STORAGE_", env_file=".env", extra="ignore")

    default: str = "public"
    disks: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Signs temporary URLs for local disks. S3 disks presign with their own
    # credentials and ignore this.
    signing_key: SecretStr | None = None

    # Serve local disks from this app. Convenient in development; in
    # production Caddy or a CDN should serve the public disk directly, and a
    # Python worker should not be spending its time on static bytes.
    serve_local: bool = True
    prefix: str = "/storage"

    # Serve `/storage/{key}` as well as `/storage/{disk}/{key}`, so a stored
    # URL survives the object moving to another disk. Off by default: it adds
    # a URL shape to every route table, and a service that never migrates does
    # not need one.
    resolve_by_key: bool = False
    resolve_strategy: str = "recorded"
    # Probe order, newest disk first. The first entry is also the disk
    # copy_on_read copies into.
    read_order: list[str] = Field(default_factory=list)
    copy_on_read: bool = False

    # MinIO in the generated compose file.
    minio_include_infra: bool = False
    minio_port_offset: int = 6


class DiskRegistry:
    """The `storage` provider: `storage.disk("public")`."""

    def __init__(
        self,
        disks: dict[str, StorageBackend],
        default: str,
        *,
        prefix: str = "/storage",
        signer: UrlSigner | None = None,
        resolver: KeyResolver | None = None,
        ledger: DiskLedger | None = None,
    ) -> None:
        self._disks = disks
        self._default = default
        self._prefix = prefix.rstrip("/")
        self._signer = signer
        self._resolver = resolver
        self._ledger = ledger

    def disk(self, name: str | None = None) -> StorageBackend:
        chosen = name or self._default
        try:
            return self._disks[chosen]
        except KeyError:
            available = ", ".join(sorted(self._disks)) or "<none>"
            raise PluginError(
                f"No storage disk named {chosen!r}. Available: {available}."
            ) from None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._disks))

    def describe(self) -> dict[str, Any]:
        described: dict[str, Any] = {
            "default": self._default,
            "disks": {
                name: {"driver": type(disk).__name__, "visibility": disk.visibility}
                for name, disk in self._disks.items()
            },
        }
        if self._resolver is not None:
            described["resolve"] = self._resolver.describe()
        return described

    # -- resolution ----------------------------------------------------

    def use_ledger(self, ledger: DiskLedger) -> None:
        """Swap in the application's own record of where objects live.

        The default ledger is in memory, which is right for tests and wrong
        for anything with two replicas. An application that already stores a
        row per file has the answer in a column and should say so here.
        """
        self._ledger = ledger
        if self._resolver is not None:
            self._resolver.ledger = ledger

    async def record(self, key: str, disk: str) -> None:
        if self._ledger is None:
            raise PluginError(
                "storage has no ledger; set [plugin.storage] resolve_by_key = true "
                "or install one with storage.use_ledger()"
            )
        await self._ledger.record(normalise_key(key), disk)

    async def locate(self, key: str) -> str | None:
        if self._ledger is None:
            return None
        return await self._ledger.locate(normalise_key(key))

    async def resolve(self, key: str) -> tuple[str, StorageBackend]:
        """Which disk holds `key`, without being told. Raises `FileNotFound`."""
        if self._resolver is None:
            raise PluginError(
                "storage resolution is off; set [plugin.storage] resolve_by_key = true"
            )
        name = await self._resolver.locate(normalise_key(key))
        return name, self._disks[name]

    def stable_url(self, key: str) -> str:
        """A URL that names the object and not the disk it happens to be on."""
        return f"{self._prefix}/{normalise_key(key)}"

    async def stable_temporary_url(self, key: str, *, expires_in: int = 300) -> str:
        """The signed form of `stable_url`.

        Signed with the service's own key rather than the disk's, because the
        object may be on a different disk by the time the link is followed —
        that is the point of the link.
        """
        if self._signer is None:
            raise PluginError("storage has no signing key; set JFAST_STORAGE_SIGNING_KEY")
        safe = normalise_key(key)
        expires_at = int(time.time()) + expires_in
        signature = self._signer.sign(safe, expires_at)
        return f"{self._prefix}/{safe}?expires={expires_at}&signature={signature}"

    # -- moving between disks -------------------------------------------

    async def copy(self, key: str, source: str, target: str) -> StoredFile:
        """Copy an object to another disk, leaving the original in place."""
        safe = normalise_key(key)
        data = await self.disk(source).get(safe)
        info = await self.disk(source).stat(safe)
        # write(), not put(): the object is already stored. Re-running the
        # target's pipeline would let a rule tightened today reject a file that
        # was legal when it was written, and fail the migration.
        return await self.disk(target).write(
            safe, data, content_type=info.content_type, metadata=info.metadata
        )

    async def move(self, key: str, source: str, target: str) -> StoredFile:
        """Copy, then delete the original, then re-point the ledger.

        In that order on purpose: a crash between the copy and the delete
        leaves two copies, which is recoverable. The other order loses the
        file.
        """
        safe = normalise_key(key)
        stored = await self.copy(safe, source, target)
        await self.disk(source).delete(safe)
        if self._ledger is not None:
            await self._ledger.record(safe, target)
        return stored


DEFAULT_DISKS: dict[str, dict[str, Any]] = {
    "public": {"driver": "local", "root": "storage/public", "visibility": "public"},
    "private": {"driver": "local", "root": "storage/private", "visibility": "private"},
}


class StoragePlugin(Plugin):
    meta = PluginMeta(
        name="storage",
        version="0.1.0",
        description="Named disks over the local filesystem, S3 or MinIO.",
        after=("observability",),
        provides=("storage",),
        default_enabled=False,
        extra="jfastframework[storage]",
    )
    Settings = StorageSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._registry: DiskRegistry | None = None
        self._locals: dict[str, LocalStorage] = {}
        self._signer: UrlSigner | None = None

    def _read_disk_config(self, name: str, raw: dict[str, Any]) -> DiskConfig:
        """Turn one disk's table into a `DiskConfig`, or fail at startup.

        Everything in here is a configuration mistake that would otherwise be
        silent.
        A key nobody reads produces no error and no behaviour, and the only
        symptom is the feature you thought you turned on not being on.
        """
        raw = dict(raw)
        driver = str(raw.get("driver", "local"))
        if driver not in DRIVERS:
            raise PluginError(
                f"storage disk {name!r} has driver {driver!r}; choose from {', '.join(DRIVERS)}"
            )

        step_names = [str(step) for step in raw.pop("pipeline", [])]
        step_config: dict[str, dict[str, Any]] = {}
        for step in step_names:
            block = raw.pop(step, None)
            if block is None:
                continue
            if not isinstance(block, dict):
                raise PluginError(
                    f"storage disk {name!r}: [plugin.storage.disks.{name}.{step}] "
                    f"must be a table of that step's settings"
                )
            step_config[step] = dict(block)

        allowed = DRIVER_KEYS[driver]
        unknown = sorted(set(raw) - allowed)
        for key in unknown:
            if key in STEP_FACTORIES:
                raise PluginError(
                    f"storage disk {name!r} configures the {key!r} step but does not run it; "
                    f'add pipeline = ["{key}"] to the disk'
                )
            other = sorted(other for other, keys in DRIVER_KEYS.items() if key in keys)
            hint = (
                f" It belongs to the {', '.join(other)} driver."
                if other
                else f" Valid keys: {', '.join(sorted(allowed))}."
            )
            raise PluginError(
                f"storage disk {name!r} uses driver {driver!r}, which has no setting {key!r}.{hint}"
            )

        return DiskConfig(name=name, pipeline=step_names, pipeline_config=step_config, **raw)

    def _build_disk(self, name: str, raw: dict[str, Any]) -> StorageBackend:
        settings: StorageSettings = self.settings
        config = self._read_disk_config(name, raw)

        try:
            pipeline = build_pipeline(name, config.pipeline, config.pipeline_config)
        except StorageError as exc:
            raise PluginError(str(exc)) from exc

        if config.driver == "local":
            disk = LocalStorage(
                name,
                root=config.root,
                visibility=config.visibility,
                url_prefix=config.url_prefix or f"{settings.prefix}/{name}",
                public_base_url=config.public_base_url,
                signing_key=(
                    settings.signing_key.get_secret_value() if settings.signing_key else ""
                ),
                pipeline=pipeline,
            )
            self._locals[name] = disk
            return disk

        if not config.bucket:
            raise PluginError(f"storage disk {name!r} uses s3 but has no bucket")

        from jfastframework.storage.s3 import S3Storage

        return S3Storage(
            name,
            bucket=config.bucket,
            visibility=config.visibility,
            region=config.region,
            endpoint_url=config.endpoint_url,
            access_key=config.access_key,
            secret_key=config.secret_key,
            force_path_style=config.force_path_style,
            public_base_url=config.public_base_url,
            pipeline=pipeline,
        )

    def register(self, ctx: AppContext) -> None:
        settings: StorageSettings = self.settings
        raw_disks = settings.disks or DEFAULT_DISKS

        disks = {name: self._build_disk(name, dict(raw)) for name, raw in raw_disks.items()}
        if settings.default not in disks:
            raise PluginError(
                f"storage default is {settings.default!r}, which is not a configured disk. "
                f"Configured: {', '.join(sorted(disks)) or '<none>'}"
            )

        private_locals = [
            name for name, disk in self._locals.items() if disk.visibility != "public"
        ]
        if private_locals and settings.signing_key is None:
            # Without a key, temporary_url() raises at call time -- which is
            # discovered by a user hitting a download link, not by a deploy.
            ctx.logger.warning(
                "storage: no signing key, so temporary URLs are unavailable for %s. "
                "Set JFAST_STORAGE_SIGNING_KEY.",
                ", ".join(private_locals),
            )

        signing_key = settings.signing_key.get_secret_value() if settings.signing_key else ""
        self._signer = UrlSigner(signing_key) if signing_key else None

        ledger: DiskLedger | None = None
        resolver: KeyResolver | None = None
        if not settings.resolve_by_key and (settings.read_order or settings.copy_on_read):
            raise PluginError(
                "storage read_order and copy_on_read do nothing while resolve_by_key is "
                "false; set resolve_by_key = true or drop them"
            )
        if settings.resolve_by_key:
            ledger = InMemoryLedger()
            try:
                resolver = KeyResolver(
                    disks,
                    strategy=settings.resolve_strategy,
                    read_order=settings.read_order,
                    copy_on_read=settings.copy_on_read,
                    ledger=ledger,
                )
            except StorageError as exc:
                raise PluginError(str(exc)) from exc
            if resolver.strategy == "recorded":
                # The in-memory ledger does not survive a restart and is not
                # shared between replicas, so a recorded lookup answers 404
                # for everything until the application installs its own.
                ctx.logger.info(
                    "storage: resolving /%s/{key} from an in-memory ledger. "
                    "Call storage.use_ledger() with your own before production.",
                    settings.prefix.strip("/"),
                )

        self._registry = DiskRegistry(
            disks,
            settings.default,
            prefix=settings.prefix,
            signer=self._signer,
            resolver=resolver,
            ledger=ledger,
        )
        ctx.provide("storage", self._registry)

        if settings.serve_local and (self._locals or settings.resolve_by_key):
            ctx.app.include_router(self._build_router(), prefix=settings.prefix, tags=["storage"])
            if ctx.settings.is_production:
                ctx.logger.warning(
                    "storage is serving local disks from the application in production. "
                    "Put Caddy or a CDN in front and set [plugin.storage] serve_local = false."
                )

    def _serve(self, backend: StorageBackend, key: str, request: Request) -> None:
        """Authorise a download. Raises rather than returning a verdict."""
        if backend.visibility == "public":
            return
        # A private disk is reachable only with a signature covering both the
        # key and the expiry. The signature is checked against the service's
        # key rather than the disk's, so a link keeps working after the object
        # moves to a disk that does no signing of its own, such as S3.
        expires = request.query_params.get("expires")
        signature = request.query_params.get("signature")
        if not expires or not signature or self._signer is None:
            raise ForbiddenError("this file requires a signed URL")
        try:
            valid = self._signer.verify(key, int(expires), signature)
        except (ValueError, StorageError):
            valid = False
        if not valid:
            # One message for "expired" and for "forged": telling them apart
            # tells an attacker whether the key exists.
            raise ForbiddenError("this link is invalid or has expired")

    async def _body(self, backend: StorageBackend, key: str, request: Request) -> Response:
        self._serve(backend, key, request)
        try:
            data = await backend.get(key)
            info = await backend.stat(key)
        except InvalidKey as exc:
            raise NotFoundError(str(exc)) from exc
        except FileNotFound as exc:
            raise NotFoundError(f"{key} not found") from exc

        # attachment + nosniff by default: serving an uploaded .html or
        # .svg inline from this origin runs the uploader's script against
        # your users' cookies.
        headers = sanitised_download_headers(key, info.content_type)
        return Response(content=data, headers=headers)

    async def _by_key(self, key: str, request: Request) -> Response:
        assert self._registry is not None
        try:
            _, backend = await self._registry.resolve(key)
        except InvalidKey as exc:
            raise NotFoundError(str(exc)) from exc
        except FileNotFound as exc:
            raise NotFoundError(f"{key} not found") from exc
        return await self._body(backend, key, request)

    def _build_router(self) -> APIRouter:
        settings: StorageSettings = self.settings
        router = APIRouter()

        @router.get("/{disk}/{key:path}", summary="Download a stored file")
        async def download(disk: str, key: str, request: Request) -> Response:
            assert self._registry is not None
            if disk in self._registry.names:
                return await self._body(self._registry.disk(disk), key, request)
            # Not a disk name, so the whole path is the key. A disk name as
            # the first segment always wins; that ambiguity is the price of
            # keeping the older URL shape working.
            if settings.resolve_by_key:
                return await self._by_key(f"{disk}/{key}", request)
            raise NotFoundError(f"no storage disk named {disk!r}")

        if settings.resolve_by_key:

            @router.get("/{key:path}", summary="Download by key, whichever disk holds it")
            async def download_by_key(key: str, request: Request) -> Response:
                return await self._by_key(key, request)

        return router

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._registry is None:
            return HealthReport.fail("storage not initialised")

        problems: list[str] = []
        for name in self._registry.names:
            healthy, detail = await self._registry.disk(name).health()
            if not healthy:
                problems.append(f"{name}: {detail}")

        meta = self._registry.describe()
        if problems:
            return HealthReport.fail("; ".join(problems), **meta)
        return HealthReport.ok(f"{len(self._registry.names)} disk(s)", **meta)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: StorageSettings = self.settings
        if not settings.minio_include_infra:
            return []
        return [
            InfraService(
                name="minio",
                image="minio/minio:RELEASE.2024-10-13T13-34-11Z",
                port_offset=settings.minio_port_offset,
                internal_port=9000,
                command="server /data --console-address :9001",
                environment={
                    "MINIO_ROOT_USER": "jfast",
                    "MINIO_ROOT_PASSWORD": "${MINIO_PASSWORD:?set MINIO_PASSWORD}",
                },
                volumes=["minio_data:/data"],
                healthcheck={
                    "test": ["CMD", "mc", "ready", "local"],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 10,
                },
            )
        ]
