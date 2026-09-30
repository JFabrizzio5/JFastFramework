"""Events and tasks in the contract: the coupling no import shows.

The case that motivated it, from Cuadra: to avoid a cycle between
``comprobante`` and ``alerta``, ``comprobante`` queued
``Job(task="alerta.revisar_presupuesto")`` -- a call into ``alerta`` spelt as
a string -- and ``contracts check`` passed. Each rule here has a positive
case, a negative one, and a waiver:

* ``undeclared-dependency`` now also covers queuing another module's task;
* ``orphan-subscription`` -- a ``@subscribe`` nothing declares publishing;
* ``undeclared-event`` -- an ``Event(type=...)`` its module does not declare;
* ``unused-dependency`` -- a ``depends_on`` entry nothing uses.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from jfastframework import project as project_model
from jfastframework.cli.main import app
from jfastframework.contracts import Contract, check_placement, render
from jfastframework.contracts.explain import RULES, explain
from jfastframework.contracts.placement import RULES as PLACEMENT_RULES
from jfastframework.contracts.wiring import facade_functions, scan, toml_line

runner = CliRunner()

BASE = """
[project]
name = "cuadra"

[rules.placement]
enabled = true
"""

ALERTA_TASKS = (
    "from jfastframework.events import Event, subscribe\n"
    "from jfastframework.tasks import TaskSession, task\n\n\n"
    '@task("alerta.revisar_presupuesto")\n'
    "async def revisar(payload, session: TaskSession):\n"
    "    ...\n\n\n"
    '@subscribe("comprobante.registrado")\n'
    "async def al_registrar(event: Event, session: TaskSession):\n"
    "    ...\n"
)

PUBLISHES = (
    "from jfastframework.events import Event\n\n\n"
    "async def registrar(outbox, session):\n"
    '    await outbox.publish(session, "comprobantes", Event(type="comprobante.registrado"))\n'
)

QUEUES_BY_NAME = (
    "from jfastframework.queues import Job\n\n\n"
    "async def registrar(outbox, session):\n"
    '    await outbox.enqueue(session, Job(task="alerta.revisar_presupuesto"))\n'
)

FACADE = (
    "__all__ = ['total_del_mes']\n\n\n"
    "async def total_del_mes(session, *, tenant_id):\n"
    "    return 0\n\n\n"
    "async def _privada():\n"
    "    return 0\n"
)


def build(tmp_path: Path, files: dict[str, str], extra: str = "") -> tuple[Contract, Path]:
    (tmp_path / "contracts.toml").write_text(BASE + extra, encoding="utf-8")
    files = {
        "modules/comprobante/__init__.py": "",
        "modules/alerta/__init__.py": "",
        **files,
    }
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return Contract.load(tmp_path / "contracts.toml"), tmp_path


def rules(contract: Contract, root: Path) -> list[str]:
    return sorted(v.rule for v in check_placement(contract, root))


DECLARED = '\n[modules.comprobante]\npublishes = ["comprobante.registrado"]\n'


# -- the working pattern -----------------------------------------------------


def test_publish_and_subscribe_with_no_dependency_passes(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/services.py": PUBLISHES,
            "modules/alerta/tasks.py": ALERTA_TASKS,
        },
        extra=DECLARED,
    )
    # The point of events: alerta reacts to comprobante and neither depends
    # on the other.
    assert rules(contract, root) == []


def test_publishes_is_read_per_module(tmp_path: Path) -> None:
    contract, _ = build(tmp_path, {}, extra=DECLARED)
    assert contract.module_publishes == {"comprobante": ["comprobante.registrado"]}
    assert contract.describe()["modules"]["comprobante"] == {
        "depends_on": [],
        "publishes": ["comprobante.registrado"],
    }


# -- orphan-subscription -----------------------------------------------------


def test_a_subscription_nobody_declares_publishing_is_an_orphan(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"modules/alerta/tasks.py": ALERTA_TASKS})
    [violation] = check_placement(contract, root)
    assert violation.rule == "orphan-subscription"
    assert (violation.path, violation.line) == ("modules/alerta/tasks.py", 10)
    assert 'publishes = ["comprobante.registrado"]' in violation.why


def test_an_orphan_subscription_can_be_waived(tmp_path: Path) -> None:
    body = ALERTA_TASKS.replace(
        '@subscribe("comprobante.registrado")',
        '@subscribe("comprobante.registrado")  # contracts: allow publisher lands in JF-12',
    )
    contract, root = build(tmp_path, {"modules/alerta/tasks.py": body})
    assert rules(contract, root) == []


def test_an_unrelated_decorator_called_subscribe_is_not_read(tmp_path: Path) -> None:
    body = (
        "from somewhere import subscribe\n\n\n"
        '@subscribe("anything")\nasync def f(event):\n    ...\n'
    )
    contract, root = build(tmp_path, {"modules/alerta/tasks.py": body})
    assert rules(contract, root) == []


# -- undeclared-event --------------------------------------------------------


def test_publishing_an_undeclared_event_is_reported(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"modules/comprobante/services.py": PUBLISHES})
    [violation] = check_placement(contract, root)
    assert violation.rule == "undeclared-event"
    assert violation.line == 5
    assert "[modules.comprobante]" in violation.why


def test_declaring_it_for_another_module_does_not_count(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/comprobante/services.py": PUBLISHES},
        extra='\n[modules.alerta]\npublishes = ["comprobante.registrado"]\n',
    )
    assert rules(contract, root) == ["undeclared-event"]


def test_an_undeclared_event_can_be_waived_and_tests_are_not_read(tmp_path: Path) -> None:
    waived_body = PUBLISHES.replace(
        '"comprobante.registrado"))',
        '"comprobante.registrado"))  # contracts: allow migration',
    )
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/services.py": waived_body,
            "modules/comprobante/tests/test_services.py": PUBLISHES,
        },
    )
    assert rules(contract, root) == []


def test_a_type_built_at_run_time_is_not_guessed(tmp_path: Path) -> None:
    body = PUBLISHES.replace('Event(type="comprobante.registrado")', "Event(type=NOMBRE)")
    contract, root = build(tmp_path, {"modules/comprobante/services.py": body})
    assert rules(contract, root) == []


# -- undeclared-dependency, through a task name ------------------------------


def test_queuing_another_modules_task_by_name_is_an_undeclared_dependency(
    tmp_path: Path,
) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/services.py": QUEUES_BY_NAME,
            "modules/alerta/tasks.py": ALERTA_TASKS,
        },
        extra=DECLARED,
    )
    [violation] = check_placement(contract, root)
    assert violation.rule == "undeclared-dependency"
    assert "queues task 'alerta.revisar_presupuesto'" in violation.message
    assert "@subscribe" in violation.why and "event" in violation.why


def test_a_task_registered_outside_the_module_is_owned_by_its_name_prefix(
    tmp_path: Path,
) -> None:
    # The real 0.1.0a10 Cuadra: the handler is registered in a root worker.py,
    # so no module declares it with @task. The name's prefix still says whose
    # it is -- otherwise the coupling this rule exists for is invisible.
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/services.py": QUEUES_BY_NAME,
            "worker.py": "tareas.task('alerta.revisar_presupuesto')(revisar)\n",
        },
        extra=DECLARED,
    )
    [violation] = check_placement(contract, root)
    assert violation.rule == "undeclared-dependency"
    assert "module 'alerta' owns" in violation.message


def test_a_prefix_that_is_no_module_owns_nothing(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/comprobante/services.py": QUEUES_BY_NAME.replace("alerta.", "correo.")},
        extra=DECLARED,
    )
    assert rules(contract, root) == []


def test_declared_it_is_an_edge_that_can_close_a_cycle(tmp_path: Path) -> None:
    # The Cuadra shape: alerta reads comprobante's facade, comprobante queues
    # alerta's task. Declared both ways, it is a cycle, and it is reported.
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/public.py": FACADE,
            "modules/comprobante/services.py": QUEUES_BY_NAME,
            "modules/alerta/tasks.py": ALERTA_TASKS,
            "modules/alerta/services.py": "from modules.comprobante.public import total_del_mes\n",
        },
        extra=DECLARED
        + 'depends_on = ["alerta"]\n\n[modules.alerta]\ndepends_on = ["comprobante"]\n',
    )
    violations = check_placement(contract, root)
    assert [v.rule for v in violations] == ["module-cycle"]
    assert "alerta -> comprobante -> alerta" in violations[0].message


def test_queuing_its_own_task_or_a_waived_one_is_fine(tmp_path: Path) -> None:
    own = QUEUES_BY_NAME
    waived = QUEUES_BY_NAME.replace(
        '"alerta.revisar_presupuesto"))',
        '"alerta.revisar_presupuesto"))  # contracts: allow until the event ships',
    )
    contract, root = build(
        tmp_path,
        {
            "modules/alerta/services.py": own,
            "modules/comprobante/services.py": waived,
            "modules/alerta/tasks.py": ALERTA_TASKS,
        },
        extra=DECLARED,
    )
    assert rules(contract, root) == []


# -- unused-dependency -------------------------------------------------------


def test_a_depends_on_entry_nothing_uses_is_reported_at_its_line(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/comprobante/public.py": FACADE},
        extra='\n[modules.alerta]\ndepends_on = [\n  "comprobante",\n]\n',
    )
    [violation] = check_placement(contract, root)
    assert violation.rule == "unused-dependency"
    assert violation.path == "contracts.toml"
    lines = (root / "contracts.toml").read_text(encoding="utf-8").splitlines()
    assert '"comprobante"' in lines[violation.line - 1]


def test_a_used_or_waived_dependency_is_not_reported(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/public.py": FACADE,
            "modules/alerta/services.py": "from modules.comprobante.public import total_del_mes\n",
        },
        extra='\n[modules.alerta]\ndepends_on = ["comprobante"]\n',
    )
    assert rules(contract, root) == []

    contract, root = build(
        tmp_path,
        {"modules/alerta/services.py": ""},
        extra='\n[modules.alerta]\ndepends_on = ["comprobante"]  # contracts: allow next PR\n',
    )
    assert rules(contract, root) == []


def test_every_new_rule_is_switched_off_with_placement(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/services.py": PUBLISHES + QUEUES_BY_NAME.split("\n\n\n")[1],
            "modules/alerta/tasks.py": ALERTA_TASKS.replace("comprobante.registrado", "otro"),
        },
        extra='\n[modules.alerta]\ndepends_on = ["comprobante"]\n',
    )
    assert {"orphan-subscription", "undeclared-event", "unused-dependency"} <= set(
        rules(contract, root)
    )
    contract.enforce_placement = False
    assert check_placement(contract, root) == []


# -- explain, show, render ---------------------------------------------------


def test_every_placement_rule_has_an_explanation() -> None:
    for rule in PLACEMENT_RULES:
        assert rule in RULES, rule
        assert RULES[rule].instead


def test_explain_points_at_the_publishes_declaration(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {}, extra=DECLARED)
    answer = explain(contract, root, rule="orphan-subscription").describe()
    assert answer["declarations"][0]["line"] == toml_line(
        (root / "contracts.toml").read_text(encoding="utf-8").splitlines(),
        "modules.comprobante",
        "publishes",
    )
    for rule in ("undeclared-event", "unused-dependency"):
        assert explain(contract, root, rule=rule).describe()["summary"]


def test_show_json_and_contracts_md_list_publishers_and_subscribers(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    contract, root = build(
        tmp_path,
        {
            "modules/comprobante/services.py": PUBLISHES,
            "modules/alerta/tasks.py": ALERTA_TASKS,
        },
        extra=DECLARED,
    )
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["contracts", "show", "--json"])
    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    event = shown["events"]["comprobante.registrado"]
    assert event["published_by"] == ["comprobante"]
    assert [s["module"] for s in event["subscribers"]] == ["alerta"]
    assert shown["tasks"]["alerta.revisar_presupuesto"]["module"] == "alerta"

    markdown = render(contract, root)
    assert "## Events and tasks" in markdown
    assert "| `comprobante.registrado` | `comprobante` | `alerta.al_registrar` |" in markdown
    assert "| `alerta.revisar_presupuesto` | `alerta` | — |" in markdown


# -- inspect and the agent context ------------------------------------------


def test_inspect_module_cycle_uses_declared_dependencies(tmp_path: Path) -> None:
    (tmp_path / "jfast.toml").write_text('[app]\nname = "cuadra"\n', encoding="utf-8")
    _, root = build(
        tmp_path,
        {"modules/alerta/services.py": "from modules.comprobante.public import total_del_mes\n"},
        extra='\n[modules.comprobante]\ndepends_on = ["alerta"]\n',
    )
    found = project_model.analyze(project_model.load(root))
    [cycle] = [f for f in found if f.code == "module-cycle"]
    assert "alerta -> comprobante -> alerta" in cycle.message


def test_ai_context_shows_facades_events_and_tasks(tmp_path: Path) -> None:
    (tmp_path / "jfast.toml").write_text('[app]\nname = "cuadra"\n', encoding="utf-8")
    _, root = build(
        tmp_path,
        {
            "modules/comprobante/public.py": FACADE,
            "modules/comprobante/services.py": PUBLISHES,
            "modules/alerta/tasks.py": ALERTA_TASKS,
        },
        extra=DECLARED,
    )
    result = runner.invoke(app, ["ai", "context", "--path", str(root)])
    payload = json.loads(result.stdout)
    modules = {m["name"]: m for m in payload["modules"]}
    assert modules["comprobante"]["facade"] == ["total_del_mes"]
    assert modules["comprobante"]["publishes"] == ["comprobante.registrado"]
    assert modules["alerta"]["subscribes"] == ["comprobante.registrado"]
    assert modules["alerta"]["tasks"] == ["alerta.revisar_presupuesto"]
    assert "comprobante.registrado" in payload["events"]["events"]


def test_facade_functions_without_all_are_the_public_defs(tmp_path: Path) -> None:
    path = tmp_path / "modules" / "x" / "public.py"
    path.parent.mkdir(parents=True)
    path.write_text("def a():\n    ...\n\n\nasync def b():\n    ...\n\n\ndef _c():\n    ...\n")
    assert facade_functions(tmp_path, "x") == ["a", "b"]
    assert facade_functions(tmp_path, "missing") == []


def test_the_scan_never_imports_the_project(tmp_path: Path) -> None:
    (tmp_path / "modules" / "boom").mkdir(parents=True)
    (tmp_path / "modules" / "boom" / "tasks.py").write_text(
        "raise SystemExit('imported')\n", encoding="utf-8"
    )
    assert scan(tmp_path).tasks == []
