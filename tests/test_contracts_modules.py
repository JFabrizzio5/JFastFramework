"""How modules talk to each other: a facade, a declared graph, no foreign SQL.

"Queries through a facade, effects through events, nothing through shared."
Every rule below is one clause of that sentence made checkable:

* ``cross-module`` -- only ``modules/<other>/public.py`` may be imported;
* ``undeclared-dependency`` -- and only when ``depends_on`` lists it;
* ``module-cycle`` -- the declared-plus-imported graph has no cycle;
* ``public-leak`` -- the facade hands out DTOs, not entities, and knows no HTTP;
* ``cross-module-sql`` -- no raw SQL against a table another module owns.

The last test class generates real projects, one per layout, and requires the
generator's own output to pass -- a facade template that fails its own rules
would teach everyone to waive them on day one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app
from jfastframework.cli.scaffold import MODULE_LAYOUTS, Scaffolder, module_context, module_trees
from jfastframework.contracts import Contract, check, check_placement, render
from jfastframework.contracts.explain import RULES, diff, explain
from jfastframework.contracts.model import append_module_block

runner = CliRunner()

BASE = """
[project]
name = "cuadra"

[rules.placement]
enabled = true
"""

#: Module `comprobante`, as the generator would leave it: an entity, a
#: repository, and a facade returning a DTO.
COMPROBANTE = {
    "modules/comprobante/models.py": (
        "from jfastframework.db import Base\n\n\n"
        "class Comprobante(Base):\n"
        '    __tablename__ = "comprobantes"\n'
    ),
    "modules/comprobante/repository.py": "class ComprobanteRepository:\n    pass\n",
    "modules/comprobante/service.py": "class ComprobanteService:\n    pass\n",
    "modules/comprobante/enums.py": (
        "from enum import Enum\n\n\nclass Estado(str, Enum):\n    A = 'a'\n"
    ),
    "modules/comprobante/public.py": (
        "from dataclasses import dataclass\n\n"
        "from .repository import ComprobanteRepository\n\n\n"
        "@dataclass(frozen=True)\n"
        "class GastoPorCategoria:\n"
        "    categoria: str\n"
        "    total_centavos: int\n\n\n"
        "async def gasto_por_categoria(session, *, tenant_id):\n"
        "    return []\n"
    ),
    "modules/asesor/models.py": (
        "from jfastframework.db import Base\n\n\n"
        "class Conversacion(Base):\n"
        "    __tablename__: str = 'conversaciones'\n"
    ),
}


def build(
    tmp_path: Path, files: dict[str, str], *, extra: str = "", base: bool = True
) -> tuple[Contract, Path]:
    (tmp_path / "contracts.toml").write_text(BASE + extra, encoding="utf-8")
    for relative, body in {**(COMPROBANTE if base else {}), **files}.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return Contract.load(tmp_path / "contracts.toml"), tmp_path


def found(contract: Contract, root: Path, rule: str | None = None) -> list:  # type: ignore[type-arg]
    violations = check_placement(contract, root)
    return [v for v in violations if rule is None or v.rule == rule]


DEPENDS = '\n[modules.asesor]\ndepends_on = ["comprobante"]\n'
USES_FACADE = "from modules.comprobante.public import gasto_por_categoria\n"
USES_SERVICE = "from modules.comprobante.service import ComprobanteService\n"
USES_REPOSITORY = "from ..comprobante.repository import ComprobanteRepository\n"


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def test_depends_on_is_loaded_per_module(tmp_path: Path) -> None:
    contract, _ = build(
        tmp_path,
        {},
        extra=DEPENDS + '\n[modules.cartera]\n\n[modules.reporte]\ndepends_on = ["a", "b"]\n',
    )
    assert contract.module_deps == {
        "asesor": ["comprobante"],
        # A block with no depends_on declares none, the same as no block.
        "cartera": [],
        "reporte": ["a", "b"],
    }


def test_a_module_with_no_block_depends_on_nothing(tmp_path: Path) -> None:
    contract, _ = build(tmp_path, {})
    assert contract.module_deps == {}
    assert contract.module_deps.get("asesor", []) == []


def test_show_json_and_rendering_carry_the_graph(tmp_path: Path) -> None:
    contract, _ = build(tmp_path, {}, extra=DEPENDS)
    described = contract.describe()
    assert described["modules"] == {"asesor": {"depends_on": ["comprobante"]}}
    assert described["rules"]["placement"]["facade"] == "modules/<name>/public.py"
    # Survives the trip to JSON: this is what an agent reads.
    assert json.loads(json.dumps(described))["modules"]["asesor"]["depends_on"] == ["comprobante"]

    markdown = render(contract)
    assert "## Modules" in markdown
    assert "| `asesor` | `modules/comprobante/public.py` |" in markdown


def test_append_module_block_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "contracts.toml"
    path.write_text(BASE, encoding="utf-8")
    assert append_module_block(path, "asesor") is True
    assert append_module_block(path, "asesor") is False
    text = path.read_text(encoding="utf-8")
    assert text.count("[modules.asesor]") == 1
    assert Contract.load(path).module_deps == {"asesor": []}
    # A block someone already filled in is theirs.
    path.write_text(BASE + DEPENDS, encoding="utf-8")
    assert append_module_block(path, "asesor") is False
    assert Contract.load(path).module_deps == {"asesor": ["comprobante"]}


# ---------------------------------------------------------------------------
# cross-module
# ---------------------------------------------------------------------------


def test_reaching_past_the_facade_is_reported_with_the_facade_to_use(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/asesor/service.py": USES_SERVICE},
        extra=DEPENDS,
    )
    [violation] = found(contract, root, "cross-module")
    assert violation.path == "modules/asesor/service.py"
    assert "import modules.comprobante.public instead" in violation.message
    assert "modules/comprobante/public.py" in violation.why
    # Behaviour does not go to shared/: that advice is for vocabulary only.
    assert "shared/" not in violation.why


def test_a_missing_facade_is_named_as_the_file_to_create(tmp_path: Path) -> None:
    files = {k: v for k, v in COMPROBANTE.items() if not k.endswith("public.py")}
    files["modules/asesor/service.py"] = "from modules.comprobante.models import Comprobante\n"
    contract, root = build(tmp_path, files, base=False)
    [violation] = found(contract, root, "cross-module")
    assert "does not exist yet: create it" in violation.why
    assert "returns DTOs" in violation.why
    assert 'add "comprobante" to depends_on under [modules.asesor]' in violation.why


def test_an_enum_still_points_at_shared(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path, {"modules/asesor/service.py": "from modules.comprobante.enums import Estado\n"}
    )
    [violation] = found(contract, root, "cross-module")
    assert "shared/enums.py" in violation.why


def test_a_relative_import_across_modules_is_resolved(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/asesor/service.py": USES_REPOSITORY},
    )
    assert [v.rule for v in found(contract, root)] == ["cross-module"]


def test_importing_public_alongside_something_private_is_still_a_reach(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/asesor/service.py": "from modules.comprobante import public, service\n"},
        extra=DEPENDS,
    )
    assert [v.rule for v in found(contract, root)] == ["cross-module"]


def test_a_waiver_clears_a_cross_module_import(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/asesor/service.py": (
                "from modules.comprobante.service import ComprobanteService"
                "  # contracts: allow migrating to the facade, JF-88\n"
            )
        },
    )
    assert found(contract, root) == []


# ---------------------------------------------------------------------------
# undeclared-dependency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        USES_FACADE,
        "from modules.comprobante import public\n",
        "import modules.comprobante.public as comprobantes\n",
    ],
)
def test_a_declared_facade_import_is_clean(tmp_path: Path, statement: str) -> None:
    contract, root = build(tmp_path, {"modules/asesor/service.py": statement}, extra=DEPENDS)
    assert found(contract, root) == []
    # And not a layer finding either: which layer calls another module's
    # facade is not a question the layer rules answer.
    assert check(contract, root) == []


def test_an_undeclared_facade_import_is_reported(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"modules/asesor/service.py": USES_FACADE})
    [violation] = found(contract, root)
    assert violation.rule == "undeclared-dependency"
    assert violation.line == 1
    assert "'asesor' calls modules.comprobante.public" in violation.message
    assert 'add "comprobante" to depends_on under [modules.asesor]' in violation.why


def test_declaring_a_different_module_does_not_count(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/asesor/service.py": USES_FACADE, "modules/cartera/__init__.py": ""},
        extra='\n[modules.asesor]\ndepends_on = ["cartera"]\n',
    )
    assert [v.rule for v in found(contract, root)] == ["undeclared-dependency"]


def test_a_waiver_clears_an_undeclared_dependency(tmp_path: Path) -> None:
    body = USES_FACADE.rstrip("\n") + "  # contracts: allow spike, removed in JF-90\n"
    contract, root = build(tmp_path, {"modules/asesor/service.py": body})
    assert found(contract, root) == []


def test_a_module_importing_its_own_facade_is_not_a_dependency(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/comprobante/worker.py": USES_FACADE},
    )
    assert found(contract, root) == []


# ---------------------------------------------------------------------------
# module-cycle
# ---------------------------------------------------------------------------


def test_a_declared_cycle_is_reported_once_with_its_path(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {},
        extra=DEPENDS + '\n[modules.comprobante]\ndepends_on = ["asesor"]\n',
    )
    cycles = found(contract, root, "module-cycle")
    assert len(cycles) == 1
    assert "asesor -> comprobante -> asesor" in cycles[0].message
    # Made only of declarations, so there is no import line to point at.
    assert cycles[0].path == "contracts.toml"
    assert "event" in cycles[0].why


def test_a_cycle_closed_by_an_import_points_at_the_import(tmp_path: Path) -> None:
    # comprobante -> asesor is declared; asesor -> comprobante is only imported.
    contract, root = build(
        tmp_path,
        {"modules/asesor/service.py": USES_FACADE},
        extra='\n[modules.comprobante]\ndepends_on = ["asesor"]\n',
    )
    rules = sorted(v.rule for v in found(contract, root))
    assert rules == ["module-cycle", "undeclared-dependency"]
    [cycle] = found(contract, root, "module-cycle")
    assert (cycle.path, cycle.line) == ("modules/asesor/service.py", 1)


def test_a_longer_cycle_is_named_in_full(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {},
        extra=(
            '\n[modules.a]\ndepends_on = ["b"]\n'
            '\n[modules.b]\ndepends_on = ["c"]\n'
            '\n[modules.c]\ndepends_on = ["a"]\n'
        ),
    )
    [cycle] = found(contract, root, "module-cycle")
    assert cycle.message.endswith("a -> b -> c -> a")


def test_an_acyclic_graph_has_no_cycle(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {},
        extra=(
            '\n[modules.a]\ndepends_on = ["b", "c"]\n'
            '\n[modules.b]\ndepends_on = ["c"]\n'
            "\n[modules.c]\ndepends_on = []\n"
        ),
    )
    assert found(contract, root, "module-cycle") == []


# ---------------------------------------------------------------------------
# public-leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("package", ["fastapi", "starlette.requests"])
def test_a_facade_that_knows_http_is_reported(tmp_path: Path, package: str) -> None:
    contract, root = build(
        tmp_path,
        {"modules/comprobante/public.py": f"from {package} import Request\n"},
    )
    [violation] = found(contract, root)
    assert violation.rule == "public-leak"
    assert "no request" in violation.why


def test_a_facade_handing_out_an_entity_is_reported(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/public.py": (
                "from .models import Comprobante\nfrom .repository import ComprobanteRepository\n"
            )
        },
    )
    [violation] = found(contract, root)
    assert violation.rule == "public-leak"
    assert violation.line == 1
    assert "ORM entity Comprobante" in violation.message
    assert "DTO" in violation.why


def test_an_entity_declared_inside_the_facade_is_reported(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/comprobante/public.py": ('class Resumen:\n    __tablename__ = "resumenes"\n')},
    )
    assert [v.rule for v in found(contract, root)] == ["public-leak"]


def test_the_entity_rule_only_applies_to_the_facade(tmp_path: Path) -> None:
    # The service importing its own entity is ordinary code, not a leak.
    contract, root = build(
        tmp_path, {"modules/comprobante/service.py": "from .models import Comprobante\n"}
    )
    assert found(contract, root) == []


def test_a_waiver_clears_a_leak(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/public.py": (
                "from .models import Comprobante  # contracts: allow read-only admin export\n"
            )
        },
    )
    assert found(contract, root) == []


# ---------------------------------------------------------------------------
# cross-module-sql
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        'SQL = "SELECT categoria, sum(total) FROM comprobantes WHERE tenant_id = :t"\n',
        'SQL = "select * from COMPROBANTES"\n',
        'SQL = "SELECT * FROM public.\\"comprobantes\\" c"\n',
        'SQL = "UPDATE comprobantes SET estado = :e"\n',
        'SQL = "INSERT INTO comprobantes (id) VALUES (1)"\n',
        'SQL = "SELECT 1 FROM carteras k JOIN comprobantes c ON c.cartera_id = k.id"\n',
        # Implicit concatenation is one constant in the AST.
        'SQL = ("SELECT categoria "\n       "FROM comprobantes c")\n',
        # The table sits in the constant part of an f-string.
        'def q(where):\n    return f"SELECT c.categoria FROM comprobantes c WHERE {where}"\n',
        'def q(cols):\n    return f"SELECT {cols} FROM comprobantes"\n',
    ],
)
def test_sql_against_another_modules_table_is_reported(tmp_path: Path, body: str) -> None:
    contract, root = build(tmp_path, {"modules/asesor/repository.py": body})
    [violation] = found(contract, root)
    assert violation.rule == "cross-module-sql"
    assert "'comprobantes' (module 'comprobante')" in violation.message
    assert "modules/comprobante/public.py" in violation.why


def test_the_real_case_is_caught(tmp_path: Path) -> None:
    """The shape that prompted the rule, trimmed from a production module."""
    body = '''
from sqlalchemy import text


class FinanzasRepository:
    """Lecturas de las tablas de carteras y comprobantes: SQL de solo lectura."""

    def _donde(self, cartera_id):
        return "c.tenant_id = :t"

    async def por_categoria(self, cartera_id, desde, hasta):
        return await self._filas(
            f"SELECT c.categoria, sum(c.total_centavos) AS total FROM comprobantes c "
            f"WHERE {self._donde(cartera_id)} AND c.fecha >= :d GROUP BY 1 ORDER BY 2 DESC",
        )
'''
    contract, root = build(tmp_path, {"modules/asesor/repositories/asesor_repository.py": body})
    [violation] = found(contract, root)
    assert violation.rule == "cross-module-sql"
    # The docstring mentions the tables too, and is not a query.
    assert violation.line == 13


def test_sql_against_the_modules_own_table_is_fine(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/asesor/repository.py": 'SQL = "SELECT * FROM conversaciones WHERE id = :id"\n'},
    )
    assert found(contract, root) == []


def test_a_table_no_module_owns_is_not_reported(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path, {"modules/asesor/repository.py": 'SQL = "SELECT * FROM outbox_events"\n'}
    )
    assert found(contract, root) == []


def test_a_docstring_or_prose_that_is_not_a_table_is_not_reported(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/asesor/repository.py": (
                '"""Reads from comprobantes, through the facade."""\n\n'
                'MESSAGE = "update the page from the cache"\n'
            )
        },
    )
    assert found(contract, root) == []


def test_a_whole_word_is_required(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path, {"modules/asesor/repository.py": 'SQL = "SELECT * FROM comprobantes_archivo"\n'}
    )
    assert found(contract, root) == []


def test_a_waiver_on_any_line_of_the_string_clears_it(tmp_path: Path) -> None:
    body = (
        'SQL = """\n'
        "    SELECT categoria FROM comprobantes  -- contracts: allow nightly report, JF-91\n"
        '"""\n'
    )
    contract, root = build(tmp_path, {"modules/asesor/repository.py": body})
    assert found(contract, root) == []


# ---------------------------------------------------------------------------
# The switch, and the rules that did not change
# ---------------------------------------------------------------------------


def test_one_switch_turns_every_rule_off(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/asesor/service.py": (
                "from modules.comprobante.service import ComprobanteService\n" + USES_FACADE
            ),
            "modules/asesor/repository.py": 'SQL = "SELECT * FROM comprobantes"\n',
            "modules/comprobante/public.py": "from fastapi import Request\n",
            "shared/enums.py": "from modules.comprobante.enums import Estado\n",
        },
        extra='\n[modules.comprobante]\ndepends_on = ["asesor"]\n',
    )
    assert {v.rule for v in found(contract, root)} == {
        "cross-module",
        "undeclared-dependency",
        "module-cycle",
        "public-leak",
        "cross-module-sql",
        "shared-direction",
    }
    contract.enforce_placement = False
    assert found(contract, root) == []


def test_shared_importing_a_facade_is_still_the_wrong_direction(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"shared/helpers.py": USES_FACADE})
    assert [v.rule for v in found(contract, root)] == ["shared-direction"]


# ---------------------------------------------------------------------------
# explain and diff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    ["cross-module", "undeclared-dependency", "module-cycle", "public-leak", "cross-module-sql"],
)
def test_every_rule_can_be_explained(tmp_path: Path, rule: str) -> None:
    contract, root = build(tmp_path, {}, extra=DEPENDS)
    assert rule in RULES
    answer = explain(contract, root, rule=rule)
    assert answer.verdict == "informational"
    assert answer.instead


def test_a_declared_module_pair_is_allowed_through_the_facade_only(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {}, extra=DEPENDS)
    answer = explain(contract, root, subject=["asesor", "comprobante"])
    assert answer.verdict == "allowed"
    assert "modules.comprobante.public" in answer.summary
    assert any(d.text.startswith("depends_on") for d in answer.declarations)


def test_an_undeclared_module_pair_is_forbidden_with_three_ways_out(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {})
    answer = explain(contract, root, subject=["asesor", "comprobante"])
    assert answer.verdict == "forbidden"
    joined = " ".join(answer.instead)
    assert "modules/comprobante/public.py" in joined
    assert "outbox" in joined
    assert "shared/" in joined


def test_diff_separates_undeclared_from_private_and_unused(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/asesor/service.py": USES_FACADE,
            "modules/cartera/service.py": USES_SERVICE,
        },
        extra='\n[modules.comprobante]\ndepends_on = ["cartera"]\n',
    )
    result = diff(contract, root)
    added = {(d.source, d.target): d.rule for d in result.added if d.kind == "module"}
    assert added == {
        ("asesor", "comprobante"): "undeclared-dependency",
        ("cartera", "comprobante"): "cross-module",
    }
    removed = [(d.source, d.target) for d in result.removed if d.kind == "module"]
    assert removed == [("comprobante", "cartera")]


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------

#: The file in each layout that holds business logic and a session, where a
#: call to another module's facade naturally goes.
CALLER = {
    "layered": "modules/asesor/service.py",
    "modular": "modules/asesor/services/asesor_service.py",
    "screaming": "modules/asesor/storage.py",
    "hexagonal": "modules/asesor/infrastructure/repository.py",
}


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_every_layout_generates_a_facade_returning_a_dto(tmp_path: Path, layout: str) -> None:
    Scaffolder().render_trees(
        module_trees(layout, "api", tmp_path / "modules", tmp_path),
        module_context("comprobante", layout=layout),
    )
    facade = (tmp_path / "modules" / "comprobante" / "public.py").read_text(encoding="utf-8")
    assert "class ComprobanteSummary" in facade
    assert "async def get_comprobante(" in facade
    assert "session: AsyncSession, *, tenant_id: str | None, comprobante_id: int" in facade
    assert "-> ComprobanteSummary | None" in facade
    compile(facade, "public.py", "exec")


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "modules").mkdir(parents=True)
    (root / "jfast.toml").write_text(
        '[app]\nname = "shop"\nversion = "0.1.0"\nenv = "local"\nport = 8000\n\n'
        "[plugins]\nenabled = []\ndisabled = []\n",
        encoding="utf-8",
    )
    # What `jfast new service` ships, and the one directory the shared layer
    # governs: without it the contract reports that layer as covering nothing.
    (root / "shared").mkdir()
    (root / "shared" / "__init__.py").write_text("", encoding="utf-8")
    return root


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_a_generated_project_passes_and_enforces_the_facade(
    tmp_path: Path, layout: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    for name in ("comprobante", "asesor"):
        result = runner.invoke(app, ["new", "module", name, "--layout", layout])
        assert result.exit_code == 0, result.output

    contract_file = root / "contracts.toml"
    text = contract_file.read_text(encoding="utf-8")
    assert "[modules.comprobante]\ndepends_on = []" in text
    assert "[modules.asesor]\ndepends_on = []" in text
    # The generator must not violate its own contract.
    contract = Contract.load(contract_file)
    assert check(contract, root) == []
    assert contract.layer_for("modules/asesor/public.py") is contract.layers["public"]

    caller = root / CALLER[layout]
    caller.write_text(
        "from modules.comprobante.public import get_comprobante\n"
        + caller.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    undeclared = check(Contract.load(contract_file), root)
    assert [v.rule for v in undeclared] == ["undeclared-dependency"], undeclared

    contract_file.write_text(
        text.replace(
            "[modules.asesor]\ndepends_on = []", '[modules.asesor]\ndepends_on = ["comprobante"]'
        ),
        encoding="utf-8",
    )
    declared = Contract.load(contract_file)
    assert check(declared, root) == []
    # `diff` agrees: a declared facade call is neither a layer edge nor a
    # module edge the contract does not permit.
    assert diff(declared, root).added == ()

    # And a reach past the facade is still refused, in every layout.
    caller.write_text(
        "from modules.comprobante.public import get_comprobante\n"
        "import modules.comprobante.tests\n" + caller.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert "cross-module" in {v.rule for v in check(Contract.load(contract_file), root)}


# ---------------------------------------------------------------------------
# unknown-dependency
# ---------------------------------------------------------------------------


def test_a_misspelt_dependency_is_reported(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/asesor/service.py": USES_FACADE},
        extra='\n[modules.asesor]\ndepends_on = ["comprobantes"]\n',
    )
    [unknown] = found(contract, root, "unknown-dependency")
    assert "'comprobantes'" in unknown.message and "depends_on" in unknown.message
    # ...and the real one is still undeclared, which is what the typo caused.
    assert found(contract, root, "undeclared-dependency")


def test_a_block_for_a_module_that_is_gone_is_reported(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {}, extra="\n[modules.facturas]\ndepends_on = []\n")
    [unknown] = found(contract, root, "unknown-dependency")
    assert "[modules.facturas]" in unknown.message


def test_declared_modules_that_exist_are_fine(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"modules/asesor/service.py": USES_FACADE}, extra=DEPENDS)
    assert found(contract, root, "unknown-dependency") == []
