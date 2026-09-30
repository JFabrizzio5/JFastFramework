"""`jfast new module --fields/--unique/--bare`: the grammar, and what it renders.

The grammar tests pin every type and every error message's fix. The rendering
tests generate a real service and module per layout and form and run the
module's own gates on it -- ruff, ruff format and pytest; mypy is the slow one
and runs in scripts/smoke_generated_quality.sh -- with the formatter pass
switched off, so what is checked is the template itself and not ruff's repair
of it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli import scaffold
from jfastframework.cli.fields import (
    EXAMPLE_FIELDS,
    FieldSpecError,
    module_fields,
    parse_field,
    parse_fields,
    parse_unique,
    split_fields,
)
from jfastframework.cli.main import app
from jfastframework.cli.scaffold import MODULE_LAYOUTS

runner = CliRunner()

CUADRA = "cartera_id:int, mes:str(7), gasto:money, leida:bool=false, nota:text?"


# -- the grammar ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "py_type", "column"),
    [
        ("a:int", "int", "mapped_column()"),
        ("a:bigint", "int", "mapped_column(BigInteger)"),
        ("a:str(7)", "str", "mapped_column(String(7))"),
        ("a:str", "str", "mapped_column(String(255))"),
        ("a:text", "str", "mapped_column(Text)"),
        ("a:bool", "bool", "mapped_column()"),
        ("a:float", "float", "mapped_column()"),
        ("a:decimal(10,2)", "Decimal", "mapped_column(Numeric(10, 2))"),
        ("a:money", "int", "mapped_column(BigInteger)"),
        ("a:date", "date", "mapped_column()"),
        ("a:datetime", "datetime", "mapped_column(UTCDateTime())"),
        ("a:json", "dict[str, Any]", "mapped_column(JSON_COLUMN)"),
    ],
)
def test_every_type_maps_to_a_python_type_and_a_column(
    text: str, py_type: str, column: str
) -> None:
    field = parse_field(text)
    assert field.py_type == py_type
    assert field.column == column


def test_a_question_mark_is_nullable_and_defaults_to_none() -> None:
    field = parse_field("nota:text?")
    assert field.nullable
    assert field.annotation == "str | None"
    assert field.create_declaration() == "nota: str | None = None"


def test_defaults_are_written_in_the_types_own_syntax() -> None:
    assert parse_field("leida:bool=false").default == "False"
    assert parse_field("n:int=3").default == "3"
    assert parse_field("gasto:money=1050").default == "1050"
    assert parse_field("p:decimal(10,2)=3.5").default == 'Decimal("3.50")'
    assert parse_field("s:str(20)=en curso").default == '"en curso"'
    assert parse_field('s:str(20)="two words"').default == '"two words"'


def test_spaces_around_the_punctuation_are_ignored() -> None:
    assert parse_field(" monto : decimal( 10 , 2 ) ? ").precision == 10


def test_commas_inside_parentheses_do_not_split_fields() -> None:
    assert split_fields("a:decimal(10,2), b:int") == ["a:decimal(10,2)", "b:int"]


def test_a_required_string_may_not_be_empty_and_has_its_column_length() -> None:
    assert parse_field("mes:str(7)").constraints == ["min_length=1", "max_length=7"]
    assert parse_field("mes:str(7)?").constraints == ["max_length=7"]


def test_a_datetime_on_the_wire_must_carry_its_zone() -> None:
    assert "AwareDatetime" in parse_field("cuando:datetime").create_declaration()


@pytest.mark.parametrize(
    ("text", "fix"),
    [
        ("mes", "write it as name:type"),
        ("mes:varchar", "Choose from: int, bigint"),
        ("id:int", "drop it from --fields"),
        ("tenant_id:str", "drop it from --fields"),
        ("json:int", "rename it"),
        ("model_name:str", "rename it"),
        ("Mes:str", "snake_case"),
        ("class:str", "snake_case"),
        ("mes:str(0)", "Use text for a string with no limit"),
        ("monto:decimal", r"decimal\(10,2\)"),
        ("monto:decimal(2,5)", r"decimal\(10,2\)"),
        ("n:int(4)", "takes no arguments"),
        ("n:int=many", "is not an integer"),
        ("b:bool=yes", "true or false"),
        ("mes:str(3)=abcd", "at most 3 characters"),
        ("dia:date=today", "set it in the service"),
    ],
)
def test_every_mistake_names_its_fix(text: str, fix: str) -> None:
    with pytest.raises(FieldSpecError, match=fix):
        parse_field(text)


def test_a_field_declared_twice_is_refused() -> None:
    with pytest.raises(FieldSpecError, match="declared twice"):
        parse_fields("a:int, a:str")


def test_unique_names_declared_fields_only() -> None:
    fields = parse_fields(CUADRA)
    with pytest.raises(FieldSpecError, match="Declared: cartera_id, mes"):
        parse_unique(["cartera_id,anio"], fields, "presupuestos")
    with pytest.raises(FieldSpecError, match="no equality for json"):
        parse_unique(["doc"], parse_fields("doc:json"), "t")


def test_the_three_forms_are_exclusive_where_they_contradict() -> None:
    with pytest.raises(FieldSpecError, match="contradict"):
        module_fields("a:int", (), bare=True, table="t")
    with pytest.raises(FieldSpecError, match="--unique needs --fields"):
        module_fields(None, ["name"], bare=False, table="t")
    assert module_fields(None, (), bare=True, table="t").fields == ()
    example = module_fields(None, (), bare=False, table="t")
    assert example.example
    assert [f.name for f in example.fields] == [
        f.split(":")[0].strip() for f in EXAMPLE_FIELDS.split(",")
    ]


def test_unique_constraints_get_distinct_names_that_fit_postgres() -> None:
    fields = parse_fields("a:int, b:int")
    one, two = parse_unique(["a", "a,b"], fields, "t")
    assert one.constraint_name != two.constraint_name
    long_name = "x" * 40
    (long,) = parse_unique([long_name], parse_fields(f"{long_name}:int"), "y" * 40)
    assert len(long.constraint_name) <= 63


def test_a_unique_key_named_like_the_loop_variable_does_not_shadow_it() -> None:
    (unique,) = parse_unique(["stored"], parse_fields("stored:int"), "t")
    assert unique.loop_variable != "stored"


# -- what it renders ---------------------------------------------------------


needs_ruff = pytest.mark.skipif(
    shutil.which("ruff") is None and not (Path(sys.executable).parent / "ruff").exists(),
    reason="ruff is not installed",
)


def _ruff() -> list[str]:
    local = Path(sys.executable).parent / "ruff"
    return [str(local)] if local.exists() else ["ruff"]


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A generated service, and no formatter: the templates must stand on their own."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scaffold, "format_generated", lambda paths, root: True)
    monkeypatch.setattr("jfastframework.cli.commands.new.format_generated", lambda p, r: True)
    result = runner.invoke(app, ["new", "service", "shop", "--with", "database"])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path / "shop")
    return tmp_path / "shop"


def _gates(root: Path) -> None:
    for arguments in (["check", "."], ["format", "--check", "."]):
        done = subprocess.run(
            [*_ruff(), *arguments], cwd=root, capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stdout + done.stderr
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "modules"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "warning" not in done.stdout.lower(), done.stdout


FORMS = {
    "example": [],
    "fields": ["--fields", CUADRA, "--unique", "cartera_id,mes"],
    "bare": ["--bare"],
    "every-type": [
        "--fields",
        "a:int, b:bigint?, c:str(20)=abierto, d:text?, e:bool=true, f:float?, "
        "g:decimal(12,2)=0, h:money, i:date, j:datetime?, k:json?",
        "--unique",
        "a",
        "--unique",
        "c,i",
    ],
}


@needs_ruff
@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
@pytest.mark.parametrize("form", list(FORMS))
def test_a_generated_module_passes_ruff_format_and_its_own_tests(
    service: Path, layout: str, form: str
) -> None:
    result = runner.invoke(app, ["new", "module", "invoice", "--layout", layout, *FORMS[form]])
    assert result.exit_code == 0, result.output
    _gates(service)


def test_the_declared_fields_reach_every_layer(service: Path) -> None:
    result = runner.invoke(
        app,
        [
            *["new", "module", "presupuesto", "--layout", "modular"],
            *["--fields", CUADRA, "--unique", "cartera_id,mes"],
        ],
    )
    assert result.exit_code == 0, result.output
    module = service / "modules" / "presupuesto"
    entity = (module / "models" / "presupuesto_entity.py").read_text(encoding="utf-8")
    assert "mes: Mapped[str] = mapped_column(String(7))" in entity
    assert "TenantMixin" in entity, "tenant_id stays on every entity, single-tenant or not"
    assert '"tenant_id",\n            "cartera_id",\n            "mes",' in entity
    wire = (module / "models" / "presupuesto_models.py").read_text(encoding="utf-8")
    assert "mes: str = Field(min_length=1, max_length=7)" in wire
    assert "leida: bool = False" in wire
    public = (module / "public.py").read_text(encoding="utf-8")
    assert "gasto: int" in public and "name" not in public
    repository = (module / "repositories" / "presupuesto_repository.py").read_text(encoding="utf-8")
    assert "async def by_cartera_id_and_mes(self, cartera_id: int, mes: str)" in repository
    rules = (module / "validations" / "presupuesto_validation.py").read_text(encoding="utf-8")
    assert "ensure_cartera_id_and_mes_is_available" in rules


def test_bare_has_the_structure_and_no_example(service: Path) -> None:
    result = runner.invoke(app, ["new", "module", "alerta", "--layout", "modular", "--bare"])
    assert result.exit_code == 0, result.output
    module = service / "modules" / "alerta"
    assert (module / "tasks.py").is_file()
    everything = "".join(p.read_text(encoding="utf-8") for p in module.rglob("*.py"))
    for example in ("is_active", "description", "AlertaStage", "ensure_name_is_available"):
        assert example not in everything


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BOX = re.compile(r"[\u2500-\u257f]")


def _plain(rendered: str) -> str:
    """The words of a CLI message, whatever the terminal did to them.

    Under CI (GITHUB_ACTIONS), Rich colours each option in an error panel and
    wraps it inside a box, so `--ui api` arrives split by escape codes and
    borders. Locally it is plain, which is how this passed here and failed there.
    """
    return re.sub(r"\s+", " ", _BOX.sub(" ", _ANSI.sub("", rendered)))


def test_htmx_pages_are_refused_with_declared_fields(service: Path) -> None:
    result = runner.invoke(
        app, ["new", "module", "pedido", "--ui", "htmx", "--fields", "total:money"]
    )
    assert result.exit_code != 0
    assert "--ui api" in _plain(result.output)


def test_a_bad_field_stops_before_anything_is_written(service: Path) -> None:
    result = runner.invoke(app, ["new", "module", "pedido", "--fields", "total:currency"])
    assert result.exit_code != 0
    assert "Choose from" in _plain(result.output)
    assert not (service / "modules" / "pedido").exists()


@pytest.mark.parametrize(
    ("plugins", "expected", "absent"),
    [
        ("database", 'getattr(request.state, "tenant_id", None)', "require_auth"),
        ("database,auth", "dependencies=[Depends(require_auth)]", "current_tenant"),
        ("database,auth,tenancy", "tenant_id: str = Depends(current_tenant)", "request.state"),
    ],
)
@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_routes_guard_themselves_as_the_service_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    plugins: str,
    expected: str,
    absent: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["new", "service", "shop", "--with", plugins]).exit_code == 0
    monkeypatch.chdir(tmp_path / "shop")
    result = runner.invoke(app, ["new", "module", "pedido", "--layout", layout])
    assert result.exit_code == 0, result.output
    http = {
        "layered": "router.py",
        "modular": "api/routes.py",
        "screaming": "http.py",
        "hexagonal": "adapters/http.py",
    }[layout]
    source = (tmp_path / "shop" / "modules" / "pedido" / http).read_text(encoding="utf-8")
    assert expected in source
    assert absent not in source


def test_new_modules_are_mounted_in_sorted_order(service: Path) -> None:
    for name in ("zeta", "alfa"):
        assert runner.invoke(app, ["new", "module", name, "--bare"]).exit_code == 0
    main = (service / "main.py").read_text(encoding="utf-8")
    assert main.index("modules.alfa") < main.index("modules.zeta")
    assert "import router as zeta_router\n\n# [jfast:imports]" in main


def test_an_enum_added_to_an_old_str_enum_file_imports_what_it_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generators now write `StrEnum`; a file from before only imports `Enum`."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shared").mkdir()
    old = (
        '"""Enums."""\n\nfrom __future__ import annotations\n\nfrom enum import Enum\n\n\n'
        'class Legacy(str, Enum):\n    A = "a"\n'
    )
    (tmp_path / "shared" / "enums.py").write_text(old, encoding="utf-8")
    result = runner.invoke(app, ["new", "enum", "Status", "--shared", "--values", "open,done"])
    assert result.exit_code == 0, result.output
    source = (tmp_path / "shared" / "enums.py").read_text(encoding="utf-8")
    assert "from enum import Enum, StrEnum" in source
    assert "class Status(StrEnum):" in source
    namespace: dict[str, object] = {}
    exec(compile(source, "enums.py", "exec"), namespace)  # the file must import


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_a_unique_key_with_an_optional_field_ignores_rows_without_a_value(
    service: Path, layout: str
) -> None:
    # Found building a receipts SaaS from scratch: `--unique uuid_cfdi` on an
    # optional field made the second receipt without a UUID a 409, because
    # NULLS NOT DISTINCT treated every missing value as the same one and the
    # rule looked the None up. It now means "unique once it has a value".
    result = runner.invoke(
        app,
        [
            "new",
            "module",
            "gasto",
            "--layout",
            layout,
            "--fields",
            "folio:str(36)?,total:money",
            "--unique",
            "folio",
        ],
    )
    assert result.exit_code == 0, result.output
    module = service / "modules" / "gasto"
    everything = "".join(p.read_text(encoding="utf-8") for p in module.rglob("*.py"))
    assert 'postgresql_where=text("folio IS NOT NULL")' in everything
    assert "UniqueConstraint(" not in everything
    assert "if folio is None:" in everything
    if layout == "modular":
        assert "test_rows_without_a_key_value_never_collide" in everything


def test_a_required_unique_key_keeps_its_constraint(service: Path) -> None:
    result = runner.invoke(
        app,
        ["new", "module", "gasto", "--fields", "folio:str(36),total:money", "--unique", "folio"],
    )
    assert result.exit_code == 0, result.output
    everything = "".join(
        p.read_text(encoding="utf-8") for p in (service / "modules" / "gasto").rglob("*.py")
    )
    assert "UniqueConstraint(" in everything and "postgresql_where" not in everything
    assert "is None:" not in everything.split("ensure_folio_is_available")[1][:400]
