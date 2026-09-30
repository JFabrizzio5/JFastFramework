"""Scaffolding: naming, tree composition, and the two Jinja environments."""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.cli.scaffold import (
    CONTRACT_TEMPLATE_FOR,
    MODULE_LAYOUTS,
    Scaffolder,
    Tree,
    module_context,
    module_trees,
    pluralize,
    service_context,
    service_trees,
    to_pascal,
    to_snake,
)
from jfastframework.contracts import Contract, check

#: The file that answers HTTP in each layout. That layer is the one every
#: contract forbids `sqlalchemy` on; nothing else about the four is the same,
#: which is why the same violation is planted in all of them.
HTTP_FILE = {
    "layered": "modules/widget/router.py",
    "modular": "modules/widget/api/routes.py",
    "screaming": "modules/widget/http.py",
    "hexagonal": "modules/widget/adapters/http.py",
}


def generated_service(root: Path, layout: str) -> Path:
    """`jfast new service`, then `jfast new module --layout X`.

    Deliberately not `jfast contracts init --layout X`. That path always chose
    the right template, and testing only it is how a scaffold that never
    consulted the layout shipped a layered contract into every service.
    """
    root.mkdir(parents=True, exist_ok=True)
    scaffolder = Scaffolder()
    scaffolder.render_trees(
        service_trees("api", None, root),
        service_context("shop", kind="api", plugins=["database"]),
    )
    scaffolder.render_trees(
        module_trees(layout, "api", root / "modules", root),
        module_context("widget", layout=layout),
    )
    return root


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("order", "orders"),
        ("invoice", "invoices"),
        ("category", "categories"),
        ("box", "boxes"),
        ("address", "addresses"),
        ("day", "days"),
        ("payment_method", "payment_methods"),
    ],
)
def test_pluralize(word: str, expected: str) -> None:
    assert pluralize(word) == expected


def test_names_are_normalised() -> None:
    assert to_snake("BillingAccount") == "billing_account"
    assert to_snake("billing account") == "billing_account"
    assert to_pascal("billing_account") == "BillingAccount"


def test_table_defaults_to_the_plural_and_dodges_reserved_words() -> None:
    # "order" is a SQL reserved word; "orders" is not.
    assert module_context("order")["table"] == "orders"


def test_table_can_be_overridden() -> None:
    assert module_context("order", table="sales_order")["table"] == "sales_order"


def test_api_ui_renders_one_tree_and_htmx_adds_the_overlay(tmp_path: Path) -> None:
    target, root = tmp_path / "modules", tmp_path
    (root / "contracts.toml").write_text('[project]\nname = "shop"\n', encoding="utf-8")

    assert module_trees("layered", "api", target, root) == [Tree("module_layered", target)]
    assert module_trees("screaming", "htmx", target, root) == [
        Tree("module_screaming", target),
        Tree("ui_htmx", root),
    ]


def test_unknown_layout_and_ui_are_rejected() -> None:
    # "onion" rather than "hexagonal": the latter is a real layout now, and a
    # test whose invalid example quietly becomes valid stops testing anything.
    with pytest.raises(ValueError, match="Unknown layout"):
        module_trees("onion", "api", Path("m"), Path("."))
    with pytest.raises(ValueError, match="Unknown ui"):
        module_trees("layered", "vue", Path("m"), Path("."))


def test_every_layout_renders_and_exports_a_router(tmp_path: Path) -> None:
    """`router` is the name the CLI splices into main.py.

    Without it a generated module is written, registered, and then crashes the
    service on import -- which looks like a framework bug rather than a missing
    export.
    """
    scaffolder = Scaffolder()
    for layout in MODULE_LAYOUTS:
        target = tmp_path / layout
        context = module_context("order", layout=layout)
        scaffolder.render_tree(f"module_{layout}", target, context)
        init = (target / "order" / "__init__.py").read_text(encoding="utf-8")
        assert "router" in init, f"{layout} does not export router"


def test_the_original_layouts_still_expose_build_service(tmp_path: Path) -> None:
    """The shared factory is what makes the HTMX overlay layout-agnostic."""
    scaffolder = Scaffolder()
    for layout in ("layered", "screaming"):
        target = tmp_path / layout
        context = module_context("order", layout=layout)
        scaffolder.render_tree(f"module_{layout}", target, context)
        init = (target / "order" / "__init__.py").read_text(encoding="utf-8")
        assert "def build_service(" in init
        assert '"router"' in init


def test_every_layout_has_a_contract_template() -> None:
    """`jfast contracts init --layout X` must not fail for a layout we offer."""
    from jfastframework.cli.scaffold import TEMPLATE_ROOT

    for layout in MODULE_LAYOUTS:
        template = CONTRACT_TEMPLATE_FOR[layout]
        assert (TEMPLATE_ROOT / template / "contracts.toml.j2").is_file(), template


# -- which contract a scaffolded service gets ---------------------------


def test_a_service_defers_its_contract_until_a_layout_is_known(tmp_path: Path) -> None:
    """No module, no layout, no honest contract.

    Writing one anyway is the defect: `jfast new service` had no layout to
    consult and shipped the layered one, so three of four layouts got a
    contract whose globs matched none of their files.
    """
    templates = [tree.template for tree in service_trees("api", None, tmp_path)]
    assert not [name for name in templates if name.startswith("contracts_")]

    # A caller that does know says so, and gets that layout's contract.
    for layout in MODULE_LAYOUTS:
        named = [tree.template for tree in service_trees("api", None, tmp_path, layout=layout)]
        assert CONTRACT_TEMPLATE_FOR[layout] in named


def test_the_first_module_writes_the_contract_for_its_own_layout(tmp_path: Path) -> None:
    for layout in MODULE_LAYOUTS:
        root = tmp_path / layout
        root.mkdir()
        trees = module_trees(layout, "api", root / "modules", root)
        assert trees[-1].template == CONTRACT_TEMPLATE_FOR[layout]
        # The module context has no project name; the contract template needs
        # one, so the tree carries it.
        assert trees[-1].extra is not None
        assert trees[-1].extra["project"] == layout


def test_a_second_module_never_rewrites_an_existing_contract(tmp_path: Path) -> None:
    root = tmp_path / "shop"
    generated_service(root, "layered")
    edited = (root / "contracts.toml").read_text(encoding="utf-8") + '\n# owned by "@platform"\n'
    (root / "contracts.toml").write_text(edited, encoding="utf-8")

    trees = module_trees("hexagonal", "api", root / "modules", root)
    assert [tree.template for tree in trees] == ["module_hexagonal"]

    Scaffolder().render_trees(trees, module_context("order", layout="hexagonal"))
    # A contract on disk is a document someone has had the chance to edit, and
    # the layer paths are the least of what it carries.
    assert (root / "contracts.toml").read_text(encoding="utf-8") == edited


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_the_generated_contract_enforces_the_generated_layout(tmp_path: Path, layout: str) -> None:
    """The defect, stated as a rule, on the path a user takes.

    `import sqlalchemy` in the file that answers HTTP is a violation in every
    layout. Before this, only `layered` reported it: the other three carried a
    contract whose globs matched nothing they had, and reported a clean pass.
    """
    root = generated_service(tmp_path / "shop", layout)
    contract = Contract.load(root / "contracts.toml")
    assert check(contract, root) == [], "the generator must not violate its own contract"

    planted = root / HTTP_FILE[layout]
    body = "import sqlalchemy\n" + planted.read_text(encoding="utf-8")
    planted.write_text(body, encoding="utf-8")

    violations = check(contract, root)
    assert "layer-package" in {v.rule for v in violations}, violations
    assert any("sqlalchemy" in v.message for v in violations)


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_the_generated_contract_lets_scripts_print(tmp_path: Path, layout: str) -> None:
    """`scripts/` is a terminal's entry point: printing there is the interface.

    The async rules already exempted it and `forbid_call print` did not, so a
    project's own e2e script failed its contract twelve times.
    """
    root = generated_service(tmp_path / "shop", layout)
    contract = Contract.load(root / "contracts.toml")
    for relative in ("scripts/e2e.py", "scripts/seed/tenants.py"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text('print("ok")\n', encoding="utf-8")
    assert check(contract, root) == []

    planted = root / HTTP_FILE[layout]
    planted.write_text(planted.read_text(encoding="utf-8") + '\nprint("debug")\n', "utf-8")
    violations = [v for v in check(contract, root) if "print" in v.message]
    assert [v.path for v in violations] == [HTTP_FILE[layout]], violations


def test_html_templates_keep_their_runtime_jinja(tmp_path: Path) -> None:
    scaffolder = Scaffolder()
    context = module_context("product", ui="htmx")
    scaffolder.render_tree("ui_htmx", tmp_path, context)

    row = (tmp_path / "templates" / "product" / "_row.html").read_text(encoding="utf-8")
    # Scaffold-time values were substituted, under the /ui/ prefix the HTML
    # surface uses. Sharing the prefix with the JSON router meant whichever
    # registered first answered, so the page returned JSON and the form POSTed
    # into the API handler.
    assert 'hx-delete="/ui/products/' in row
    # ...and runtime Jinja survived untouched.
    assert "{{ item.id }}" in row
    assert "{{ item.name }}" in row


def test_existing_files_are_skipped_unless_forced(tmp_path: Path) -> None:
    scaffolder = Scaffolder()
    context = module_context("order")
    scaffolder.render_tree("module_layered", tmp_path, context)

    models = tmp_path / "order" / "models.py"
    models.write_text("# edited by hand\n", encoding="utf-8")

    again = scaffolder.render_tree("module_layered", tmp_path, context)
    assert models.read_text(encoding="utf-8") == "# edited by hand\n"
    assert any(not item.created for item in again)

    scaffolder.render_tree("module_layered", tmp_path, context, force=True)
    assert models.read_text(encoding="utf-8") != "# edited by hand\n"


def test_a_stamp_records_the_template_for_future_upgrades(tmp_path: Path) -> None:
    import json

    scaffolder = Scaffolder()
    scaffolder.render_tree("module_layered", tmp_path, module_context("order"))
    stamp = json.loads((tmp_path / ".jfast-template").read_text(encoding="utf-8"))
    assert "module_layered" in stamp["templates"]
    assert stamp["templates"]["module_layered"]["context"]["table"] == "orders"


def test_every_shipped_plugin_is_in_the_menu_a_generated_service_shows():
    """`PLUGIN_CATALOG` and the entry points are two hand-kept lists.

    Three plugins -- ratelimit, channels, websocket -- shipped as entry points
    and appeared in no menu, so the only way to find them was to read
    pyproject.toml. Nothing went red, because nothing compared the two lists.
    Both directions matter: a catalog entry with no entry point offers an
    install that cannot work.
    """
    import tomllib

    from jfastframework.cli.scaffold import PLUGIN_CATALOG

    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    shipped = set(manifest["project"]["entry-points"]["jfastframework.plugins"])
    catalogued = set(PLUGIN_CATALOG)

    assert shipped - catalogued == set(), (
        f"plugins ship but are in no menu: {sorted(shipped - catalogued)}. "
        "Add each to PLUGIN_CATALOG in cli/scaffold.py."
    )
    assert catalogued - shipped == set(), (
        f"the menu offers plugins that do not ship: {sorted(catalogued - shipped)}. "
        "Add each to the jfastframework.plugins entry points, or drop it."
    )
