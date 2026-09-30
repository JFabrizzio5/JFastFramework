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
