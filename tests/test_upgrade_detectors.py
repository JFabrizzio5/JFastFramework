"""One project that must be told, and one that must not, for every upgrade note.

`jfast upgrade --check` is worth reading for one reason: what it prints applies
to the project it ran on. Every `detect` in `upgrades.CHANGES` carries that
promise, and each can break it in two directions. A false positive teaches
people to skip the report; a miss lets a refusal at boot reach production with
nobody warned. So every change has both halves here:

* an **affected** project -- the smallest one on disk the detector must flag,
  returning the lines it must print, so a finding that names the wrong file,
  line or setting fails as surely as one that is missing;
* a **clean** project -- built from the affected one plus the single edit the
  remedy asks for, or with the plugin off, so it is the nearest thing that must
  stay quiet rather than an empty directory that proves nothing.

Both are registered by change code, and
`test_every_change_in_the_manifest_has_an_affected_and_a_clean_project` compares
the registry with the manifest: a note added to `upgrades.py` without both
fixtures fails here instead of shipping untested.

Where a detector was wrong, a test states the correct behaviour on the
smallest project that showed the bug, so the fix cannot quietly come undone.

Everything runs through `report`, which takes the same steps as the command:
read the pin, load the project, ask `applicable` for the range. Nothing here
needs a database, a broker or the network -- the detectors read files.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from jfastframework import __version__, upgrades
from jfastframework.cli.scaffold import MODULE_LAYOUTS, Scaffolder, module_context, module_trees
from jfastframework.plugins.builtin.rag import RagSettings
from jfastframework.project import load
from jfastframework.settings import UPLOAD_MAX_BODY_BYTES, UPLOAD_REQUEST_TIMEOUT, JFastSettings

CHANGE = {change.code: change for change in upgrades.CHANGES}

#: Builds an affected project under the path and returns what the detector must
#: print for it. Each line is a prefix of the reported one: enough to pin the
#: file, the line and the subject without restating a rule's whole message.
Affected = Callable[[Path], list[str]]
#: Builds a project the change must not be reported for.
Clean = Callable[[Path], None]

AFFECTED: dict[str, list[Affected]] = {}
CLEAN: dict[str, list[Clean]] = {}


def affected_by(code: str) -> Callable[[Affected], Affected]:
    def register(build: Affected) -> Affected:
        AFFECTED.setdefault(code, []).append(build)
        return build

    return register


def unaffected_by(code: str) -> Callable[[Clean], Clean]:
    def register(build: Clean) -> Clean:
        CLEAN.setdefault(code, []).append(build)
        return build

    return register


# ---------------------------------------------------------------------------
# Building projects
# ---------------------------------------------------------------------------


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def edit(path: Path, old: str, new: str) -> None:
    """Replace *old* with *new*, refusing when *old* is not there.

    A clean fixture is an affected one plus this edit. If the edit silently
    matched nothing, the "clean" project would be the affected one and the
    test would be asserting that a bug is absent from a project that has it.
    """
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def service(root: Path, *plugins: str, extra: str = "", pin: str = "0.1.0a3") -> None:
    """A service with *plugins* enabled, pinned to *pin* in requirements.txt.

    0.1.0a3 is older than every note, so a fixture pinned there is in range of
    all of them and the only thing deciding whether a note fires is its
    detector.
    """
    enabled = ", ".join(f'"{plugin}"' for plugin in plugins)
    write(
        root / "jfast.toml",
        '[app]\nname = "billing"\nversion = "0.1.0"\nenv = "local"\n\n'
        f"[plugins]\nenabled = [{enabled}]\ndisabled = []\n{extra}",
    )
    write(root / "requirements.txt", f"jfastframework[db,server]=={pin}\n")


def report(root: Path) -> dict[str, list[str]]:
    """What `jfast upgrade --check` finds in *root*, by change code.

    The command's own steps without its rendering, which `test_cli_upgrade.py`
    covers: the pin it reads, the project it loads, the range it asks for.
    """
    pin = upgrades.pinned_version(root)
    assert pin is not None, "every fixture pins a version"
    found = upgrades.applicable(load(root), current=pin.version, installed=__version__)
    return {change.code: lines for change, lines in found}


# ---------------------------------------------------------------------------
# 0.1.0a10: async-dependencies
# ---------------------------------------------------------------------------

DEPS = "modules/invoice/deps.py"


@affected_by("async-dependencies")
def _a_plain_function_that_calls_require_auth(root: Path) -> list[str]:
    service(root, "observability", "auth")
    write(
        root / DEPS,
        "from fastapi import Request\n\n"
        "from jfastframework.plugins.builtin.auth import require_auth\n\n\n"
        "def owner_of(request: Request) -> str:\n"
        "    return require_auth(request).subject\n",
    )
    return [f"{DEPS}:7 require_auth(...)"]


@unaffected_by("async-dependencies")
def _the_same_call_awaited_from_an_async_caller(root: Path) -> None:
    _a_plain_function_that_calls_require_auth(root)
    edit(root / DEPS, "def owner_of", "async def owner_of")
    edit(root / DEPS, "return require_auth(request)", "return (await require_auth(request))")


@unaffected_by("async-dependencies")
def _the_dependency_passed_to_depends(root: Path) -> None:
    # As a Depends target the function is passed, not called, and FastAPI
    # awaits it: the one spelling the change never touched.
    service(root, "observability", "auth")
    write(
        root / DEPS,
        "from fastapi import Depends\n\n"
        "from jfastframework.plugins.builtin.auth import require_auth\n\n\n"
        "def owner_of(principal=Depends(require_auth)) -> str:\n"
        "    return principal.subject\n",
    )


def test_a_projects_own_current_tenant_helper_is_not_mistaken_for_the_framework_one(
    tmp_path: Path,
) -> None:
    """`current_tenant` is a name any multi-tenant codebase might already use.

    A synchronous helper of the project's own never became a coroutine, so
    telling its callers to await it sends them to break working code.
    """
    service(tmp_path, "observability")
    write(
        tmp_path / "shared" / "tenancy.py",
        "import contextvars\n\n"
        '_TENANT = contextvars.ContextVar("tenant", default="public")\n\n\n'
        "def current_tenant() -> str:\n"
        "    return _TENANT.get()\n",
    )
    write(
        tmp_path / "modules" / "invoice" / "service.py",
        "from shared.tenancy import current_tenant\n\n\n"
        "def label() -> str:\n"
        "    return current_tenant()\n",
    )
    assert "async-dependencies" not in report(tmp_path)


# ---------------------------------------------------------------------------
# 0.1.0a10: sync-service-factories
# ---------------------------------------------------------------------------

ROUTER = "modules/invoice/router.py"
HEXAGONAL_HTTP = "modules/orders/adapters/http.py"


@affected_by("sync-service-factories")
def _modules_wired_with_plain_def_factories(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(
        root / ROUTER,
        "from fastapi import Request\n\n"
        "from jfastframework.plugins.builtin.database import DbSession\n\n"
        "from .service import InvoiceService\n\n\n"
        "def get_service(request: Request, session: DbSession) -> InvoiceService:\n"
        "    return InvoiceService(session)\n",
    )
    write(
        root / HEXAGONAL_HTTP,
        "from jfastframework.plugins.builtin.database import DbSession\n\n\n"
        "def get_use_cases(request, session: DbSession):\n"
        "    return None\n",
    )
    return [f"{ROUTER}:8 def get_service", f"{HEXAGONAL_HTTP}:4 def get_use_cases"]


@unaffected_by("sync-service-factories")
def _the_same_factories_made_async(root: Path) -> None:
    _modules_wired_with_plain_def_factories(root)
    edit(root / ROUTER, "def get_service", "async def get_service")
    edit(root / HEXAGONAL_HTTP, "def get_use_cases", "async def get_use_cases")


def test_a_method_named_get_service_is_not_a_request_factory(tmp_path: Path) -> None:
    """The threadpool hop is paid by a module-level dependency, not by a method.

    A lookup table with a `get_service` method is ordinary Python, and adding
    `async` to it -- which is what the note says to do -- breaks every caller.
    """
    service(tmp_path, "observability")
    write(
        tmp_path / "modules" / "invoice" / "registry.py",
        "class Handlers:\n"
        "    def get_service(self, name: str) -> object:\n"
        "        return self.__dict__[name]\n",
    )
    assert "sync-service-factories" not in report(tmp_path)


# The notes that read module source, and nothing the generator of this release
# writes may trigger one: a warning on freshly generated code is one nobody can
# act on. `layer-globs-narrowed` is left out on purpose -- it compares against
# the matcher 0.1.0a4 used, and under that matcher the screaming contract's
# catch-all really did reach `tests/`, so a project on 0.1.0a4 is rightly told.
_SOURCE_NOTES_ON_GENERATED_CODE = (
    "async-dependencies",
    "sync-service-factories",
    "module-boundaries",
    "screaming-public-layer",
    "naive-datetime-rule",
    "hexagonal-eager-create-payload",
    "publish-without-receiver",
    "contracts-event-rules",
    "outbox-manual-construction",
    "worker-drain-timeout",
    "worker-py-to-module-tasks",
    "redis-command-timeout",
    "database-unavailable-503",
)


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_a_freshly_generated_module_is_told_nothing_about_its_own_source(
    tmp_path: Path, layout: str
) -> None:
    service(
        tmp_path,
        "observability",
        "database",
        extra=f'\n[modules.orders]\nlayout = "{layout}"\nui = "api"\n',
    )
    Scaffolder().render_trees(
        module_trees(layout, "api", tmp_path / "modules", tmp_path),
        module_context("orders", layout=layout),
    )
    found = report(tmp_path)
    assert [code for code in _SOURCE_NOTES_ON_GENERATED_CODE if code in found] == [], found


# ---------------------------------------------------------------------------
# 0.1.0a10: metrics-route-labels
# ---------------------------------------------------------------------------


@affected_by("metrics-route-labels")
def _a_service_with_metrics_enabled(root: Path) -> list[str]:
    service(root, "observability", "metrics")
    return ["[plugins] enabled includes metrics"]


@unaffected_by("metrics-route-labels")
def _the_same_service_without_metrics(root: Path) -> None:
    _a_service_with_metrics_enabled(root)
    edit(root / "jfast.toml", ', "metrics"]', "]")


def test_metrics_loaded_by_default_is_reported(tmp_path: Path) -> None:
    """An empty allow-list loads every `default_enabled` plugin, metrics among them.

    The registry decides that, not the list, so a service that never named
    metrics still exports the relabelled series its dashboards query.
    """
    service(tmp_path)
    assert "metrics-route-labels" in report(tmp_path)


def test_metrics_named_in_disabled_is_not_reported(tmp_path: Path) -> None:
    """`disabled` is the documented way to strip monitoring without editing code."""
    _a_service_with_metrics_enabled(tmp_path)
    edit(tmp_path / "jfast.toml", "disabled = []", 'disabled = ["metrics"]')
    assert "metrics-route-labels" not in report(tmp_path)


# ---------------------------------------------------------------------------
# 0.1.0a10: the rag plugin
# ---------------------------------------------------------------------------


def rag_service(root: Path, settings: str = "") -> None:
    service(root, "observability", "database", "rag", extra=f"\n[plugin.rag]\n{settings}")


def without_rag(root: Path) -> None:
    """The plugin off, its settings table left behind: settings alone do nothing."""
    edit(root / "jfast.toml", ', "rag"]', "]")


@affected_by("rag-tenant-required")
def _rag_left_on_the_tenant_scoped_default(root: Path) -> list[str]:
    rag_service(root)
    return ["[plugin.rag] tenant_scoped defaults to true"]


@unaffected_by("rag-tenant-required")
def _rag_declared_single_tenant(root: Path) -> None:
    rag_service(root, "tenant_scoped = false\n")


@unaffected_by("rag-tenant-required")
def _rag_turned_off(root: Path) -> None:
    _rag_left_on_the_tenant_scoped_default(root)
    without_rag(root)


@affected_by("rag-router-off")
def _rag_that_relied_on_the_router_being_mounted(root: Path) -> list[str]:
    rag_service(root)
    return ["[plugin.rag] mount_router now defaults to false (POST /rag/search is gone)"]


@unaffected_by("rag-router-off")
def _rag_that_already_said_the_router_is_off(root: Path) -> None:
    rag_service(root, "mount_router = false\n")


@unaffected_by("rag-router-off")
def _rag_router_note_with_rag_off(root: Path) -> None:
    _rag_that_relied_on_the_router_being_mounted(root)
    without_rag(root)


def test_a_router_mounted_on_purpose_without_auth_is_reported(tmp_path: Path) -> None:
    """The project this change breaks hardest, and the one the detector skips.

    `mount_router = true` was legal in 0.1.0a9 with no auth plugin. 0.1.0a10
    refuses to start that configuration, and even with auth enabled the router
    now wants a signed-in caller and ignores the tenant in the body. Only an
    explicit `false` means there is nothing left to do.
    """
    rag_service(tmp_path, "mount_router = true\n")
    assert "rag-router-off" in report(tmp_path)


@affected_by("rag-qdrant-point-ids")
def _rag_on_qdrant_with_a_named_collection(root: Path) -> list[str]:
    rag_service(root, 'store = "qdrant"\ncollection = "contratos"\n')
    return ['[plugin.rag] store = "qdrant", collection = "contratos"']


@affected_by("rag-qdrant-point-ids")
def _rag_on_qdrant_with_the_default_collection(root: Path) -> list[str]:
    rag_service(root, 'store = "qdrant"\n')
    # From the plugin, not a literal: the collection named has to be the one
    # the re-ingest will actually have to empty.
    collection = RagSettings.model_fields["collection"].default
    return [f'[plugin.rag] store = "qdrant", collection = "{collection}"']


@unaffected_by("rag-qdrant-point-ids")
def _rag_on_pgvector(root: Path) -> None:
    # pgvector's table is upgraded in place by ensure_schema; only Qdrant's
    # point ids are baked into data that is already written.
    rag_service(root, 'store = "pgvector"\n')


@unaffected_by("rag-qdrant-point-ids")
def _qdrant_settings_with_rag_off(root: Path) -> None:
    _rag_on_qdrant_with_a_named_collection(root)
    without_rag(root)


@affected_by("rag-recursive-chunking")
def _rag_on_the_default_chunking(root: Path) -> list[str]:
    rag_service(root)
    return ['[plugin.rag] chunk_strategy defaults to "recursive"']


@unaffected_by("rag-recursive-chunking")
def _rag_that_kept_fixed_chunks(root: Path) -> None:
    rag_service(root, 'chunk_strategy = "fixed"\n')


@unaffected_by("rag-recursive-chunking")
def _chunking_note_with_rag_off(root: Path) -> None:
    _rag_on_the_default_chunking(root)
    without_rag(root)


# ---------------------------------------------------------------------------
# 0.1.0a10: module-boundaries
# ---------------------------------------------------------------------------

PLACEMENT_CONTRACT = '[project]\nname = "cuadra"\n\n[rules.placement]\nenabled = true\n'
DECLARED = '\n[modules.asesor]\ndepends_on = ["comprobante"]\n'


def two_modules(root: Path) -> None:
    """`comprobante` owns a table and exposes a facade; `asesor` wants its data."""
    service(root, "observability", "database")
    write(root / "contracts.toml", PLACEMENT_CONTRACT)
    write(
        root / "modules" / "comprobante" / "models.py",
        "from jfastframework.db import Base\n\n\n"
        "class Comprobante(Base):\n"
        '    __tablename__ = "comprobantes"\n',
    )
    write(
        root / "modules" / "comprobante" / "public.py",
        "from dataclasses import dataclass\n\n\n"
        "@dataclass(frozen=True)\n"
        "class Total:\n"
        "    centavos: int\n\n\n"
        "async def total(session, *, tenant_id):\n"
        "    return Total(0)\n",
    )
    write(root / "modules" / "comprobante" / "repository.py", "class Repo:\n    pass\n")
    write(root / "modules" / "asesor" / "__init__.py", "")


@affected_by("module-boundaries")
def _a_facade_called_without_depends_on(root: Path) -> list[str]:
    two_modules(root)
    write(
        root / "modules" / "asesor" / "service.py",
        "from modules.comprobante.public import total\n",
    )
    return ["modules/asesor/service.py:1 undeclared-dependency: "]


@affected_by("module-boundaries")
def _raw_sql_against_another_modules_table(root: Path) -> list[str]:
    two_modules(root)
    write(root / "contracts.toml", PLACEMENT_CONTRACT + DECLARED)
    write(
        root / "modules" / "asesor" / "repository.py",
        '\nSQL = "SELECT sum(total) FROM comprobantes WHERE tenant_id = :t"\n',
    )
    return ["modules/asesor/repository.py:2 cross-module-sql: "]


@unaffected_by("module-boundaries")
def _the_facade_call_declared_in_depends_on(root: Path) -> None:
    _a_facade_called_without_depends_on(root)
    write(root / "contracts.toml", PLACEMENT_CONTRACT + DECLARED)


@unaffected_by("module-boundaries")
def _the_same_modules_with_no_contract(root: Path) -> None:
    # No contracts.toml, no `contracts check`: nothing this release added to
    # the checker can fail a build that never runs it.
    _a_facade_called_without_depends_on(root)
    (root / "contracts.toml").unlink()


def test_a_relative_import_across_modules_is_reported(tmp_path: Path) -> None:
    """The note's own detail says `cross-module` now catches relative imports.

    `from ..comprobante.repository import Repo` passed 0.1.0a9's check and fails
    this one, so it is a build that breaks on upgrade -- and it is the only
    violation here, so the report says nothing at all about it.
    """
    two_modules(tmp_path)
    write(tmp_path / "contracts.toml", PLACEMENT_CONTRACT + DECLARED)
    write(
        tmp_path / "modules" / "asesor" / "service.py",
        "from ..comprobante.repository import Repo\n",
    )
    assert "module-boundaries" in report(tmp_path)


# ---------------------------------------------------------------------------
# 0.1.0a10: screaming-public-layer
# ---------------------------------------------------------------------------

SCREAMING_A9 = """\
[project]
name = "billing"

[layers.domain]
paths = ["modules/*/[!_]*.py"]
may_import = ["shared"]

[layers.shared]
paths = ["shared/*.py"]
may_import = []
"""

PUBLIC_LAYER = """
[layers.public]
paths = ["modules/*/public.py"]
may_import = ["domain", "shared"]
"""


@affected_by("screaming-public-layer")
def _a_screaming_module_under_the_0_1_0a9_contract(root: Path) -> list[str]:
    service(root, "observability", extra='\n[modules.invoice]\nlayout = "screaming"\n')
    write(root / "contracts.toml", SCREAMING_A9)
    write(root / "modules" / "invoice" / "__init__.py", "")
    write(root / "modules" / "invoice" / "public.py", "VALUE = 1\n")
    return ["contracts.toml has no [layers.public]"]


@unaffected_by("screaming-public-layer")
def _the_same_contract_with_the_public_layer_added(root: Path) -> None:
    _a_screaming_module_under_the_0_1_0a9_contract(root)
    write(root / "contracts.toml", SCREAMING_A9 + PUBLIC_LAYER)


@unaffected_by("screaming-public-layer")
def _the_same_contract_over_a_layered_module(root: Path) -> None:
    # The catch-all glob is the screaming contract's; a layered module has no
    # layer that swallows public.py.
    _a_screaming_module_under_the_0_1_0a9_contract(root)
    edit(root / "jfast.toml", 'layout = "screaming"', 'layout = "layered"')


# ---------------------------------------------------------------------------
# 0.1.0a5: contracts
# ---------------------------------------------------------------------------

NARROWED = "modules/billing/deep/nested/handlers.py"


@affected_by("layer-globs-narrowed")
def _a_single_star_glob_that_used_to_reach_a_deep_file(root: Path) -> list[str]:
    service(root, "observability")
    write(
        root / "contracts.toml",
        '[project]\nname = "billing"\n\n'
        '[layers.handlers]\npaths = ["modules/*/handlers.py"]\nmay_import = ["shared"]\n',
    )
    write(root / NARROWED, "VALUE = 1\n")
    return [f"{NARROWED} (was layer 'handlers')"]


@unaffected_by("layer-globs-narrowed")
def _the_glob_widened_to_double_star(root: Path) -> None:
    # The remedy the note prints: `**` crosses directories on purpose under
    # both matchers, so nothing changes hands.
    _a_single_star_glob_that_used_to_reach_a_deep_file(root)
    edit(root / "contracts.toml", "modules/*/handlers.py", "modules/**/handlers.py")


def test_a_file_its_own_layer_still_claims_did_not_change_hands(tmp_path: Path) -> None:
    """The screaming contract's shape: a domain catch-all and a use_cases layer.

    Under fnmatch the catch-all also matched `use_cases/create_invoice.py`, but
    `layer_for` picks the most specific pattern, so the file was use_cases then
    and is use_cases now. The note says the files it lists "match none today";
    this one is listed as "was layer 'domain'" while its own layer governs it.
    A freshly generated screaming module gets one such line per use case.
    """
    service(tmp_path, "observability")
    write(
        tmp_path / "contracts.toml",
        '[project]\nname = "billing"\n\n'
        '[layers.domain]\npaths = ["modules/*/[!_]*.py"]\nmay_import = ["shared"]\n\n'
        '[layers.use_cases]\npaths = ["modules/*/use_cases/*.py"]\nmay_import = ["domain"]\n',
    )
    write(tmp_path / "modules" / "billing" / "use_cases" / "create_invoice.py", "VALUE = 1\n")
    assert "layer-globs-narrowed" not in report(tmp_path)


STAMP = "modules/billing/stamp.py"


@affected_by("naive-datetime-rule")
def _a_naive_datetime_under_the_default_contract(root: Path) -> list[str]:
    service(root, "observability")
    # No [rules.async_safety] at all: the rule is on by default, which is why
    # it arrives without anyone opting in.
    write(root / "contracts.toml", '[project]\nname = "billing"\n')
    write(
        root / STAMP,
        "from datetime import datetime\n\n\n"
        "def stamped() -> str:\n"
        "    return datetime.now().isoformat()\n",
    )
    return [f"{STAMP}:5"]


@unaffected_by("naive-datetime-rule")
def _the_same_call_given_a_zone(root: Path) -> None:
    _a_naive_datetime_under_the_default_contract(root)
    edit(root / STAMP, "import datetime", "import UTC, datetime")
    edit(root / STAMP, "datetime.now()", "datetime.now(tz=UTC)")


@unaffected_by("naive-datetime-rule")
def _the_rule_deferred_in_the_contract(root: Path) -> None:
    # The other remedy the note prints. If this did not silence it, the note
    # would be sending people to a key that does nothing.
    _a_naive_datetime_under_the_default_contract(root)
    write(
        root / "contracts.toml",
        '[project]\nname = "billing"\n\n[rules.async_safety]\nnaive_datetime = false\n',
    )


# `may_import` without "shared" is exactly what 0.1.0a3 generated.
CONTRACTS_A3 = """\
[project]
name = "billing"

[layers.http]
paths = ["modules/*/router.py"]
may_import = ["service", "schemas"]

[layers.service]
paths = ["modules/*/service.py"]
may_import = ["storage", "schemas"]

[layers.schemas]
paths = ["modules/*/schemas.py"]
may_import = []

[layers.shared]
paths = ["shared/*.py"]
may_import = []
"""


@affected_by("contracts-shared-import")
def _a_contract_from_0_1_0a3(root: Path) -> list[str]:
    service(root, "observability")
    write(root / "contracts.toml", CONTRACTS_A3)
    # `shared` itself is never named: its empty may_import is what keeps the
    # graph a tree.
    return [
        '[layers.http]  may_import = ["service", "schemas"]  ->  add "shared"',
        '[layers.service]  may_import = ["storage", "schemas"]  ->  add "shared"',
        '[layers.schemas]  may_import = []  ->  add "shared"',
    ]


@unaffected_by("contracts-shared-import")
def _the_same_contract_with_shared_added_everywhere(root: Path) -> None:
    _a_contract_from_0_1_0a3(root)
    path = root / "contracts.toml"
    edit(path, '["service", "schemas"]', '["service", "schemas", "shared"]')
    edit(path, '["storage", "schemas"]', '["storage", "schemas", "shared"]')
    edit(path, 'schemas.py"]\nmay_import = []', 'schemas.py"]\nmay_import = ["shared"]')


@affected_by("contracts-layout-mismatch")
def _a_layered_contract_over_a_hexagonal_module(root: Path) -> list[str]:
    service(root, "observability", extra='\n[modules.invoice]\nlayout = "hexagonal"\nui = ""\n')
    write(root / "contracts.toml", CONTRACTS_A3)
    # Sorted by layer name, and `shared` is absent: it governs shared/, not a
    # module, so it says nothing about which layout the modules use.
    return [
        "[layers.http]  paths = ['modules/*/router.py']  ->  matches no hexagonal module",
        "[layers.schemas]  paths = ['modules/*/schemas.py']  ->  matches no hexagonal module",
        "[layers.service]  paths = ['modules/*/service.py']  ->  matches no hexagonal module",
    ]


@unaffected_by("contracts-layout-mismatch")
def _the_same_contract_over_a_layered_module(root: Path) -> None:
    _a_layered_contract_over_a_hexagonal_module(root)
    edit(root / "jfast.toml", 'layout = "hexagonal"', 'layout = "layered"')


@unaffected_by("contracts-layout-mismatch")
def _a_module_that_recorded_no_layout(root: Path) -> None:
    # Nothing on disk says which layout an unrecorded module is in, and a
    # guess is a warning that may not apply.
    _a_layered_contract_over_a_hexagonal_module(root)
    edit(root / "jfast.toml", 'layout = "hexagonal"\n', "")


# ---------------------------------------------------------------------------
# 0.1.0a4: timestamps, pagination, request limits, exit codes
# ---------------------------------------------------------------------------

MODELS = "modules/invoice/models.py"


@affected_by("timestamps-timezone-aware")
def _a_model_that_mixes_in_the_timestamps(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(
        root / MODELS,
        "from jfastframework.db import Base, TimestampMixin\n\n\n"
        "class Invoice(Base, TimestampMixin):\n"
        '    __tablename__ = "invoices"\n',
    )
    # Whole statement, USING clause included: without it PostgreSQL converts
    # through the implicit cast and shifts every row on a server not in UTC.
    return [
        f"{MODELS}  ->  invoices\n"
        "ALTER TABLE invoices\n"
        "    ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',\n"
        "    ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';"
    ]


@unaffected_by("timestamps-timezone-aware")
def _the_same_model_without_the_mixin(root: Path) -> None:
    _a_model_that_mixes_in_the_timestamps(root)
    edit(root / MODELS, "class Invoice(Base, TimestampMixin):", "class Invoice(Base):")


REPOSITORY = "modules/invoice/repository.py"


@affected_by("pagination-total-optional")
def _a_repository_that_paginates(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(
        root / REPOSITORY,
        "from jfastframework.db import BaseRepository\n\n"
        "from .models import Invoice\n\n\n"
        "class InvoiceRepository(BaseRepository[Invoice]):\n"
        "    model = Invoice\n\n"
        "    async def recent(self):\n"
        "        return await self.paginate(limit=50, offset=0)\n",
    )
    return [f"{REPOSITORY}:10  ->  paginate(...)"]


@unaffected_by("pagination-total-optional")
def _the_same_call_on_a_paginator_that_is_not_the_frameworks(root: Path) -> None:
    # `paginate` is too common a name to report on its own; a project that
    # never imports jfastframework.db has never held a Page.
    _a_repository_that_paginates(root)
    edit(root / REPOSITORY, "from jfastframework.db import", "from shared.paging import")


_PLAIN_BODY = JFastSettings.model_fields["max_body_bytes"].default
_PLAIN_TIMEOUT = JFastSettings.model_fields["request_timeout"].default


@affected_by("request-limit-defaults")
def _a_service_that_set_neither_limit(root: Path) -> list[str]:
    service(root, "observability", "database")
    return [f"max_body_bytes = {_PLAIN_BODY}", f"request_timeout = {_PLAIN_TIMEOUT}"]


@affected_by("request-limit-defaults")
def _an_upload_service_that_set_neither_limit(root: Path) -> list[str]:
    # The raised pair from the kernel: the report has to quote what the
    # running service will resolve, not what a scaffold once wrote.
    service(root, "observability", "database", "storage")
    note = "  (this service enables storage)"
    return [
        f"max_body_bytes = {UPLOAD_MAX_BODY_BYTES}{note}",
        f"request_timeout = {UPLOAD_REQUEST_TIMEOUT}{note}",
    ]


@affected_by("request-limit-defaults")
def _a_service_that_set_only_the_body_limit(root: Path) -> list[str]:
    service(root, "observability", "database")
    edit(root / "jfast.toml", 'env = "local"\n', 'env = "local"\nmax_body_bytes = 1024\n')
    return [f"request_timeout = {_PLAIN_TIMEOUT}"]


@unaffected_by("request-limit-defaults")
def _a_service_that_set_both_limits_to_unlimited(root: Path) -> None:
    # The note's own escape hatch: an explicit 0 keeps the old behaviour.
    _a_service_that_set_neither_limit(root)
    edit(
        root / "jfast.toml",
        'env = "local"\n',
        'env = "local"\nmax_body_bytes = 0\nrequest_timeout = 0\n',
    )


@affected_by("cli-exit-codes")
def _any_service_pinned_before_0_1_0a4(root: Path) -> list[str]:
    # Nothing on disk says whether CI branches on an exit code, so it is
    # stated unconditionally -- with nothing to point at.
    service(root, "observability")
    return []


@unaffected_by("cli-exit-codes")
def _any_service_pinned_at_0_1_0a4(root: Path) -> None:
    service(root, "observability", pin="0.1.0a4")


def test_the_exit_code_note_is_the_only_one_nothing_on_disk_can_decide() -> None:
    """`detect is None` means stated for every project in range.

    A second one would be a note that fires on every upgrade whatever the
    project holds, so it has to be a decision, not an accident.
    """
    assert [change.code for change in upgrades.CHANGES if change.detect is None] == [
        "cli-exit-codes"
    ]


# ---------------------------------------------------------------------------
# 0.1.0a4 and 0.1.0a5: services that mint tokens
# ---------------------------------------------------------------------------

AUTH_ISSUING = '\n[plugin.auth]\nmode = "jwks"\nissue_tokens = true\n'


def token_issuer(root: Path) -> None:
    service(root, "observability", "database", "auth", extra=AUTH_ISSUING)


def only_verifies_tokens(root: Path) -> None:
    token_issuer(root)
    edit(root / "jfast.toml", "issue_tokens = true", "issue_tokens = false")


def auth_turned_off(root: Path) -> None:
    """`issue_tokens = true` left in a table whose plugin is no longer loaded."""
    token_issuer(root)
    edit(root / "jfast.toml", ', "auth"]', "]")


@affected_by("refresh-tokens-rejected")
def _a_service_that_mints_refresh_tokens(root: Path) -> list[str]:
    token_issuer(root)
    return ["[plugin.auth] issue_tokens = true -- this service mints the tokens"]


@affected_by("logout-ends-one-session")
def _a_service_that_serves_logout(root: Path) -> list[str]:
    token_issuer(root)
    return ["[plugin.auth] issue_tokens = true -- this service serves /auth/logout"]


@affected_by("access-token-fam-claim")
def _a_service_that_mints_access_tokens(root: Path) -> list[str]:
    token_issuer(root)
    return ["[plugin.auth] issue_tokens = true -- this service mints the tokens"]


@affected_by("refresh-grace-seconds")
def _an_issuer_that_never_chose_a_grace_window(root: Path) -> list[str]:
    token_issuer(root)
    return ["[plugin.auth] refresh_grace_seconds is not set, so the window is on"]


for _code in (
    "refresh-tokens-rejected",
    "logout-ends-one-session",
    "access-token-fam-claim",
    "refresh-grace-seconds",
):
    # Verifying somebody else's tokens is most services with auth on, and
    # none of the issuing endpoints' changes reaches them.
    unaffected_by(_code)(only_verifies_tokens)
    unaffected_by(_code)(auth_turned_off)


@unaffected_by("refresh-grace-seconds")
def _an_issuer_that_chose_strict_reuse_detection(root: Path) -> None:
    token_issuer(root)
    write(root / "jfast.toml", (root / "jfast.toml").read_text() + "refresh_grace_seconds = 0\n")


STORE = "shared/store.py"


@affected_by("token-store-rotate-refresh")
def _a_token_store_of_the_projects_own(root: Path) -> list[str]:
    token_issuer(root)
    write(
        root / STORE,
        "class DynamoTokenStore:\n"
        "    async def rotate_refresh(self, token_id: str, *, family: str, ttl: int) -> bool:\n"
        "        return True\n",
    )
    return [f"{STORE}:2  ->  DynamoTokenStore.rotate_refresh"]


@unaffected_by("token-store-rotate-refresh")
def _an_issuer_on_the_shipped_stores(root: Path) -> None:
    # The framework calls its own stores and reads the new return value.
    token_issuer(root)


def test_a_token_store_already_on_the_new_protocol_is_not_reported(tmp_path: Path) -> None:
    """A project fixes its store first and bumps the pin second.

    Between the two, the note keeps naming a method that already takes
    `grace` and returns the three literals -- a warning whose remedy has been
    applied, which is exactly the kind people learn to skip.
    """
    token_issuer(tmp_path)
    write(
        tmp_path / STORE,
        "class DynamoTokenStore:\n"
        "    async def rotate_refresh(\n"
        "        self, token_id: str, *, family: str, ttl: int, grace: int = 0\n"
        "    ) -> str:\n"
        '        return "rotated"\n',
    )
    assert "token-store-rotate-refresh" not in report(tmp_path)


# ---------------------------------------------------------------------------
# 0.1.0a8: the three that stop a boot
# ---------------------------------------------------------------------------


@affected_by("session-store-per-process")
def _an_issuer_with_nothing_shared_to_record_sessions(root: Path) -> list[str]:
    token_issuer(root)
    return ['[plugins] enabled has "auth" with issue_tokens = true and no "cache"']


@unaffected_by("session-store-per-process")
def _the_same_issuer_with_the_cache_plugin(root: Path) -> None:
    token_issuer(root)
    edit(root / "jfast.toml", '"auth"]', '"cache", "auth"]')


unaffected_by("session-store-per-process")(only_verifies_tokens)


def test_cache_pulled_in_by_ratelimit_counts_as_a_shared_store(tmp_path: Path) -> None:
    """`ratelimit` requires `cache`, and the registry loads it without being asked.

    auth then finds `cache.client` and uses the Redis store, so the refusal
    this note warns about never happens -- the note is about a list, not about
    what the service will run.
    """
    token_issuer(tmp_path)
    edit(tmp_path / "jfast.toml", '"auth"]', '"auth", "ratelimit"]')
    assert "session-store-per-process" not in report(tmp_path)


@affected_by("mail-backend-silent")
def _mail_on_the_default_backend(root: Path) -> list[str]:
    # The default is the silent one, which is why a config naming no backend
    # at all is the case that matters.
    service(root, "observability", "mail")
    return ['[plugin.mail] backend = "console"']


@affected_by("mail-backend-silent")
def _mail_on_the_memory_backend(root: Path) -> list[str]:
    service(root, "observability", "mail", extra='\n[plugin.mail]\nbackend = "memory"\n')
    return ['[plugin.mail] backend = "memory"']


@unaffected_by("mail-backend-silent")
def _mail_over_smtp(root: Path) -> None:
    service(root, "observability", "mail", extra='\n[plugin.mail]\nbackend = "smtp"\n')


@unaffected_by("mail-backend-silent")
def _no_mail_plugin(root: Path) -> None:
    _mail_on_the_memory_backend(root)
    edit(root / "jfast.toml", ', "mail"]', "]")


WORKER = (
    "from jfastframework.queues.worker import Worker\n"
    "worker = Worker(backend, registry, job_timeout=600)\n"
)


@affected_by("job-timeout-past-visibility")
def _a_worker_that_outlives_its_claim(root: Path) -> list[str]:
    service(root, "observability", "queue", extra="\n[plugin.queue]\nvisibility_timeout = 300\n")
    write(root / "worker.py", WORKER)
    return ["worker.py:2: job_timeout=600s against visibility_timeout=300s"]


@affected_by("job-timeout-past-visibility")
def _a_worker_at_the_default_window_exactly(root: Path) -> list[str]:
    # Both defaulted to 300 and raced at the boundary; equal is a duplicate
    # run waiting to happen, and the window here is the plugin's default.
    service(root, "observability", "queue")
    write(root / "worker.py", WORKER.replace("600", "300"))
    return ["worker.py:2: job_timeout=300s against visibility_timeout=300s"]


@unaffected_by("job-timeout-past-visibility")
def _the_window_raised_above_the_job(root: Path) -> None:
    _a_worker_that_outlives_its_claim(root)
    edit(root / "jfast.toml", "visibility_timeout = 300", "visibility_timeout = 1800")


@unaffected_by("job-timeout-past-visibility")
def _the_timeout_left_to_the_worker(root: Path) -> None:
    # The first remedy: without the argument the worker derives 80% of the
    # window, which cannot reach past it.
    _a_worker_that_outlives_its_claim(root)
    edit(root / "worker.py", ", job_timeout=600", "")


# ---------------------------------------------------------------------------
# 0.1.0a9: migrations and sessions
# ---------------------------------------------------------------------------


@affected_by("autogenerate-drops-framework-tables")
def _an_env_py_without_the_framework_filter(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(root / "migrations" / "env.py", "target_metadata = Base.metadata\n")
    return ["migrations/env.py"]


@unaffected_by("autogenerate-drops-framework-tables")
def _the_same_env_py_passing_include_name(root: Path) -> None:
    _an_env_py_without_the_framework_filter(root)
    write(
        root / "migrations" / "env.py",
        "from jfastframework.db.framework import include_name\n"
        "target_metadata = Base.metadata\n"
        "context.configure(target_metadata=target_metadata, include_name=include_name)\n",
    )


ROUTES = "routes.py"


@affected_by("session-commits-after-response")
def _routes_whose_sessions_commit_after_the_response(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(
        root / ROUTES,
        "from fastapi import Depends\n"
        "from jfastframework.plugins.builtin.database import (\n"
        "    read_session_dependency, session_dependency)\n"
        "def get_service(session=Depends(session_dependency)): ...\n"
        "def fine(session=Depends(session_dependency, scope='function')): ...\n"
        "def reads(session=Depends(read_session_dependency)): ...\n",
    )
    # Line 5 is function-scoped already and must not be listed.
    return [
        f"{ROUTES}:4  ->  Depends(session_dependency)",
        f"{ROUTES}:6  ->  Depends(read_session_dependency)",
    ]


@unaffected_by("session-commits-after-response")
def _the_same_routes_on_the_scoped_aliases(root: Path) -> None:
    service(root, "observability", "database")
    write(
        root / ROUTES,
        "from jfastframework.plugins.builtin.database import DbSession, ReadSession\n"
        "def get_service(session: DbSession): ...\n"
        "def reads(session: ReadSession): ...\n",
    )


def test_a_project_inside_a_folder_named_build_is_still_read(tmp_path: Path) -> None:
    """`_python_files` tests every part of the absolute path against SKIP_DIRS.

    So a checkout at `~/build/billing`, `/srv/site/billing` or `~/dist/billing`
    has every file skipped, and every source-reading note -- timestamps,
    sessions, pagination, token stores -- reports a clean project. The skip is
    meant for directories inside the project.
    """
    root = tmp_path / "build" / "billing"
    expected = _routes_whose_sessions_commit_after_the_response(root)
    assert report(root).get("session-commits-after-response") == expected


# ---------------------------------------------------------------------------
# 0.1.0a7: compose-artifacts-stale
# ---------------------------------------------------------------------------

STALE_COMPOSE = """\
services:
  app:
    build: .
    env_file: .env
"""


@affected_by("compose-artifacts-stale")
def _a_compose_file_from_before_0_1_0a7(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(root / "docker-compose.yml", STALE_COMPOSE)
    return [
        "docker-compose.yml: builds an image, and there is no Dockerfile to build",
        # From the database plugin's own infra declaration: the variable its
        # client reads, missing, so the container reads the host's .env.
        "docker-compose.yml: no internal address for JFAST_DB_DSN",
    ]


@unaffected_by("compose-artifacts-stale")
def _the_same_compose_file_regenerated(root: Path) -> None:
    _a_compose_file_from_before_0_1_0a7(root)
    write(root / "Dockerfile", "FROM python:3.12-slim\n")
    write(
        root / "docker-compose.yml",
        STALE_COMPOSE
        + "    environment:\n"
        + "      JFAST_DB_DSN: postgresql+asyncpg://app:app@db:5432/app\n",
    )


@unaffected_by("compose-artifacts-stale")
def _a_service_with_no_compose_file(root: Path) -> None:
    # Nothing stale: the next `jfast deploy compose` writes the current shape.
    _a_compose_file_from_before_0_1_0a7(root)
    (root / "docker-compose.yml").unlink()


# ---------------------------------------------------------------------------
# 0.1.0a11: the hexagonal package init
# ---------------------------------------------------------------------------

HEX_INIT = "modules/orders/__init__.py"

HEX_INIT_A10 = '''\
"""Orders module (hexagonal layout)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .application.use_cases import OrdersUseCases

# The payload type is part of the module's public surface.
from .adapters.http import OrdersCreate as CreatePayload

__all__ = ["CreatePayload", "build_service", "router"]


def __getattr__(name: str) -> Any:
    if name == "router":
        from .adapters.http import router

        return router
    raise AttributeError(name)
'''


@affected_by("hexagonal-eager-create-payload")
def _a_hexagonal_init_from_0_1_0a10(root: Path) -> list[str]:
    service(root, "observability", "database", extra='\n[modules.orders]\nlayout = "hexagonal"\n')
    write(root / HEX_INIT, HEX_INIT_A10)
    return [f"{HEX_INIT}:11 from .adapters.http import OrdersCreate as CreatePayload"]


@unaffected_by("hexagonal-eager-create-payload")
def _the_same_init_deferring_the_payload(root: Path) -> None:
    # The remedy: the import moves under TYPE_CHECKING and into __getattr__,
    # which is where 0.1.0a11's template keeps it.
    _a_hexagonal_init_from_0_1_0a10(root)
    edit(root / HEX_INIT, "from .adapters.http import OrdersCreate as CreatePayload\n", "")
    edit(
        root / HEX_INIT,
        "    from .application.use_cases import OrdersUseCases\n",
        "    from .adapters.http import OrdersCreate as CreatePayload\n"
        "    from .application.use_cases import OrdersUseCases\n",
    )
    edit(
        root / HEX_INIT,
        "    raise AttributeError(name)",
        '    if name == "CreatePayload":\n'
        "        from .adapters.http import OrdersCreate\n\n"
        "        return OrdersCreate\n"
        "    raise AttributeError(name)",
    )


def test_the_smoke_scripts_remedy_is_one_the_detector_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`scripts/smoke_upgrade.sh` carries a project through this note by script.

    The same regex, applied to the 0.1.0a10 init, must leave nothing to report
    -- or the smoke would pass on a remedy the report still rejects.
    """
    _a_hexagonal_init_from_0_1_0a10(tmp_path)
    script = (Path(__file__).parents[1] / "scripts" / "smoke_upgrade.sh").read_text()
    body = script.split("<<'PYEOF'\n", 1)[1].split("PYEOF\n", 1)[0]
    monkeypatch.chdir(tmp_path)
    exec(compile(body, "smoke_upgrade.sh", "exec"), {})
    assert "hexagonal-eager-create-payload" not in report(tmp_path)


# ---------------------------------------------------------------------------
# 0.1.0a11: tasks, events, workers
# ---------------------------------------------------------------------------

TASKS_LAYER = """
[layers.tasks]
paths = ["modules/*/tasks.py", "modules/*/tasks/*.py"]
may_import = ["domain", "shared"]
"""

TASKS = "modules/invoice/tasks.py"


@affected_by("contracts-tasks-layer")
def _a_tasks_file_under_the_screaming_catch_all(root: Path) -> list[str]:
    service(root, "observability", extra='\n[modules.invoice]\nlayout = "screaming"\n')
    write(root / "contracts.toml", SCREAMING_A9 + PUBLIC_LAYER)
    write(root / TASKS, '"""Work this module owns."""\n')
    return [f"{TASKS}  ->  layer 'domain'"]


@affected_by("contracts-tasks-layer")
def _a_tasks_file_no_layer_of_a_0_1_0a3_contract_claims(root: Path) -> list[str]:
    service(root, "observability")
    write(root / "contracts.toml", CONTRACTS_A3)
    write(root / TASKS, '"""Work this module owns."""\n')
    return [f"{TASKS}  ->  no layer"]


@unaffected_by("contracts-tasks-layer")
def _the_same_contract_with_a_tasks_layer(root: Path) -> None:
    _a_tasks_file_under_the_screaming_catch_all(root)
    write(root / "contracts.toml", SCREAMING_A9 + PUBLIC_LAYER + TASKS_LAYER)


@unaffected_by("contracts-tasks-layer")
def _a_contract_with_no_tasks_file_to_misplace(root: Path) -> None:
    _a_tasks_file_under_the_screaming_catch_all(root)
    (root / TASKS).unlink()


QUEUES_BY_NAME = "modules/comprobante/service.py"


def modules_with_a_task_queued_by_name(root: Path, *, declared: bool) -> None:
    """Cuadra's shape: `comprobante` queues a job whose handler `alerta` owns."""
    service(root, "observability", "database", "queue")
    deps = '["alerta"]' if declared else "[]"
    write(
        root / "contracts.toml",
        PLACEMENT_CONTRACT + f"\n[modules.comprobante]\ndepends_on = {deps}\n",
    )
    write(root / "modules" / "alerta" / "__init__.py", "")
    write(
        root / "modules" / "alerta" / "public.py",
        "def nothing() -> None:\n    return None\n",
    )
    write(
        root / QUEUES_BY_NAME,
        "from jfastframework.queues.base import Job\n\n\n"
        "def revisar() -> Job:\n"
        '    return Job(task="alerta.revisar_presupuesto", payload={})\n',
    )


@affected_by("contracts-event-rules")
def _a_job_queued_by_name_for_another_modules_task(root: Path) -> list[str]:
    modules_with_a_task_queued_by_name(root, declared=False)
    return [f"{QUEUES_BY_NAME}:5 undeclared-dependency: module 'comprobante' queues task"]


SUBSCRIBER = "modules/alerta/tasks.py"


@affected_by("contracts-event-rules")
def _a_subscription_to_an_event_nobody_declares(root: Path) -> list[str]:
    service(root, "observability", "database", "queue")
    write(root / "contracts.toml", PLACEMENT_CONTRACT)
    write(root / "modules" / "alerta" / "__init__.py", "")
    write(
        root / SUBSCRIBER,
        "from jfastframework.events import subscribe\n\n\n"
        '@subscribe("comprobante.registrado")\n'
        "async def revisar(event: object) -> None:\n"
        "    return None\n",
    )
    return [f"{SUBSCRIBER}:4 orphan-subscription: "]


@unaffected_by("contracts-event-rules")
def _the_task_owner_declared_in_depends_on(root: Path) -> None:
    modules_with_a_task_queued_by_name(root, declared=True)


@unaffected_by("contracts-event-rules")
def _the_event_declared_by_its_publisher(root: Path) -> None:
    _a_subscription_to_an_event_nobody_declares(root)
    write(
        root / "contracts.toml",
        PLACEMENT_CONTRACT + '\n[modules.comprobante]\npublishes = ["comprobante.registrado"]\n',
    )
    write(root / "modules" / "comprobante" / "__init__.py", "")


PUBLISHER = "modules/comprobante/service.py"


@affected_by("publish-without-receiver")
def _an_event_with_no_subscriber_and_no_bus(root: Path) -> list[str]:
    service(root, "observability", "database", "queue", "outbox")
    write(
        root / PUBLISHER,
        "from jfastframework.events import Event\n\n\n"
        "def registrado() -> Event:\n"
        '    return Event(type="comprobante.registrado", data={})\n',
    )
    return [f'{PUBLISHER}:5 Event(type="comprobante.registrado")']


@unaffected_by("publish-without-receiver")
def _the_same_event_with_a_local_subscriber(root: Path) -> None:
    _an_event_with_no_subscriber_and_no_bus(root)
    write(
        root / SUBSCRIBER,
        "from jfastframework.events import subscribe\n\n\n"
        '@subscribe("comprobante.registrado")\n'
        "async def revisar(event: object) -> None:\n"
        "    return None\n",
    )


@unaffected_by("publish-without-receiver")
def _the_same_event_with_a_bus_for_other_services(root: Path) -> None:
    _an_event_with_no_subscriber_and_no_bus(root)
    edit(root / "jfast.toml", '"outbox"]', '"outbox", "events"]')


BUS = "shared/bus.py"


@affected_by("outbox-manual-construction")
def _an_outbox_built_with_only_a_queue(root: Path) -> list[str]:
    service(root, "observability", "database", "queue")
    write(
        root / BUS,
        "from jfastframework.outbox import Outbox\n\n"
        "def build(queue):\n"
        "    return Outbox(queue=queue)\n",
    )
    return [f"{BUS}:4 Outbox(...) without events="]


@unaffected_by("outbox-manual-construction")
def _the_same_outbox_given_the_bus(root: Path) -> None:
    _an_outbox_built_with_only_a_queue(root)
    edit(root / BUS, "Outbox(queue=queue)", "Outbox(queue=queue, events=None)")


@unaffected_by("outbox-manual-construction")
def _a_class_of_the_projects_own_named_outbox(root: Path) -> None:
    _an_outbox_built_with_only_a_queue(root)
    edit(root / BUS, "from jfastframework.outbox import Outbox", "from shared.mail import Outbox")


ROOT_WORKER = (
    "from jfastframework.queues.worker import Worker\n\n"
    "from main import app\n\n\n"
    "async def main(ctx) -> None:\n"
    "    tareas = ctx.require('tasks')\n\n"
    '    @tareas.task("alerta.revisar_presupuesto")\n'
    "    async def revisar(payload: dict) -> None:\n"
    "        return None\n\n"
    "    await Worker(ctx.require('queue'), tareas, concurrency=2).run()\n"
)


@affected_by("worker-py-to-module-tasks")
def _a_root_worker_py_like_cuadras(root: Path) -> list[str]:
    service(root, "observability", "database", "queue")
    write(root / "worker.py", ROOT_WORKER)
    return ["worker.py:9 tareas.task(...)", "worker.py:13 Worker(...)"]


@unaffected_by("worker-py-to-module-tasks")
def _the_handler_moved_into_the_module(root: Path) -> None:
    service(root, "observability", "database", "queue")
    write(
        root / "modules" / "alerta" / "tasks.py",
        "from jfastframework.tasks import task\n\n\n"
        '@task("alerta.revisar_presupuesto")\n'
        "async def revisar(payload: dict) -> None:\n"
        "    return None\n",
    )


@unaffected_by("worker-py-to-module-tasks")
def _a_root_script_that_runs_no_worker(root: Path) -> None:
    service(root, "observability", "database", "queue")
    write(root / "manage.py", "import sys\n\nprint(sys.argv)\n")


@affected_by("worker-drain-timeout")
def _a_worker_on_the_default_drain_window(root: Path) -> list[str]:
    service(root, "observability", "queue")
    write(root / "worker.py", ROOT_WORKER)
    return ["worker.py:13 Worker(...) drains for 25s"]


@unaffected_by("worker-drain-timeout")
def _the_same_worker_with_a_window_of_its_own(root: Path) -> None:
    _a_worker_on_the_default_drain_window(root)
    edit(root / "worker.py", "concurrency=2)", "concurrency=2, drain_timeout=120)")


@unaffected_by("worker-drain-timeout")
def _a_worker_class_of_the_projects_own(root: Path) -> None:
    _a_worker_on_the_default_drain_window(root)
    edit(
        root / "worker.py",
        "from jfastframework.queues.worker import Worker",
        "from shared.pool import Worker",
    )


QUEUE_COMPOSE = STALE_COMPOSE + "    environment:\n      JFAST_DB_DSN: postgresql://db/app\n"


@affected_by("regenerate-deploy-for-worker")
def _a_compose_file_with_a_queue_and_no_worker(root: Path) -> list[str]:
    service(root, "observability", "database", "queue")
    write(root / "docker-compose.yml", QUEUE_COMPOSE)
    return ["docker-compose.yml: no worker container"]


@affected_by("regenerate-deploy-for-worker")
def _kubernetes_manifests_with_no_worker(root: Path) -> list[str]:
    service(root, "observability", "database", "queue")
    write(root / "k8s" / "api.yaml", "kind: Deployment\nmetadata:\n  name: billing-api\n")
    return ["k8s/: no worker container"]


@unaffected_by("regenerate-deploy-for-worker")
def _the_same_compose_file_with_a_worker(root: Path) -> None:
    _a_compose_file_with_a_queue_and_no_worker(root)
    write(
        root / "docker-compose.yml",
        QUEUE_COMPOSE + "  worker:\n    build: .\n    command: [jfast, worker, --grace=25]\n",
    )


@unaffected_by("regenerate-deploy-for-worker")
def _a_compose_file_for_a_service_without_a_queue(root: Path) -> None:
    _a_compose_file_with_a_queue_and_no_worker(root)
    edit(root / "jfast.toml", ', "queue"]', "]")


def workspace_file(root: Path, *services: tuple[str, str, str]) -> None:
    """`jfast.workspace.toml` listing `(name, kind, path)` services."""
    blocks = "".join(
        f'\n[[workspace.services]]\nname = "{name}"\nkind = "{kind}"\npath = "{path}"\n'
        + f"port = {8000 + 10 * index}\n"
        + ('frontend = "vue"\n' if kind == "spa" else "")
        for index, (name, kind, path) in enumerate(services)
    )
    write(
        root / "jfast.workspace.toml",
        '[workspace]\nname = "shop"\nbase_port = 7990\n' + blocks,
    )


@affected_by("workspace-compose-client-env")
def _a_workspace_compose_without_the_database_address(root: Path) -> list[str]:
    service(root, "observability", "database")
    workspace_file(root, ("billing", "api", "."))
    write(root / "docker-compose.yml", "services:\n  billing:\n    build: .\n")
    return ["docker-compose.yml: no internal address for JFAST_DB_DSN"]


@unaffected_by("workspace-compose-client-env")
def _the_same_workspace_compose_regenerated(root: Path) -> None:
    _a_workspace_compose_without_the_database_address(root)
    write(
        root / "docker-compose.yml",
        "services:\n  billing:\n    build: .\n    environment:\n"
        "      JFAST_DB_DSN: postgresql+asyncpg://app:app@billing-database:5432/app\n",
    )


@unaffected_by("workspace-compose-client-env")
def _a_workspace_that_does_not_list_this_service(root: Path) -> None:
    # A workspace file above an unrelated checkout says nothing about it.
    _a_workspace_compose_without_the_database_address(root)
    workspace_file(root, ("other", "api", "other"))


MIGRATION = "migrations/versions/0001_grants.py"


@affected_by("framework-tables-altered-at-startup")
def _migrations_that_manage_the_queue_table(root: Path) -> list[str]:
    service(root, "observability", "database", "queue")
    write(
        root / MIGRATION,
        'def upgrade() -> None:\n    op.execute("GRANT SELECT, INSERT ON jfast_jobs TO app")\n',
    )
    return [f"{MIGRATION}:2 names jfast_jobs"]


@affected_by("framework-tables-altered-at-startup")
def _sql_that_creates_the_users_table(root: Path) -> list[str]:
    service(root, "observability", "database", "auth", "accounts")
    write(root / "db" / "schema.sql", "-- owned by the migrator\nCREATE TABLE jfast_users ();\n")
    return ["db/schema.sql:2 names jfast_users"]


@unaffected_by("framework-tables-altered-at-startup")
def _the_queue_on_redis(root: Path) -> None:
    _migrations_that_manage_the_queue_table(root)
    edit(
        root / "jfast.toml",
        "disabled = []\n",
        'disabled = []\n\n[plugin.queue]\nbackend = "redis"\n',
    )


@unaffected_by("framework-tables-altered-at-startup")
def _migrations_that_never_name_a_framework_table(root: Path) -> None:
    _migrations_that_manage_the_queue_table(root)
    edit(root / MIGRATION, "jfast_jobs", "invoices")


# ---------------------------------------------------------------------------
# 0.1.0a11: accounts and the frontend
# ---------------------------------------------------------------------------


def accounts_service(root: Path, *plugins: str, settings: str = "") -> None:
    service(
        root,
        "observability",
        "database",
        "auth",
        "accounts",
        *plugins,
        extra=f"\n[plugin.accounts]\n{settings}",
    )


def vite_project(root: Path, files: Mapping[str, str]) -> None:
    write(root / "package.json", '{"name": "web", "private": true}\n')
    for name, text in files.items():
        write(root / name, text)


STORE_A10 = """\
export const useAuthStore = defineStore('auth', {
  actions: {
    async login(credentials) {
      const { data } = await api.post('/auth/login', credentials)
      this.$patch({ token: data.access_token, user: data.user ?? null })
    },
  },
})
"""


@affected_by("frontend-login-without-account")
def _a_store_that_takes_the_user_from_login(root: Path) -> list[str]:
    accounts_service(root)
    vite_project(root / "web", {"src/stores/auth.store.js": STORE_A10})
    return ["web/src/stores/auth.store.js:5 reads data.user from /auth/login"]


@unaffected_by("frontend-login-without-account")
def _the_same_store_asking_for_the_account(root: Path) -> None:
    _a_store_that_takes_the_user_from_login(root)
    edit(
        root / "web" / "src" / "stores" / "auth.store.js",
        "user: data.user ?? null })",
        "user: (await api.get('/auth/account')).data })",
    )


@unaffected_by("frontend-login-without-account")
def _the_same_store_over_a_service_without_accounts(root: Path) -> None:
    # Without accounts the login endpoint is the project's own, and may well
    # return the user.
    _a_store_that_takes_the_user_from_login(root)
    edit(root / "jfast.toml", ', "accounts"]', "]")


def test_a_frontend_beside_the_service_in_its_workspace_is_read(tmp_path: Path) -> None:
    """`jfast start` puts the frontend next to the API, not inside it."""
    accounts_service(tmp_path / "shop")
    workspace_file(tmp_path, ("shop", "api", "shop"), ("shop_web", "spa", "shop-web"))
    vite_project(tmp_path / "shop-web", {"src/stores/auth.store.js": STORE_A10})
    assert report(tmp_path / "shop")["frontend-login-without-account"] == [
        "../shop-web/src/stores/auth.store.js:5 reads data.user from /auth/login"
    ]


ROUTER_A10 = "const PUBLIC_BY_DEFAULT = true\n\nexport default router\n"


@affected_by("frontend-public-by-default")
def _a_router_open_by_default_over_accounts(root: Path) -> list[str]:
    accounts_service(root)
    vite_project(root / "web", {"src/router/index.js": ROUTER_A10})
    return ["web/src/router/index.js:1 PUBLIC_BY_DEFAULT = true"]


@unaffected_by("frontend-public-by-default")
def _the_same_router_closed_by_default(root: Path) -> None:
    _a_router_open_by_default_over_accounts(root)
    edit(root / "web" / "src" / "router" / "index.js", "= true", "= false")


@unaffected_by("frontend-public-by-default")
def _an_open_router_over_a_service_without_accounts(root: Path) -> None:
    _a_router_open_by_default_over_accounts(root)
    edit(root / "jfast.toml", ', "accounts"]', "]")


API_A10 = "const config = {\n  baseURL: import.meta.env.VITE_API_URL,\n  timeout: 30000,\n}\n"


@affected_by("frontend-api-timeout")
def _a_thirty_second_client_over_an_upload_service(root: Path) -> list[str]:
    service(root, "observability", "database", "storage")
    vite_project(root / "web", {"src/services/api.js": API_A10})
    return [
        "web/src/services/api.js:3 timeout: 30000 -- "
        f"this API answers for up to {UPLOAD_REQUEST_TIMEOUT:g}s"
    ]


@unaffected_by("frontend-api-timeout")
def _the_same_client_reading_the_timeout_from_the_env(root: Path) -> None:
    _a_thirty_second_client_over_an_upload_service(root)
    edit(
        root / "web" / "src" / "services" / "api.js",
        "timeout: 30000",
        "timeout: Number(import.meta.env.VITE_API_TIMEOUT) || 60000",
    )


@unaffected_by("frontend-api-timeout")
def _a_thirty_second_client_over_a_thirty_second_api(root: Path) -> None:
    # The two agree: the client gives up when the server does.
    _a_thirty_second_client_over_an_upload_service(root)
    edit(root / "jfast.toml", ', "storage"]', "]")


@affected_by("accounts-verification-required")
def _verification_switched_on_as_required(root: Path) -> list[str]:
    accounts_service(root, settings='email_verification = "required"\n')
    return ['[plugin.accounts] email_verification = "required"']


@unaffected_by("accounts-verification-required")
def _verification_optional(root: Path) -> None:
    # Optional never refuses a sign-in, so nobody is locked out.
    accounts_service(root, settings='email_verification = "optional"\n')


@affected_by("accounts-mfa-login-challenge")
def _mfa_on_and_required_for_a_role(root: Path) -> list[str]:
    accounts_service(root, settings='mfa = true\nmfa_required_roles = ["admin"]\n')
    return ["[plugin.accounts] mfa = true", "[plugin.accounts] mfa_required_roles = ['admin']"]


@unaffected_by("accounts-mfa-login-challenge")
def _mfa_left_off(root: Path) -> None:
    accounts_service(root, settings="mfa = false\n")


@affected_by("accounts-sign-in-rate-limit")
def _accounts_with_redis_to_count_in(root: Path) -> list[str]:
    accounts_service(root, "cache")
    from jfastframework.plugins.builtin.accounts import AccountsSettings

    per_ip = AccountsSettings.model_fields["login_limit_per_ip"].default
    return [f"[plugin.accounts] rate_limit defaults to true: {per_ip} sign-ins per IP"]


@unaffected_by("accounts-sign-in-rate-limit")
def _accounts_that_chose_no_rate_limit(root: Path) -> None:
    accounts_service(root, "cache", settings="rate_limit = false\n")


@unaffected_by("accounts-sign-in-rate-limit")
def _accounts_with_no_cache(root: Path) -> None:
    # The limiter counts in Redis; without one it does not run.
    accounts_service(root)


# ---------------------------------------------------------------------------
# 0.1.0a11: settings, tenancy, resilience
# ---------------------------------------------------------------------------


@affected_by("settings-refused-at-boot")
def _database_settings_that_never_worked(root: Path) -> list[str]:
    service(
        root,
        "observability",
        "database",
        extra='\n[plugin.database]\npool_size = 0\nsession_timezone = "Mars/Olympus"\n'
        'tenant_dsn_template = "postgresql://db/app"\n',
    )
    return [
        "[plugin.database] pool_size = 0",
        '[plugin.database] session_timezone = "Mars/Olympus"',
        "[plugin.database] tenant_dsn_template has no {tenant}",
    ]


@affected_by("settings-refused-at-boot")
def _plugin_settings_that_fail_on_first_use(root: Path) -> list[str]:
    service(
        root,
        "observability",
        "database",
        "queue",
        "auth",
        "mail",
        "storage",
        extra='\n[plugin.storage.disks.files]\ndriver = "s3"\nbucket = "b"\naccess_key = "k"\n'
        'visibility = "internal"\n'
        '\n[plugin.queue]\nname = "jobs-queue"\n'
        '\n[plugin.auth]\nmode = "public_key"\nissue_tokens = true\nalgorithms = ["RS999"]\n'
        '\n[plugin.mail]\nfrom_email = "noreply"\n',
    )
    return [
        '[plugin.storage.disks.files] visibility = "internal"',
        "[plugin.storage.disks.files] sets one of access_key and secret_key",
        '[plugin.queue] name = "jobs-queue"',
        '[plugin.auth] mode = "public_key" with issue_tokens = true',
        "[plugin.auth] algorithms has RS999",
        '[plugin.mail] from_email = "noreply"',
    ]


@affected_by("settings-refused-at-boot")
def _production_values_from_the_file(root: Path) -> list[str]:
    service(
        root,
        "observability",
        "auth",
        extra='\n[plugin.auth]\nmode = "jwks"\njwks_url = "http://idp.internal/jwks"\n',
    )
    edit(root / "jfast.toml", 'env = "local"', 'env = "prod"')
    return ["[plugin.auth] jwks_url is plain http, in production"]


@unaffected_by("settings-refused-at-boot")
def _the_same_values_corrected(root: Path) -> None:
    _plugin_settings_that_fail_on_first_use(root)
    path = root / "jfast.toml"
    edit(path, 'access_key = "k"\n', "")
    edit(path, '"internal"', '"private"')
    edit(path, '"jobs-queue"', '"jobs_queue"')
    edit(path, 'mode = "public_key"\nissue_tokens = true', 'mode = "secret"\nissue_tokens = true')
    edit(path, '["RS999"]', '["HS256"]')
    edit(path, '"noreply"', '"noreply@example.com"')


@unaffected_by("settings-refused-at-boot")
def _plain_http_jwks_outside_production(root: Path) -> None:
    # A laptop talks to a local identity service over http; only prod refuses.
    _production_values_from_the_file(root)
    edit(root / "jfast.toml", 'env = "prod"', 'env = "local"')


def _config_line(root: Path, text: str) -> int:
    lines = (root / "jfast.toml").read_text(encoding="utf-8").splitlines()
    return next(number for number, line in enumerate(lines, start=1) if line.strip() == text)


@affected_by("tenancy-consistency-check")
def _a_tenant_scoped_rag_with_nothing_to_resolve_a_tenant(root: Path) -> list[str]:
    rag_service(root)
    line = _config_line(root, "[plugin.rag]")
    return [f"jfast.toml:{line} tenancy-rag-scoped-without-tenancy (medium)"]


@unaffected_by("tenancy-consistency-check")
def _the_same_rag_declared_single_tenant(root: Path) -> None:
    rag_service(root, "tenant_scoped = false\n")


@unaffected_by("tenancy-consistency-check")
def _the_same_rag_with_tenancy_on(root: Path) -> None:
    _a_tenant_scoped_rag_with_nothing_to_resolve_a_tenant(root)
    edit(root / "jfast.toml", '"rag"]', '"rag", "auth", "tenancy"]')
    write(
        root / "jfast.toml",
        (root / "jfast.toml").read_text() + '\n[plugin.tenancy]\nsources = ["token"]\n',
    )


GATEWAY_CLIENT = (
    "import httpx\n\n\n"
    "async def forward(client: httpx.AsyncClient, tenant: str) -> None:\n"
    '    await client.get("/items", headers={"X-Tenant-ID": tenant})\n'
)


@affected_by("tenant-header-not-a-tenant")
def _code_sending_the_tenant_header_without_tenancy(root: Path) -> list[str]:
    service(root, "observability", "auth", extra=AUTH_ISSUING)
    write(root / "shared" / "gateway_client.py", GATEWAY_CLIENT)
    return ["shared/gateway_client.py:5 "]


@affected_by("tenant-header-not-a-tenant")
def _a_configured_tenant_header(root: Path) -> list[str]:
    service(root, "observability", extra='\n[plugin.observability]\ntenant_header = "X-Org"\n')
    return ["[plugin.observability] tenant_header = 'X-Org'"]


@unaffected_by("tenant-header-not-a-tenant")
def _tenancy_that_lists_the_header(root: Path) -> None:
    _code_sending_the_tenant_header_without_tenancy(root)
    edit(root / "jfast.toml", '"auth"', '"auth", "tenancy"')
    write(
        root / "jfast.toml",
        (root / "jfast.toml").read_text() + '\n[plugin.tenancy]\nsources = ["token", "header"]\n',
    )


@unaffected_by("tenant-header-not-a-tenant")
def _the_header_only_in_a_comment_or_a_test(root: Path) -> None:
    service(root, "observability", "auth", extra=AUTH_ISSUING)
    write(root / "shared" / "notes.py", "# X-Tenant-ID is not trusted here\n")
    write(root / "tests" / "test_x.py", GATEWAY_CLIENT)


OLD_DOCKERFILE = (
    "FROM python:3.12-slim\nWORKDIR /app\n"
    "RUN useradd --create-home --uid 10001 appuser\n"
    "COPY --chown=appuser:appuser . /app\nUSER appuser\n"
)


@affected_by("image-cannot-write-local-storage")
def _a_local_disk_in_an_image_that_leaves_app_to_root(root: Path) -> list[str]:
    service(root, "observability", "storage")
    write(root / "Dockerfile", OLD_DOCKERFILE)
    return ["Dockerfile: USER appuser"]


@unaffected_by("image-cannot-write-local-storage")
def _the_regenerated_dockerfile(root: Path) -> None:
    from jfastframework.deploy.compose import render_dockerfile

    service(root, "observability", "storage")
    write(root / "Dockerfile", render_dockerfile())


@unaffected_by("image-cannot-write-local-storage")
def _only_object_storage_disks(root: Path) -> None:
    service(
        root,
        "observability",
        "storage",
        extra='\n[plugin.storage.disks.files]\ndriver = "s3"\nbucket = "b"\n',
    )
    write(root / "Dockerfile", OLD_DOCKERFILE)


ADJUNTOS = (
    '\n[plugin.storage.disks.public]\ndriver = "local"\nroot = "storage/public"\n'
    '\n[plugin.storage.disks.private]\ndriver = "local"\nroot = "storage/private"\n'
    '\n[plugin.storage.disks.adjuntos]\ndriver = "local"\nroot = "storage/adjuntos"\n'
)

#: What the first 0.1.0a12 build generated: /app handed over, the two default
#: disks created, and nothing for a disk of the project's own.
A12_STORAGE_LINES = (
    "FROM python:3.12-slim\nWORKDIR /app\n"
    "RUN useradd --create-home --uid 10001 appuser\n"
    "COPY --chown=appuser:appuser . /app\n"
    "RUN mkdir -p /app/storage/public /app/storage/private \\\n"
    " && chown appuser:appuser /app /app/storage /app/storage/public /app/storage/private\n"
    "USER appuser\n"
)


@affected_by("image-cannot-write-local-storage")
def _a_disk_of_its_own_the_dockerfile_never_creates(root: Path) -> list[str]:
    """The help desk's `adjuntos` disk (bitácora F13): /ready 503, upload 500."""
    service(root, "observability", "storage", extra=ADJUNTOS)
    write(root / "Dockerfile", A12_STORAGE_LINES)
    return ["Dockerfile: /app/storage/adjuntos (disk adjuntos) is not created for appuser"]


@affected_by("image-cannot-write-local-storage")
def _a_disk_created_but_left_to_root(root: Path) -> list[str]:
    service(root, "observability", "storage", extra=ADJUNTOS)
    write(root / "Dockerfile", A12_STORAGE_LINES)
    edit(
        root / "Dockerfile",
        "mkdir -p /app/storage/public /app/storage/private",
        "mkdir -p /app/storage/public /app/storage/private /app/storage/adjuntos",
    )
    return ["Dockerfile: /app/storage/adjuntos (disk adjuntos)"]


@unaffected_by("image-cannot-write-local-storage")
def _the_dockerfile_regenerated_from_the_disks(root: Path) -> None:
    from jfastframework.deploy.compose import read_storage_disks, render_dockerfile

    service(root, "observability", "storage", extra=ADJUNTOS)
    write(root / "Dockerfile", render_dockerfile(disks=read_storage_disks(root / "jfast.toml")))


@unaffected_by("image-cannot-write-local-storage")
def _the_storage_line_refreshed_by_jfast_add_storage(root: Path) -> None:
    from jfastframework.deploy.compose import read_storage_disks, refresh_storage_block

    service(root, "observability", "storage", extra=ADJUNTOS)
    refreshed = refresh_storage_block(A12_STORAGE_LINES, read_storage_disks(root / "jfast.toml"))
    assert refreshed is not None
    write(root / "Dockerfile", refreshed)


@unaffected_by("image-cannot-write-local-storage")
def _the_whole_storage_tree_handed_over_recursively(root: Path) -> None:
    service(root, "observability", "storage", extra=ADJUNTOS)
    write(root / "Dockerfile", A12_STORAGE_LINES)
    edit(
        root / "Dockerfile",
        "RUN mkdir -p /app/storage/public /app/storage/private \\\n"
        " && chown appuser:appuser /app /app/storage /app/storage/public "
        "/app/storage/private\n",
        "RUN mkdir -p /app/storage/adjuntos && chown -R appuser:appuser /app\n",
    )


@unaffected_by("image-cannot-write-local-storage")
def _a_second_disk_on_object_storage(root: Path) -> None:
    service(root, "observability", "storage", extra=ADJUNTOS)
    edit(
        root / "jfast.toml",
        'disks.adjuntos]\ndriver = "local"\nroot = "storage/adjuntos"',
        'disks.adjuntos]\ndriver = "s3"\nbucket = "adjuntos"',
    )
    write(root / "Dockerfile", A12_STORAGE_LINES)


NULLABLE_UNIQUE_ENTITY = (
    "from sqlalchemy import UniqueConstraint\n"
    "from sqlalchemy.orm import Mapped, mapped_column\n\n\n"
    "class Gasto(Base):\n"
    "    __tablename__ = 'gastos'\n"
    "    __table_args__ = (\n"
    "        UniqueConstraint('tenant_id', 'folio', name='uq_gastos_folio',\n"
    "                         postgresql_nulls_not_distinct=True),\n"
    "    )\n"
    "    folio: Mapped[str | None] = mapped_column(nullable=True)\n"
    "    total: Mapped[int] = mapped_column()\n"
)


@affected_by("unique-key-on-optional-field")
def _a_generated_key_over_an_optional_field(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(root / "modules" / "gasto" / "models.py", NULLABLE_UNIQUE_ENTITY)
    return ["modules/gasto/models.py:8 Gasto: folio"]


@unaffected_by("unique-key-on-optional-field")
def _a_key_over_a_required_field(root: Path) -> None:
    service(root, "observability", "database")
    write(
        root / "modules" / "gasto" / "models.py",
        NULLABLE_UNIQUE_ENTITY.replace(
            "Mapped[str | None] = mapped_column(nullable=True)", "Mapped[str] = mapped_column()"
        ),
    )


A11_FACADE = (
    "from __future__ import annotations\n\n"
    "from .repositories import InvoiceRepository\n\n\n"
    "async def get_invoice(\n"
    "    session: AsyncSession, *, tenant_id: str | None, invoice_id: int\n"
    ") -> InvoiceSummary | None:\n"
    "    return await InvoiceRepository(session, tenant_id=tenant_id).get(invoice_id)\n"
)


@affected_by("facade-tenant-optional")
def _a_multitenant_facade_that_admits_none(root: Path) -> list[str]:
    service(root, "observability", "database", "auth", "tenancy")
    write(root / "modules" / "invoice" / "public.py", A11_FACADE)
    return ["modules/invoice/public.py:7 get_invoice(tenant_id: str | None)"]


@unaffected_by("facade-tenant-optional")
def _the_facade_requiring_the_tenant(root: Path) -> None:
    _a_multitenant_facade_that_admits_none(root)
    edit(root / "modules" / "invoice" / "public.py", "tenant_id: str | None", "tenant_id: str")


@unaffected_by("facade-tenant-optional")
def _the_same_facade_in_a_single_tenant_service(root: Path) -> None:
    # One customer: its rows carry no tenant, so None is the value that finds them.
    _a_multitenant_facade_that_admits_none(root)
    edit(root / "jfast.toml", ', "tenancy"', "")


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_a_facade_generated_for_a_multitenant_service_is_not_told(
    tmp_path: Path, layout: str
) -> None:
    service(tmp_path, "observability", "database", "auth", "tenancy")
    Scaffolder().render_trees(
        module_trees(layout, "api", tmp_path / "modules", tmp_path),
        module_context("orders", layout=layout, access="tenant"),
    )
    assert "facade-tenant-optional" not in report(tmp_path)


@affected_by("jfast-env-wins-over-the-file")
def _the_env_jfast_start_wrote(root: Path) -> list[str]:
    # `service` writes the [app] block every 0.1.0a11 project has.
    service(root, "observability")
    return ['jfast.toml:4 [app] env = "local": JFAST_ENV in the environment now wins']


@affected_by("jfast-env-wins-over-the-file")
def _debug_committed_on(root: Path) -> list[str]:
    service(root, "observability")
    edit(root / "jfast.toml", 'env = "local"\n', "debug = true\n")
    return ["jfast.toml:4 [app] debug = true: JFAST_DEBUG in the environment now wins"]


@unaffected_by("jfast-env-wins-over-the-file")
def _the_line_removed(root: Path) -> None:
    _the_env_jfast_start_wrote(root)
    edit(root / "jfast.toml", 'env = "local"\n', "")


@unaffected_by("jfast-env-wins-over-the-file")
def _env_only_in_another_table(root: Path) -> None:
    # A plugin's own `env` key is not the deployment's.
    _the_line_removed(root)
    write(root / "jfast.toml", (root / "jfast.toml").read_text() + '\n[plugin.x]\nenv = "a"\n')


TENANT_BY_SUBDOMAIN = (
    '\n[plugin.tenancy]\nsources = ["token", "subdomain"]\nbase_domain = "localhost"\n'
)


@affected_by("unsigned-tenant-needs-a-session")
def _auth_and_a_subdomain_tenant(root: Path) -> list[str]:
    # The help desk of F1: the documented example, anonymous rows by Host header.
    service(root, "observability", "database", "auth", "tenancy", extra=TENANT_BY_SUBDOMAIN)
    return ['[plugin.tenancy] sources = ["token", "subdomain"]: subdomain no longer grants']


@affected_by("unsigned-tenant-needs-a-session")
def _auth_and_the_default_sources(root: Path) -> list[str]:
    # No `sources` line: the plugin's default is ["token", "subdomain"].
    service(
        root,
        "observability",
        "auth",
        "tenancy",
        extra='\n[plugin.tenancy]\nbase_domain = "app.example.com"\n',
    )
    return ['[plugin.tenancy] sources = ["token", "subdomain"] (the default): subdomain']


@unaffected_by("unsigned-tenant-needs-a-session")
def _only_signed_sources(root: Path) -> None:
    # What `jfast start --multitenant` writes: nothing a client can choose.
    _auth_and_a_subdomain_tenant(root)
    edit(root / "jfast.toml", '["token", "subdomain"]', '["token", "user"]')


@unaffected_by("unsigned-tenant-needs-a-session")
def _a_public_site_without_auth(root: Path) -> None:
    # No principal to check against: the subdomain's tenant stays usable.
    _auth_and_a_subdomain_tenant(root)
    edit(root / "jfast.toml", '"auth", ', "")


@unaffected_by("unsigned-tenant-needs-a-session")
def _a_subdomain_source_without_a_base_domain(root: Path) -> None:
    # Refused at boot before and after: nothing changed for it at runtime.
    _auth_and_a_subdomain_tenant(root)
    edit(root / "jfast.toml", 'base_domain = "localhost"\n', "")


@affected_by("revocation-fail-open")
def _auth_checking_revocation_against_redis(root: Path) -> list[str]:
    service(root, "observability", "database", "cache", "auth", extra=AUTH_ISSUING)
    return ["[plugin.auth] revocation_fail_open is not set"]


@unaffected_by("revocation-fail-open")
def _auth_that_chose_to_fail_closed(root: Path) -> None:
    _auth_checking_revocation_against_redis(root)
    edit(
        root / "jfast.toml",
        "issue_tokens = true",
        "issue_tokens = true\nrevocation_fail_open = false",
    )


@unaffected_by("revocation-fail-open")
def _auth_on_a_store_that_cannot_go_down(root: Path) -> None:
    # In memory, per process: there is no outage to fail open through.
    _auth_checking_revocation_against_redis(root)
    edit(root / "jfast.toml", '"cache", ', "")


LIMITER = "shared/limiter.py"


@affected_by("redis-command-timeout")
def _a_lua_script_on_the_cache_client(root: Path) -> list[str]:
    service(root, "observability", "cache")
    write(
        root / LIMITER,
        "def hit(ctx, key: str) -> int:\n"
        '    client = ctx.require("cache.client")\n'
        "    return client.eval(SCRIPT, 1, key)\n",
    )
    return [f"{LIMITER}:3 .eval(...)"]


@unaffected_by("redis-command-timeout")
def _the_deadline_raised_for_it(root: Path) -> None:
    _a_lua_script_on_the_cache_client(root)
    write(
        root / "jfast.toml",
        (root / "jfast.toml").read_text() + "\n[plugin.cache]\ncommand_timeout = 5.0\n",
    )


@unaffected_by("redis-command-timeout")
def _an_eval_that_is_not_redis(root: Path) -> None:
    # `eval` is an ordinary method name; a file that never reaches Redis is
    # not guessed at.
    _a_lua_script_on_the_cache_client(root)
    edit(root / LIMITER, 'ctx.require("cache.client")', "ctx.rules")


HANDLERS = "main.py"


@affected_by("database-unavailable-503")
def _a_handler_of_the_projects_own_for_operational_errors(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(
        root / HANDLERS,
        "from sqlalchemy.exc import OperationalError\n\n"
        "from jfastframework import create_app\n\n"
        "app = create_app()\n"
        "app.add_exception_handler(OperationalError, on_database_error)\n",
    )
    return [f"{HANDLERS}:6 handles OperationalError"]


@unaffected_by("database-unavailable-503")
def _a_handler_for_something_else(root: Path) -> None:
    _a_handler_of_the_projects_own_for_operational_errors(root)
    edit(
        root / HANDLERS, "add_exception_handler(OperationalError", "add_exception_handler(KeyError"
    )


@affected_by("database-behind-pgbouncer")
def _a_dsn_through_pgbouncer(root: Path) -> list[str]:
    service(root, "observability", "database")
    write(
        root / ".env",
        "# local\nJFAST_DB_DSN=postgresql+asyncpg://app:app@pgbouncer:6432/app\n",
    )
    return [".env:2 a DSN through a pooler"]


@unaffected_by("database-behind-pgbouncer")
def _the_same_dsn_with_the_setting_on(root: Path) -> None:
    _a_dsn_through_pgbouncer(root)
    write(
        root / "jfast.toml",
        (root / "jfast.toml").read_text() + "\n[plugin.database]\npgbouncer = true\n",
    )


@unaffected_by("database-behind-pgbouncer")
def _a_dsn_straight_to_postgres(root: Path) -> None:
    _a_dsn_through_pgbouncer(root)
    edit(root / ".env", "pgbouncer:6432", "db:5432")


# ---------------------------------------------------------------------------
# 0.1.0a11: a project this release generates is told nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tenancy", ["--single-tenant", "--multitenant"])
def test_a_project_jfast_start_generates_is_told_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenancy: str
) -> None:
    """Every layout, the workspace compose and the frontend, pinned to 0.1.0a10.

    Everything on disk is what this release writes, so every note in range --
    all of 0.1.0a11's -- must stay quiet: a note that fires on the generator's
    own output is one nobody can act on.
    """
    from typer.testing import CliRunner

    from jfastframework.cli.main import app

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["start", "shop", tenancy])
    assert result.exit_code == 0, result.output
    api = tmp_path / "shop"
    monkeypatch.chdir(api)
    for layout in MODULE_LAYOUTS:
        made = runner.invoke(app, ["new", "module", f"m_{layout}", "--layout", layout])
        assert made.exit_code == 0, made.output
    write(api / "requirements.txt", "jfastframework[db,server]==0.1.0a10\n")
    assert report(api) == {}


# ---------------------------------------------------------------------------
# Every change, both halves
# ---------------------------------------------------------------------------


def _cases(registry: Mapping[str, Sequence[Callable[[Path], object]]]) -> list[object]:
    return [
        pytest.param(code, build, id=f"{code}:{build.__name__.strip('_')}")
        for code, builders in registry.items()
        for build in builders
    ]


@pytest.mark.parametrize(("code", "build"), _cases(AFFECTED))
def test_an_affected_project_is_told_and_shown_where(
    tmp_path: Path, code: str, build: Affected
) -> None:
    expected = build(tmp_path)
    found = report(tmp_path)
    assert code in found, sorted(found)
    lines = found[code]
    assert len(lines) == len(expected), lines
    for line, start in zip(lines, expected, strict=True):
        assert line.startswith(start), f"{line!r} does not start with {start!r}"


@pytest.mark.parametrize(("code", "build"), _cases(CLEAN))
def test_the_nearest_clean_project_is_told_nothing(tmp_path: Path, code: str, build: Clean) -> None:
    build(tmp_path)
    found = report(tmp_path)
    assert code not in found, found.get(code)


def _predecessor(version: str) -> str:
    # Every note so far landed on an 0.1.0aN. A note on another shape of
    # version needs its own rule here, and failing says so.
    match = re.fullmatch(r"0\.1\.0a(\d+)", version)
    assert match, f"no predecessor rule for {version}"
    return f"0.1.0a{int(match.group(1)) - 1}"


@pytest.mark.parametrize(
    ("code", "build"),
    [pytest.param(code, builders[0], id=code) for code, builders in AFFECTED.items()],
)
def test_a_change_applies_only_between_the_versions_that_straddle_it(
    tmp_path: Path, code: str, build: Affected
) -> None:
    """`applicable` keeps `(current, installed]`, so each note has one edge on each side.

    Asserted on a project the detector does flag, so the only thing that can
    keep the note out is the version arithmetic.
    """
    build(tmp_path)
    project = load(tmp_path)
    landed = CHANGE[code].version
    before = _predecessor(landed)

    def codes(current: str, installed: str) -> set[str]:
        found = upgrades.applicable(project, current=current, installed=installed)
        return {change.code for change, _ in found}

    assert code in codes(before, landed), "a project one version behind must be told"
    assert code not in codes(landed, __version__), "a project already on it must not"
    assert code not in codes("0.1.0a0", before), "an install that predates it cannot have it"


def test_every_change_in_the_manifest_has_an_affected_and_a_clean_project() -> None:
    """The test that keeps this file honest as the manifest grows."""
    codes = [change.code for change in upgrades.CHANGES]
    assert len(codes) == len(set(codes)), "two changes share a code"
    assert sorted(set(codes) - set(AFFECTED)) == [], "changes with no affected project"
    assert sorted(set(codes) - set(CLEAN)) == [], "changes with no clean project"
    # And nothing registered under a code the manifest no longer has.
    assert sorted((set(AFFECTED) | set(CLEAN)) - set(codes)) == []
