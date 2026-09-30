"""The documented ``shared/`` move, checked against the contract each layout ships.

``shared/`` is generated into every service, and everything that describes it
says the same thing: when a second module needs the same enum, move it there.
``shared/README.md`` says it, ``docs/shared-and-events.md`` says it, the
``[rules.placement]`` comment block inside the generated ``contracts.toml``
says it, and the checker's own cross-module message says it while naming the
file. That instruction is only true if the generated contract lets a module
import ``shared/`` -- and in all four layouts it did not. Following the
framework's own documentation produced a ``layer`` violation on a project one
command old.

The drift is the reason this file exists. Nothing else compares what the
documentation promises against what the default contract permits, so the two
went apart with nothing to notice. These tests render each contract template,
perform the documented move, and require the result to pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.cli.scaffold import CONTRACT_TEMPLATE_FOR, MODULE_LAYOUTS, Scaffolder
from jfastframework.contracts import Contract, check

#: One representative file per layer, per layout, named the way the generator
#: names them. Every layer a template declares has to appear here --
#: `test_every_declared_layer_is_covered_here` is what stops a layer being
#: added to a template without anyone deciding whether it may speak the shared
#: vocabulary, which is exactly how this bug got in.
LAYER_FILES: dict[str, dict[str, str]] = {
    "layered": {
        "http": "modules/order/router.py",
        "service": "modules/order/service.py",
        "storage": "modules/order/repository.py",
        "schemas": "modules/order/schemas.py",
        # The facade: the one file another module may import.
        "public": "modules/order/public.py",
        # @task and @subscribe: an entry point, found at boot by API and worker.
        "tasks": "modules/order/tasks.py",
    },
    "modular": {
        "http": "modules/order/api/routes.py",
        "service": "modules/order/services/order_service.py",
        "validation": "modules/order/validations/order_validation.py",
        "storage": "modules/order/repositories/order_repository.py",
        "schemas": "modules/order/models/order_models.py",
        # The facade: the one file another module may import.
        "public": "modules/order/public.py",
        # @task and @subscribe: an entry point, found at boot by API and worker.
        "tasks": "modules/order/tasks.py",
    },
    "screaming": {
        "domain": "modules/order/order.py",
        "use_cases": "modules/order/use_cases/create_order.py",
        "storage": "modules/order/storage.py",
        "http": "modules/order/http.py",
        # The facade: the one file another module may import.
        "public": "modules/order/public.py",
        # @task and @subscribe: an entry point, found at boot by API and worker.
        "tasks": "modules/order/tasks.py",
    },
    "hexagonal": {
        "domain": "modules/order/domain/entities.py",
        "application": "modules/order/application/use_cases.py",
        "infrastructure": "modules/order/infrastructure/orm.py",
        "adapters": "modules/order/adapters/http.py",
        # The facade: the one file another module may import.
        "public": "modules/order/public.py",
        # @task and @subscribe: an entry point, found at boot by API and worker.
        "tasks": "modules/order/tasks.py",
    },
}

LAYER_CASES = [
    (layout, layer, path) for layout, files in LAYER_FILES.items() for layer, path in files.items()
]
LAYER_IDS = [f"{layout}-{layer}" for layout, layer, _ in LAYER_CASES]

#: What `jfast new enum Status --shared` writes.
SHARED_ENUMS = '''"""Enums shared by more than one module."""

from enum import Enum


class Status(str, Enum):
    DRAFT = "draft"
'''

#: What the documentation tells you to write next, in the module that needed it.
USES_SHARED = 'from shared.enums import Status\n\n__all__ = ["Status"]\n'


def scaffold(tmp_path: Path, layout: str, files: dict[str, str]) -> tuple[Contract, Path]:
    """A project carrying `layout`'s generated contract, a shared/ and `files`."""
    Scaffolder().render_tree(
        CONTRACT_TEMPLATE_FOR[layout],
        tmp_path,
        {"project": "shop", "layout": layout, "Project": "Shop"},
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "enums.py").write_text(SHARED_ENUMS, encoding="utf-8")

    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    # `[[rules.require]]` is not what these tests are about. Satisfy it so a
    # missing README never arrives disguised as a layer finding.
    for module in (tmp_path / "modules").glob("*"):
        if not module.is_dir():
            continue
        (module / "tests").mkdir(exist_ok=True)
        (module / "use_cases").mkdir(exist_ok=True)
        (module / "README.md").write_text(f"# {module.name}\n", encoding="utf-8")

    return Contract.load(tmp_path / "contracts.toml"), tmp_path


def rules(violations: list) -> list[str]:  # type: ignore[type-arg]
    return [v.rule for v in violations]


@pytest.mark.parametrize(("layout", "layer", "path"), LAYER_CASES, ids=LAYER_IDS)
def test_a_layer_may_import_the_shared_vocabulary(
    layout: str, layer: str, path: str, tmp_path: Path
) -> None:
    # The reproduction: `shared/enums.py` ships with the service, the docs say
    # to put the twice-wanted enum in it, and the contract rejected the import
    # at generation time. A default that contradicts its own instructions
    # teaches everyone to ignore the checker on day one.
    contract, root = scaffold(tmp_path, layout, {path: USES_SHARED})
    violations = check(contract, root)
    assert violations == [], f"{layout}/{layer}: " + "; ".join(str(v) for v in violations)


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_shared_may_still_not_import_a_module(layout: str, tmp_path: Path) -> None:
    # The other half of the rule, and the half that keeps the first one safe:
    # widening the permission is only sound while the direction stays one-way.
    contract, root = scaffold(tmp_path, layout, {})
    (root / "shared" / "enums.py").write_text(
        "from modules.order.enums import Status\n", encoding="utf-8"
    )
    assert "shared-direction" in rules(check(contract, root))


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_a_module_may_still_not_import_another_module(layout: str, tmp_path: Path) -> None:
    path = next(iter(LAYER_FILES[layout].values()))
    contract, root = scaffold(
        tmp_path, layout, {path: "from modules.payment.enums import Method\n"}
    )
    # If this stopped firing, `shared/` would have become optional rather than
    # the answer, and the cross-import it exists to replace would be legal.
    assert "cross-module" in rules(check(contract, root))


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_shared_itself_declares_no_imports(layout: str, tmp_path: Path) -> None:
    contract, _ = scaffold(tmp_path, layout, {})
    # Not an accident of the template: `[rules.placement]` reads the same
    # direction from the other side, and a `shared` that may import a layer
    # turns the dependency graph into a circle.
    assert contract.layers["shared"].may_import == []


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_every_declared_layer_is_covered_here(layout: str, tmp_path: Path) -> None:
    contract, _ = scaffold(tmp_path, layout, {})
    declared = set(contract.layers) - {"shared"}
    # A new layer in a template is a new decision about the shared vocabulary.
    # Failing here is the prompt to make it deliberately.
    assert declared == set(LAYER_FILES[layout])
