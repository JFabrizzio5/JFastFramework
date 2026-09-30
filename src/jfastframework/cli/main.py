"""The ``jfast`` command line.

Two audiences, one interface:

* humans get readable output;
* agents get ``--json`` on every inspection command, so an AI assistant reads
  machine state instead of grepping the source tree.
"""

from __future__ import annotations

import typer

from jfastframework.cli import ai as ai_cli
from jfastframework.cli import check as check_cli
from jfastframework.cli import dev as devtools
from jfastframework.cli import explain as explain_cli
from jfastframework.cli import migrations as migrations_cli
from jfastframework.cli import tenancy as tenancy_cli
from jfastframework.cli import upgrade as upgrade_cli
from jfastframework.cli.commands import add as add_cli
from jfastframework.cli.commands import contracts as contracts_cli
from jfastframework.cli.commands import deploy as deploy_cli
from jfastframework.cli.commands import describe as describe_cli
from jfastframework.cli.commands import install as install_cli
from jfastframework.cli.commands import new as new_cli
from jfastframework.cli.commands import project as project_cli
from jfastframework.cli.commands import run as run_cli
from jfastframework.cli.commands import worker as worker_cli
from jfastframework.cli.commands import workspace as workspace_cli
from jfastframework.cli.generate import (
    _write_dockerignore,
    _write_service_envs,
    _write_workspace_secrets,
)

# What other code reaches through this module. `devtools` is the module `jfast
# dev` calls into, so replacing an attribute on it here replaces it for the
# command; the three writers are imported by the tests from their old home.
__all__ = [
    "_write_dockerignore",
    "_write_service_envs",
    "_write_workspace_secrets",
    "app",
    "devtools",
]

app = typer.Typer(
    name="jfast",
    help="JFastFramework: build, inspect and deploy plugin-based FastAPI services.",
    no_args_is_help=True,
    add_completion=False,
)

# ---------------------------------------------------------------------------
# The command table.
#
# The order of these calls is the order `jfast --help` prints: top-level
# commands in the order they are attached, then the groups in theirs. The two
# orders are independent -- `new` is first because the `new` group heads the
# groups, not because anything it adds heads the commands. Moving a line here
# reorders the help.
# ---------------------------------------------------------------------------
new_cli.register(app)
describe_cli.register(app)
deploy_cli.register(app)
add_cli.register(app)
run_cli.register(app)
worker_cli.register(app)
workspace_cli.register(app)
contracts_cli.register(app)
install_cli.register(app)
project_cli.register(app)

# ---------------------------------------------------------------------------
# The lifecycle commands.
#
# Each lives in its own module and attaches itself rather than being spelled out
# here: a shared block in this file is the one thing more than one author cannot
# edit at once.
#
# `explain` goes last on purpose -- it attaches to the `contracts` group above
# when it finds one, and would otherwise become a top-level `jfast explain`.
# ---------------------------------------------------------------------------
migrations_cli.register(app)
check_cli.register(app)
tenancy_cli.register(app)
ai_cli.register(app)
upgrade_cli.register(app)
explain_cli.register(app)


if __name__ == "__main__":  # pragma: no cover
    app()
