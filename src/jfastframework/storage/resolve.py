"""Finding an object without being told which disk it is on.

A download route of `/storage/{disk}/{key}` bakes the disk name into every
URL ever handed out. That makes moving a file from `local` to `s3` a 404 on
every stored link, which is the whole reason a migration to S3 keeps
getting postponed. `/storage/{key}` names only the object, and the app works
out where it lives.

There are two honest ways to do that and neither is free:

**Recorded.** Whoever wrote the object also wrote down which disk took it, and
resolution is one lookup in something the application already has — a column
on the row that owns the file. Cheap, exact, and it only works for objects
that were recorded, so it does nothing for the files already on the old disk
when you switch this on.

**Probing.** Try the disks in a configured order and take the first that says
`exists()`. It needs no bookkeeping and works on day one, and it costs a round
trip per disk that does *not* have the object — on a miss that is a head
request to every disk in the list before the 404. It is a migration window,
not a steady state.

`copy_on_read` closes the window as traffic flows: when probing finds an
object on an older disk, it is copied to the first disk in `read_order` and
recorded, so the second request for that object is a recorded hit. It turns a
GET into a read plus a write, so the first request for every object costs a
full copy — fine for a few thousand avatars, not for a bucket of video.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from jfastframework.storage.base import FileNotFound, StorageBackend, StorageError

logger = logging.getLogger("jfast.storage")

STRATEGIES = ("recorded", "probe")


@runtime_checkable
class DiskLedger(Protocol):
    """Where an application writes down which disk holds which key."""

    async def record(self, key: str, disk: str) -> None: ...

    async def locate(self, key: str) -> str | None:
        """The disk holding `key`, or None when nothing was recorded for it."""
        ...

    async def forget(self, key: str) -> None: ...


class InMemoryLedger:
    """A ledger that dies with the process.

    Enough for tests and a single-process development server, and wrong for
    anything else: two replicas do not share it, and a restart loses every
    mapping — which for the `recorded` strategy means every stored URL 404s
    until something re-records it. A real deployment points the ledger at the
    column it already has next to the key.
    """

    def __init__(self) -> None:
        self._where: dict[str, str] = {}

    async def record(self, key: str, disk: str) -> None:
        self._where[key] = disk

    async def locate(self, key: str) -> str | None:
        return self._where.get(key)

    async def forget(self, key: str) -> None:
        self._where.pop(key, None)


class KeyResolver:
    """Turns a key into the disk that holds it."""

    def __init__(
        self,
        disks: Mapping[str, StorageBackend],
        *,
        strategy: str = "recorded",
        read_order: Sequence[str] = (),
        copy_on_read: bool = False,
        ledger: DiskLedger | None = None,
    ) -> None:
        if strategy not in STRATEGIES:
            raise StorageError(
                f"storage resolve_strategy is {strategy!r}; choose from {', '.join(STRATEGIES)}"
            )
        unknown = [name for name in read_order if name not in disks]
        if unknown:
            raise StorageError(
                f"storage read_order names {', '.join(unknown)}, which are not configured "
                f"disks. Configured: {', '.join(sorted(disks)) or '<none>'}."
            )
        if strategy == "probe" and not read_order:
            raise StorageError(
                "storage resolve_strategy is 'probe' but read_order is empty; "
                "list the disks to try, newest first"
            )
        if strategy != "probe" and read_order:
            raise StorageError(
                "storage read_order is only read while probing; a recorded lookup "
                'does not try disks in order. Set resolve_strategy = "probe".'
            )
        if copy_on_read and strategy != "probe":
            raise StorageError(
                "storage copy_on_read only means something while probing; "
                "a recorded lookup already knows where the object is"
            )

        self._disks = disks
        self.strategy = strategy
        self.read_order = tuple(read_order)
        self.copy_on_read = copy_on_read
        self.ledger = ledger

    def describe(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "read_order": list(self.read_order),
            "copy_on_read": self.copy_on_read,
        }

    async def locate(self, key: str) -> str:
        """The name of the disk holding `key`. Raises `FileNotFound`."""
        if self.strategy == "recorded":
            return await self._recorded(key)
        return await self._probe(key)

    async def _recorded(self, key: str) -> str:
        if self.ledger is None:
            raise FileNotFound(f"no ledger to look {key!r} up in")
        found = await self.ledger.locate(key)
        if found is None or found not in self._disks:
            # A stale ledger entry and an absent one are the same 404 to the
            # caller; the difference is only interesting in the log.
            if found is not None:
                logger.warning("storage: ledger points %r at unknown disk %r", key, found)
            raise FileNotFound(f"no object recorded at {key!r}")
        return found

    async def _probe(self, key: str) -> str:
        for name in self.read_order:
            if await self._disks[name].exists(key):
                if self.copy_on_read and name != self.read_order[0]:
                    return await self._migrate(key, name)
                return name
        raise FileNotFound(f"no object at {key!r} on any of {', '.join(self.read_order)}")

    async def _migrate(self, key: str, source: str) -> str:
        target = self.read_order[0]
        try:
            data = await self._disks[source].get(key)
            info = await self._disks[source].stat(key)
            # write(), not put(): the object is already stored and was legal
            # when it was written. Re-running the target's pipeline would make
            # a tightened rule break the migration of older files.
            await self._disks[target].write(key, data, content_type=info.content_type)
        except StorageError as exc:
            # A failed copy must not turn a readable object into a 404. Serve
            # it from where it is and try again on the next request.
            logger.warning(
                "storage: copy-on-read %s -> %s failed for %r: %s", source, target, key, exc
            )
            return source
        if self.ledger is not None:
            await self.ledger.record(key, target)
        logger.info("storage: copied %r from %s to %s on read", key, source, target)
        return target
