"""What a new project gets when nobody chooses: a modular module, and a table
name pluralised in the project's own language."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app
from jfastframework.cli.scaffold import DEFAULT_LAYOUT, module_context, pluralize

runner = CliRunner()


@pytest.mark.parametrize(
    ("word", "plural"),
    [
        ("camion", "camiones"),
        ("chofer", "choferes"),
        ("sucursal", "sucursales"),
        ("cliente", "clientes"),
        ("factura", "facturas"),
        ("lapiz", "lapices"),
        ("mes", "meses"),
        ("lunes", "lunes"),
        ("rey", "reyes"),
        ("orden_compra", "ordenes_compra"),
    ],
)
def test_spanish(word: str, plural: str) -> None:
    assert pluralize(word, "es") == plural


@pytest.mark.parametrize(
    ("word", "plural"),
    [
        ("order", "orders"),
        ("category", "categories"),
        ("box", "boxes"),
        ("line_item", "line_items"),
    ],
)
def test_english_is_unchanged(word: str, plural: str) -> None:
    assert pluralize(word) == plural


def test_the_default_layout_is_modular() -> None:
    assert DEFAULT_LAYOUT == "modular"
    assert module_context("order")["layout"] == "modular"


def _service(tmp_path: Path, scaffold: str = "") -> Path:
    root = tmp_path / "shop"
    (root / "modules").mkdir(parents=True)
    (root / "jfast.toml").write_text(
        '[app]\nname = "shop"\nversion = "0.1.0"\nenv = "local"\nport = 8000\n\n'
        "[plugins]\nenabled = []\ndisabled = []\n" + scaffold,
        encoding="utf-8",
    )
    return root


def test_a_module_nobody_chose_a_layout_for_is_modular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _service(tmp_path)
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["new", "module", "order"])
    assert result.exit_code == 0, result.output
    for folder in ("api", "models", "repositories", "services", "validations"):
        assert (root / "modules" / "order" / folder).is_dir(), folder
    assert 'layout = "modular"' in (root / "jfast.toml").read_text(encoding="utf-8")


def test_a_spanish_project_gets_spanish_table_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _service(tmp_path, '\n[scaffold]\nlanguage = "es"\n')
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["new", "module", "camion"])
    assert result.exit_code == 0, result.output
    models = "".join(p.read_text(encoding="utf-8") for p in (root / "modules").rglob("*.py"))
    assert '__tablename__ = "camiones"' in models


def test_the_flag_wins_over_the_project_and_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _service(tmp_path, '\n[scaffold]\nlanguage = "es"\n')
    monkeypatch.chdir(root)
    english = runner.invoke(app, ["new", "module", "truck", "--language", "en"])
    assert english.exit_code == 0, english.output
    models = "".join(p.read_text(encoding="utf-8") for p in (root / "modules").rglob("*.py"))
    assert '__tablename__ = "trucks"' in models

    wrong = runner.invoke(app, ["new", "module", "caja", "--language", "fr"])
    assert wrong.exit_code != 0
