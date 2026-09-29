"""`jfast contracts explain`: from a rule name back to the line that declares it.

`contracts check` reports that a rule was broken. It cannot say why the rule
exists, and an agent handed a violation with no remedy satisfies the checker
instead of fixing the design -- by deleting the import, copying the code, or
turning the rule off.

So what is pinned here is not the wording of the answer but the two things that
make it usable: the `contracts.toml` line that declares the rule, and the
rationale its author wrote above that line. Every assertion about a line number
is computed from the generated file, and every layout is exercised, because the
four layouts declare different layers at different lines -- an answer that is
hardcoded, or generic, passes on one of them and fails on three.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework.cli.explain import register
from jfastframework.cli.scaffold import (
    CONTRACT_TEMPLATE_FOR,
    Scaffolder,
    module_context,
    module_trees,
)
from jfastframework.contracts import Contract, check
from jfastframework.contracts.checker import layer_matches
from jfastframework.contracts.explain import diff, explain

LAYOUTS = ("layered", "modular", "screaming", "hexagonal")

#: A file in each layout's HTTP-ish layer, and the name that layer goes by
#: there. Every layout forbids `sqlalchemy` on it; nothing else about them is
#: the same, which is the point of running the same tests over all four.
HTTP_FILE = {
    "layered": "modules/invoice/router.py",
    "modular": "modules/invoice/api/routes.py",
    "screaming": "modules/invoice/http.py",
    "hexagonal": "modules/invoice/adapters/api.py",
}
HTTP_LAYER = {
    "layered": "http",
    "modular": "http",
    "screaming": "http",
    "hexagonal": "adapters",
}

runner = CliRunner()


def _service(tmp_path: Path, layout: str, files: dict[str, str]) -> tuple[Contract, Path]:
    Scaffolder().render_tree(
        CONTRACT_TEMPLATE_FOR[layout],
        tmp_path,
        {"project": "billing", "layout": layout, "Project": "Billing"},
    )
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return Contract.load(tmp_path / "contracts.toml"), tmp_path


def _mismatched(tmp_path: Path) -> tuple[Contract, Path]:
    """The project the defect produced: hexagonal modules under a layered contract.

    Generated rather than written here. A contract composed in this file to
    match nothing would prove only that a contract composed in this file
    matches nothing; what has to be reproduced is the pair `jfast new` put on
    disk -- modules in one layout, and the contract `jfast new service` wrote
    for another before it learned to wait for the first module.
    """
    scaffolder = Scaffolder()
    scaffolder.render_trees(
        module_trees("hexagonal", "api", tmp_path / "modules", tmp_path),
        module_context("invoice", layout="hexagonal"),
    )
    (tmp_path / "shared").mkdir(exist_ok=True)
    (tmp_path / "shared" / "enums.py").write_text("STATUS = 1\n", encoding="utf-8")

    scaffolder.render_tree(
        CONTRACT_TEMPLATE_FOR["layered"],
        tmp_path,
        {"project": "billing", "layout": "layered", "Project": "Billing"},
        force=True,
    )
    return Contract.load(tmp_path / "contracts.toml"), tmp_path


def _lines(root: Path) -> list[str]:
    return (root / "contracts.toml").read_text(encoding="utf-8").splitlines()


def _table_line(root: Path, header: str) -> int:
    """1-based line of a `[table]` header in the generated contract."""
    for number, text in enumerate(_lines(root), start=1):
        if text.strip() == header:
            return number
    raise AssertionError(f"{header} is not in the generated contracts.toml")


def _key_line(root: Path, header: str, key: str) -> int:
    """1-based line of `key = ...` inside `[header]`."""
    lines = _lines(root)
    start = _table_line(root, header)
    for number in range(start, len(lines) + 1):
        text = lines[number - 1].strip()
        if number > start and text.startswith("["):
            break
        if text.startswith(f"{key} ="):
            return number
    raise AssertionError(f"{key} is not declared in {header}")


def _comment_directly_above(root: Path, line: int) -> list[str]:
    lines = _lines(root)
    found: list[str] = []
    for number in range(line - 1, 0, -1):
        text = lines[number - 1].strip()
        if not text.startswith("#"):
            break
        found.append(text.lstrip("# ").strip())
    return list(reversed(found))


def _app() -> typer.Typer:
    app = typer.Typer()
    register(app)
    return app


# -- the declaring line, in every layout ---------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_forbidden_package_is_traced_back_to_the_line_that_forbids_it(
    tmp_path: Path, layout: str
) -> None:
    layer = HTTP_LAYER[layout]
    contract, root = _service(tmp_path, layout, {HTTP_FILE[layout]: "import sqlalchemy\n"})

    # The workflow: the checker names the rule, `explain` answers for it.
    assert "layer-package" in {v.rule for v in check(contract, root)}

    answer = explain(contract, root, subject=[layer, "sqlalchemy"])
    assert answer.rule == "layer-package"
    assert answer.verdict == "forbidden"

    expected = _key_line(root, f"[layers.{layer}]", "forbid_packages")
    declared = [(d.table, d.line) for d in answer.declarations]
    assert (f"layers.{layer}", expected) in declared, declared

    # The rationale is the one the contract's author wrote above that line,
    # not a sentence this command invented.
    rationale = _comment_directly_above(root, expected)
    assert rationale, "the template stopped explaining forbid_packages"
    assert rationale[-1] in " ".join(answer.why)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_layer_that_may_not_import_another_names_its_may_import_line(
    tmp_path: Path, layout: str
) -> None:
    contract, root = _service(tmp_path, layout, {})

    source, target = next(
        (a, b)
        for a in sorted(contract.layers)
        for b in sorted(contract.layers)
        if a != b and b not in contract.layers[a].may_import
    )
    answer = explain(contract, root, subject=[source, target])

    assert answer.verdict == "forbidden"
    assert answer.rule == "layer"
    expected = _key_line(root, f"[layers.{source}]", "may_import")
    assert (f"layers.{source}", expected) in [(d.table, d.line) for d in answer.declarations]
    assert target not in contract.layers[source].may_import


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_permitted_import_is_answered_as_permitted(tmp_path: Path, layout: str) -> None:
    contract, root = _service(tmp_path, layout, {})
    source, target = next(
        (a, b) for a in sorted(contract.layers) for b in contract.layers[a].may_import
    )
    answer = explain(contract, root, subject=[source, target])
    assert answer.verdict == "allowed"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_cross_module_is_traced_to_the_placement_rule(tmp_path: Path, layout: str) -> None:
    contract, root = _service(
        tmp_path,
        layout,
        {
            HTTP_FILE[layout]: "from modules.customer.service import CustomerService\n",
            "modules/customer/service.py": "class CustomerService:\n    pass\n",
        },
    )
    assert "cross-module" in {v.rule for v in check(contract, root)}

    answer = explain(contract, root, subject=["invoice", "customer"])
    assert answer.rule == "cross-module"
    assert answer.verdict == "forbidden"

    expected = _table_line(root, "[rules.placement]")
    assert ("rules.placement", expected) in [(d.table, d.line) for d in answer.declarations]
    assert "Queries through a facade" in " ".join(answer.why)
    # The remedy has to name a destination. "Do not do that" is what makes an
    # agent delete the import instead of moving the code: the facade for data,
    # an event for reactions, shared/ for vocabulary.
    assert any("modules/customer/public.py" in step for step in answer.instead)
    assert any("outbox" in step for step in answer.instead)
    assert any("shared/" in step for step in answer.instead)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_rule_name_alone_finds_every_place_it_is_declared(tmp_path: Path, layout: str) -> None:
    contract, root = _service(tmp_path, layout, {})
    answer = explain(contract, root, rule="forbid-call")
    declared = [(d.table, d.line) for d in answer.declarations]
    assert declared, "no declaration found for a rule the template always writes"
    assert all(table == "rules.forbid_call" for table, _ in declared)
    assert any("Configuration is typed" in why for why in answer.why)


# -- honesty ------------------------------------------------------------


def test_an_unknown_name_says_so_and_lists_what_it_knows(tmp_path: Path) -> None:
    contract, root = _service(tmp_path, "layered", {})
    answer = explain(contract, root, subject=["billing", "analytics"])
    assert answer.verdict == "unknown"
    assert answer.unknown
    assert "http" in answer.context["layers"]


def test_a_file_no_layer_claims_is_reported_as_unchecked(tmp_path: Path) -> None:
    contract, root = _service(tmp_path, "layered", {"scripts/backfill.py": "import sqlalchemy\n"})
    answer = explain(contract, root, file="scripts/backfill.py")
    assert answer.verdict == "unknown"
    assert any("no layer" in note for note in answer.unknown)


def test_a_rule_the_contract_never_declares_is_not_invented(tmp_path: Path) -> None:
    contract, root = _service(tmp_path, "layered", {})
    answer = explain(contract, root, rule="forbid-import")
    assert answer.declarations == ()
    assert answer.unknown


# -- what a file may do -------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
def test_explaining_a_file_names_its_layer_and_its_live_violations(
    tmp_path: Path, layout: str
) -> None:
    relative = HTTP_FILE[layout]
    contract, root = _service(tmp_path, layout, {relative: "import sqlalchemy\n"})
    answer = explain(contract, root, file=relative)

    assert answer.context["layer"] == HTTP_LAYER[layout]
    assert answer.context["may_import"] == contract.layers[HTTP_LAYER[layout]].may_import
    assert [v["rule"] for v in answer.context["violations"]] == ["layer-package"]


# -- the JSON an agent reads instead of contracts.toml -------------------


def test_the_json_carries_everything_needed_without_opening_the_contract(tmp_path: Path) -> None:
    _service(tmp_path, "layered", {HTTP_FILE["layered"]: "import sqlalchemy\n"})
    result = runner.invoke(
        _app(), ["explain", "http", "sqlalchemy", "--json", "--contract", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["rule"] == "layer-package"
    assert payload["verdict"] == "forbidden"

    declaration = payload["declarations"][0]
    assert declaration["file"].endswith("contracts.toml")
    assert declaration["line"] > 0
    assert "forbid_packages" in declaration["text"]
    assert declaration["comment"]

    assert payload["why"] and payload["instead"]
    # What waiving costs, both halves, so the cheap silent option is not the
    # one an agent discovers first.
    waiver = payload["waiver"]
    assert waiver["inline"] == "# contracts: allow <reason>"
    assert "jfast contracts waivers" in waiver["listed_by"]
    assert "everyone" in waiver["editing_the_contract"]


def test_the_human_output_leads_with_the_rule_and_the_file_and_line(tmp_path: Path) -> None:
    _service(tmp_path, "layered", {})
    result = runner.invoke(_app(), ["explain", "http", "sqlalchemy", "--contract", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "layer-package" in result.output
    assert "contracts.toml:" in result.output
    assert "contracts: allow" in result.output


def test_an_unanswerable_question_exits_non_zero(tmp_path: Path) -> None:
    _service(tmp_path, "layered", {})
    result = runner.invoke(_app(), ["explain", "nope", "nothing", "--contract", str(tmp_path)])
    assert result.exit_code != 0
    assert "nope" in result.output


def test_with_no_arguments_it_lists_the_rules_that_can_fire(tmp_path: Path) -> None:
    _service(tmp_path, "layered", {})
    result = runner.invoke(_app(), ["explain", "--contract", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for rule in ("layer", "layer-package", "cross-module", "shared-direction", "async-blocking"):
        assert rule in result.output


# -- diff ----------------------------------------------------------------


def test_diff_reports_an_import_the_contract_does_not_permit(tmp_path: Path) -> None:
    contract, root = _service(
        tmp_path,
        "layered",
        {
            "modules/invoice/service.py": (
                "from modules.customer.service import CustomerService\n\n\n"
                "class InvoiceService:\n    pass\n"
            ),
            "modules/customer/service.py": "class CustomerService:\n    pass\n",
        },
    )
    report = diff(contract, root)

    added = {(d.source, d.target) for d in report.added if d.kind == "module"}
    assert ("invoice", "customer") in added
    # The cost of enforcing it, named: which symbol the importer loses, so the
    # remedy is "move this", not "delete the line".
    assert any("CustomerService" in cost for cost in report.costs)


def test_diff_reports_a_permission_nothing_uses(tmp_path: Path) -> None:
    """The `-` half, on a tree the contract does describe.

    The files are in the layered layout the contract names, so both layers in
    the edge govern something and the unused permission between them is the
    real thing rather than a symptom of the contract missing the tree.
    """
    contract, root = _service(
        tmp_path,
        "layered",
        {
            "modules/invoice/router.py": "router = None\n",
            "modules/invoice/service.py": "class InvoiceService:\n    pass\n",
        },
    )
    counts = layer_matches(contract, root)
    assert counts["http"] and counts["service"]

    report = diff(contract, root)
    assert ("http", "service") in {(d.source, d.target) for d in report.removed}
    assert ("http", "service") not in {(d.source, d.target) for d in report.unsound}


# -- diff on a contract that governs nothing -----------------------------


def test_diff_does_not_offer_a_dead_layer_as_a_tightening_opportunity(tmp_path: Path) -> None:
    """The defect: ten `-` edges at once, on a contract enforcing nothing.

    Every permission the contract declares lands in `permitted - observed`
    when no file is in any of its layers, and the command read that as ten
    chances to tighten the architecture. It is one outage.
    """
    contract, root = _mismatched(tmp_path)
    empty = {name for name, count in layer_matches(contract, root).items() if not count}
    assert empty, "the layered contract now matches the hexagonal tree; the reproduction is stale"

    report = diff(contract, root)
    assert set(report.ungoverned) == empty
    assert report.unsound, "the permissions on the empty layers vanished instead of being reframed"
    assert all(empty & {d.source, d.target} for d in report.unsound)
    assert not any(empty & {d.source, d.target} for d in report.removed)

    # The rule `contracts check` reports for this state. Two commands naming
    # one condition two ways is how a reader ends up trusting neither.
    assert {d.rule for d in report.unsound} == {"layer-unmatched"}
    assert "layer-unmatched" in {v.rule for v in check(contract, root)}


def test_diff_says_which_layers_govern_nothing_before_it_lists_their_edges(
    tmp_path: Path,
) -> None:
    _mismatched(tmp_path)
    result = runner.invoke(_app(), ["diff", "--contract", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert "govern no file" in result.output
    assert "layer-unmatched" in result.output
    assert "~ http -> service" in result.output
    # `http` governs nothing, so none of its permissions may be printed under
    # the mark that means "you could tighten this".
    assert "- http ->" not in result.output
    assert result.output.index("govern no file") < result.output.index("~ http -> service")


def test_diff_json_keeps_the_dead_permissions_out_of_removed(tmp_path: Path) -> None:
    contract, root = _mismatched(tmp_path)
    empty = {name for name, count in layer_matches(contract, root).items() if not count}
    result = runner.invoke(_app(), ["diff", "--json", "--contract", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert set(payload["ungoverned_layers"]) == empty
    assert not any(empty & {e["source"], e["target"]} for e in payload["removed"])
    assert {edge["rule"] for edge in payload["unsound"]} == {"layer-unmatched"}
    assert {edge["state"] for edge in payload["unsound"]} == {"ungoverned"}


def test_diff_output_says_what_it_compared(tmp_path: Path) -> None:
    _service(
        tmp_path,
        "layered",
        {
            "modules/invoice/service.py": "from modules.customer.service import CustomerService\n",
            "modules/customer/service.py": "class CustomerService:\n    pass\n",
        },
    )
    result = runner.invoke(_app(), ["diff", "--contract", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "+ invoice -> customer" in result.output
    # Not a git diff, and the output has to say so rather than imply one.
    assert "not a git diff" in result.output.lower()


def test_diff_json_is_complete(tmp_path: Path) -> None:
    _service(
        tmp_path,
        "layered",
        {
            "modules/invoice/service.py": "from modules.customer.service import CustomerService\n",
            "modules/customer/service.py": "class CustomerService:\n    pass\n",
        },
    )
    result = runner.invoke(_app(), ["diff", "--json", "--contract", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["compares"]
    edge = next(e for e in payload["added"] if e["kind"] == "module")
    assert edge["source"] == "invoice"
    assert edge["target"] == "customer"
    assert edge["rule"] == "cross-module"
    assert edge["evidence"]
    assert edge["declaration"]["line"] > 0


# -- wiring --------------------------------------------------------------


def test_register_attaches_both_commands_to_a_contracts_group() -> None:
    root = typer.Typer()
    contracts = typer.Typer()
    root.add_typer(contracts, name="contracts")
    register(root)

    names = {command.name for command in contracts.registered_commands}
    assert names == {"explain", "diff"}
