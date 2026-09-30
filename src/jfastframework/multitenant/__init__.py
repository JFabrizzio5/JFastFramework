"""Single-tenant today, multitenant tomorrow.

Three pieces, each usable without the others:

* :mod:`~jfastframework.multitenant.consistency` -- tenant settings that
  contradict each other or the code today. The ``tenancy`` check of
  ``jfast check``.
* :mod:`~jfastframework.multitenant.readiness` -- what a switch to several
  customers would break, with file and line. ``jfast check
  --multitenant-ready``.
* :mod:`~jfastframework.multitenant.switch` -- the switch itself, as a
  revision and a jfast.toml edit. ``jfast tenancy enable``.

Nothing here imports project code or connects to a database.
"""

from __future__ import annotations

from jfastframework.multitenant.consistency import consistency_findings
from jfastframework.multitenant.readiness import RULES, Readiness, ReadinessFinding, readiness
from jfastframework.multitenant.switch import SwitchError, SwitchPlan, plan_switch

__all__ = [
    "RULES",
    "Readiness",
    "ReadinessFinding",
    "SwitchError",
    "SwitchPlan",
    "consistency_findings",
    "plan_switch",
    "readiness",
]
