"""The field grammar behind `jfast new module --fields` and `--unique`.

    jfast new module presupuesto \\
        --fields "cartera_id:int, mes:str(7), gasto:money, leida:bool=false, nota:text?" \\
        --unique "cartera_id,mes"

A module generated with example fields -- ``name``, ``description``,
``is_active`` -- never fits a real domain, and the first thing done with it
was deleting most of it: 654 generated lines became 276 in the first real
module built on 0.1.0a10. Declaring the real fields up front puts them in every
place the example ones used to be -- the table, the Pydantic models, the
domain entity, the repository finders, the uniqueness rule, the public DTO and
the tests -- so there is nothing to delete.

Grammar, one field per comma (commas inside parentheses do not split)::

    field    := name ":" type ["?"] ["=" default]
    type     := int | bigint | str | str(N) | text | bool | float
              | decimal(P,S) | money | date | datetime | json

``?`` makes the column nullable. ``=default`` is the value a create without
the field gets; it is written in the type's own syntax (``=0``, ``=true``,
``=pending``, ``="two words"``, ``=0.00``). ``date``, ``datetime`` and ``json``
take no default: a default date is a decision about "now" that belongs in the
service, where the tenant's zone is known.

Every parse error names the field, what was wrong, and the fix, because the
person reading it is typing a command line, not reading this docstring.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

#: Every type the grammar accepts, with what it becomes -- the table in
#: docs/modules.md is this, in prose.
TYPES = (
    "int",
    "bigint",
    "str",
    "text",
    "bool",
    "float",
    "decimal",
    "money",
    "date",
    "datetime",
    "json",
)

#: `str` without a length. VARCHAR(255) is a limit nobody chose, which is why
#: `str(N)` exists; this is what `str` alone means rather than an unbounded one.
DEFAULT_STR_LENGTH = 255

#: Columns every generated entity already has, from the primary key and the
#: mixins, or that SQLAlchemy's declarative base reserves.
RESERVED = frozenset(
    {"id", "tenant_id", "created_at", "updated_at", "version", "metadata", "registry"}
)

#: What `jfast new module` generates with no --fields and no --bare: the
#: example the templates have always carried, now expressed in the grammar so
#: one set of templates renders both.
EXAMPLE_FIELDS = "name:str(200), description:str(2000)?, is_active:bool=true"
EXAMPLE_UNIQUE = ("name",)

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TYPE = re.compile(r"(?P<type>[a-z]+)(?:\((?P<args>[^)]*)\))?(?P<nullable>\?)?")
_POSTGRES_IDENTIFIER_LIMIT = 63


class FieldSpecError(ValueError):
    """A --fields or --unique value that cannot be turned into a module."""


@dataclass(frozen=True)
class FieldSpec:
    """One declared field, and every way the templates need to spell it."""

    name: str
    type: str
    nullable: bool = False
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    #: The Python literal of the default, or None when there is none. A
    #: nullable field with no default defaults to None, which is not this.
    default: str | None = None

    # -- Python ---------------------------------------------------------

    @property
    def py_type(self) -> str:
        return {
            "int": "int",
            "bigint": "int",
            "money": "int",
            "str": "str",
            "text": "str",
            "bool": "bool",
            "float": "float",
            "decimal": "Decimal",
            "date": "date",
            "datetime": "datetime",
            "json": "dict[str, Any]",
        }[self.type]

    @property
    def annotation(self) -> str:
        return f"{self.py_type} | None" if self.nullable else self.py_type

    @property
    def optional(self) -> bool:
        """Whether a create may leave it out."""
        return self.nullable or self.default is not None

    @property
    def python_default(self) -> str | None:
        """The default a dataclass or a Pydantic model writes, as source."""
        if self.default is not None:
            return self.default
        if self.nullable:
            return "None"
        return None

    # -- SQLAlchemy -----------------------------------------------------

    @property
    def column_args(self) -> list[str]:
        """The arguments of ``mapped_column(...)`` for this field."""
        args: list[str] = []
        sa_type = {
            "bigint": "BigInteger",
            "money": "BigInteger",
            "str": f"String({self.length})",
            "text": "Text",
            "decimal": f"Numeric({self.precision}, {self.scale})",
            "datetime": "UTCDateTime()",
            "json": "JSON_COLUMN",
        }.get(self.type)
        if sa_type:
            args.append(sa_type)
        if self.python_default is not None:
            args.append(f"default={self.python_default}")
        return args

    @property
    def column(self) -> str:
        """The right-hand side of ``name: Mapped[...] = ...``."""
        return f"mapped_column({', '.join(self.column_args)})"

    @property
    def sqlalchemy_names(self) -> set[str]:
        return {
            "bigint": {"BigInteger"},
            "money": {"BigInteger"},
            "str": {"String"},
            "text": {"Text"},
            "decimal": {"Numeric"},
            "json": {"JSON"},
        }.get(self.type, set())

    # -- Pydantic -------------------------------------------------------

    @property
    def constraints(self) -> list[str]:
        """``Field(...)`` keyword arguments that restate the column's limits.

        A limit the database enforces and the model does not is a 500 where a
        422 belonged: the row reaches the INSERT and PostgreSQL refuses it.
        """
        found: list[str] = []
        if self.type in ("str", "text") and not self.nullable:
            # Required, so not empty: an empty string is the value a form
            # sends for "nothing typed", and NOT NULL does not catch it.
            found.append("min_length=1")
        if self.type == "str":
            found.append(f"max_length={self.length}")
        if self.type == "decimal":
            found += [f"max_digits={self.precision}", f"decimal_places={self.scale}"]
        return found

    @property
    def wire_type(self) -> str:
        """The type a request body declares: aware datetimes only."""
        if self.type == "datetime":
            return "AwareDatetime"
        return self.py_type

    def create_declaration(self) -> str:
        """``name: type = Field(...)`` for the create model."""
        annotation = f"{self.wire_type} | None" if self.nullable else self.wire_type
        arguments = list(self.constraints)
        default = self.python_default
        if default is not None:
            arguments.insert(0, f"default={default}")
        if not arguments:
            return f"{self.name}: {annotation}"
        if arguments == [f"default={default}"]:
            return f"{self.name}: {annotation} = {default}"
        return f"{self.name}: {annotation} = Field({', '.join(arguments)})"

    def update_declaration(self) -> str:
        """``name: type | None = ...`` for the partial update model."""
        arguments = ["default=None", *self.constraints]
        if len(arguments) == 1:
            return f"{self.name}: {self.wire_type} | None = None"
        return f"{self.name}: {self.wire_type} | None = Field({', '.join(arguments)})"

    # -- tests ----------------------------------------------------------

    def sample(self, index: int) -> str:
        """A valid value as source, different for index 0 and 1."""
        n = index + 1
        if self.type in ("int", "bigint"):
            return str(n)
        if self.type == "money":
            # Minor units: 1050 is 10.50 in the currency's major unit.
            return str(1050 * n)
        if self.type in ("str", "text"):
            text = f"{self.name}-{n}"
            limit = self.length or len(text)
            if len(text) > limit:
                text = ("ab"[index] * limit)[:limit]
            return _quoted(text)
        if self.type == "bool":
            return "True" if index == 0 else "False"
        if self.type == "float":
            return f"{n}.5"
        if self.type == "decimal":
            integer_digits = (self.precision or 1) - (self.scale or 0)
            whole = str(n) if integer_digits > 0 else "0"
            fraction = ("5" * (self.scale or 0)) if index else ("0" * (self.scale or 0))
            literal = f"{whole}.{fraction}" if fraction else whole
            return f'Decimal("{literal}")'
        if self.type == "date":
            return f"date(2026, 1, {n})"
        if self.type == "datetime":
            return f"datetime(2026, 1, {n}, 12, 0, tzinfo=UTC)"
        return f'{{"key": {n}}}'

    def check(self, source: str, index: int) -> str:
        """``source == sample``, or ``is`` for a bool, the way ruff wants it compared."""
        operator = "is" if self.type == "bool" else "=="
        return f"{source} {operator} {self.sample(index)}"

    @property
    def too_long(self) -> str | None:
        """A value one past the limit, or None when the type has none."""
        if self.type == "str" and self.length is not None:
            return f'"x" * {self.length + 1}'
        return None

    # -- the domain entity (screaming and hexagonal) ---------------------

    def domain_checks(self) -> list[tuple[str, str]]:
        """``(condition that is a violation, message)`` pairs for validate()."""
        checks: list[tuple[str, str]] = []
        ref = f"self.{self.name}"
        if self.type in ("str", "text") and not self.nullable:
            checks.append((f"not {ref}.strip()", f"{self.name} must not be empty"))
        if self.type == "str":
            if self.nullable:
                condition = f"{ref} is not None and len({ref}) > {self.length}"
            else:
                condition = f"len({ref}) > {self.length}"
            checks.append((condition, f"{self.name} must be at most {self.length} characters"))
        return checks


@dataclass(frozen=True)
class UniqueSpec:
    """One ``--unique`` set: a constraint, a finder and a rule, named once."""

    fields: tuple[FieldSpec, ...]
    table: str

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @property
    def suffix(self) -> str:
        return "_and_".join(self.names)

    @property
    def finder(self) -> str:
        return f"by_{self.suffix}"

    @property
    def port_finder(self) -> str:
        return f"find_by_{self.suffix}"

    @property
    def rule(self) -> str:
        return f"ensure_{self.suffix}_is_available"

    @property
    def parameter_list(self) -> list[str]:
        return [f"{f.name}: {f.annotation}" for f in self.fields]

    @property
    def parameters(self) -> str:
        return ", ".join(self.parameter_list)

    @property
    def arguments(self) -> str:
        return ", ".join(self.names)

    def matches(self, item: str) -> str:
        """``i.a == a and i.b == b``: the key compared on one stored object."""
        return " and ".join(f"{item}.{name} == {name}" for name in self.names)

    def sample_arguments(self, index: int) -> list[str]:
        """The key's sample values, positionally, for calling the rule in a test."""
        return [f.sample(index) for f in self.fields]

    def prefixed(self, prefix: str) -> list[str]:
        """``["payload.a", "payload.b"]``: the key read off one object."""
        return [f"{prefix}{name}" for name in self.names]

    def merged_arguments(self, current: str) -> list[str]:
        """The key after a patch: the new value where sent, the stored one where not."""
        return [f'changes.get("{name}", {current}.{name})' for name in self.names]

    @property
    def keywords(self) -> str:
        return ", ".join(f"{name}={name}" for name in self.names)

    @property
    def described(self) -> str:
        """``cartera_id={cartera_id!r} and mes={mes!r}`` for an error message."""
        return " and ".join(f"{name}={{{name}!r}}" for name in self.names)

    @property
    def constraint_name(self) -> str:
        """An explicit name, because the naming convention cannot tell two apart.

        ``uq_%(table_name)s_%(column_0_name)s`` names every tenant-first unique
        constraint ``uq_<table>_tenant_id``, so a second ``--unique`` would
        collide with the first in the migration. Long names are shortened with
        a hash rather than cut, so two long ones stay distinct.
        """
        name = f"uq_{self.table}_{'_'.join(self.names)}"
        if len(name) <= _POSTGRES_IDENTIFIER_LIMIT:
            return name
        digest = hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()[:8]
        return f"{name[: _POSTGRES_IDENTIFIER_LIMIT - 9]}_{digest}"

    @property
    def columns(self) -> str:
        return ", ".join(f'"{name}"' for name in ("tenant_id", *self.names))


@dataclass(frozen=True)
class ModuleFields:
    """Everything the module templates render from: the fields and the rules."""

    fields: tuple[FieldSpec, ...] = ()
    uniques: tuple[UniqueSpec, ...] = ()
    #: The historical example module: it also gets the example-only rules
    #: (deactivating twice, renaming) that only make sense for its fields.
    example: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> tuple[FieldSpec, ...]:
        """Fields a create must carry, in declaration order."""
        return tuple(f for f in self.fields if not f.optional)

    @property
    def non_nullable(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if not f.nullable)

    @property
    def domain_order(self) -> tuple[FieldSpec, ...]:
        """Required first: a dataclass field with no default cannot follow one with."""
        return self.required + tuple(f for f in self.fields if f.optional)

    @property
    def unique_names(self) -> set[str]:
        return {name for unique in self.uniques for name in unique.names}

    def stdlib(self, *, wire: bool = False) -> list[str]:
        """Dotted standard-library names the fields' types need.

        ``wire`` is for a Pydantic model, which spells a datetime
        ``AwareDatetime`` and so does not need the ``datetime`` import.
        """
        needed: set[str] = set()
        for f in self.fields:
            if f.type == "decimal":
                needed.add("decimal.Decimal")
            elif f.type == "date":
                needed.add("datetime.date")
            elif f.type == "datetime" and not wire:
                needed.add("datetime.datetime")
            elif f.type == "json":
                needed.add("typing.Any")
        return sorted(needed)

    def sqlalchemy(self, *, unique: bool = True) -> list[str]:
        """Dotted SQLAlchemy names the columns (and the constraints) need."""
        names: set[str] = set()
        for f in self.fields:
            names |= {f"sqlalchemy.{name}" for name in f.sqlalchemy_names}
        if self.needs_jsonb:
            names.add("sqlalchemy.dialects.postgresql.JSONB")
        if unique and self.uniques:
            names.add("sqlalchemy.UniqueConstraint")
        return sorted(names)

    def key_stdlib(self) -> list[str]:
        """Standard-library names the unique keys' parameters need."""
        keyed = ModuleFields(tuple(f for u in self.uniques for f in u.fields))
        return keyed.stdlib()

    def sample_stdlib(self) -> list[str]:
        """What a test needs to spell every field's sample values."""
        needed: set[str] = set()
        for f in self.fields:
            if f.type == "decimal":
                needed.add("decimal.Decimal")
            elif f.type == "date":
                needed.add("datetime.date")
            elif f.type == "datetime":
                needed |= {"datetime.datetime", "datetime.UTC"}
        return sorted(needed)

    @property
    def has_limits(self) -> bool:
        return any(f.too_long for f in self.fields)

    @property
    def needs_utc_datetime(self) -> bool:
        return any(f.type == "datetime" for f in self.fields)

    @property
    def needs_jsonb(self) -> bool:
        return any(f.type == "json" for f in self.fields)

    @property
    def needs_pydantic_field(self) -> bool:
        return any(
            f.create_declaration().find("Field(") != -1
            or f.update_declaration().find("Field(") != -1
            for f in self.fields
        )

    @property
    def needs_aware_datetime(self) -> bool:
        return any(f.type == "datetime" for f in self.fields)

    def sample_items(
        self, index: int, *, only: Sequence[str] | None = None, mapping: bool = False
    ) -> list[str]:
        """``["a=1", ...]`` for a call, or ``['"a": 1', ...]`` for a dict literal."""
        chosen = [f for f in self.fields if only is None or f.name in only]
        if mapping:
            return [f'"{f.name}": {f.sample(index)}' for f in chosen]
        return [f"{f.name}={f.sample(index)}" for f in chosen]

    def sample_values(self, index: int, *, only: Sequence[str] | None = None) -> str:
        """``a=1, b="x"`` for a constructor call; ``only`` limits the fields."""
        chosen = [f for f in self.fields if only is None or f.name in only]
        return ", ".join(f"{f.name}={f.sample(index)}" for f in chosen)

    def sample_dict(self, index: int, *, only: Sequence[str] | None = None) -> str:
        """``{"a": 1, "b": "x"}`` for the layouts whose use cases take a mapping."""
        chosen = [f for f in self.fields if only is None or f.name in only]
        return "{" + ", ".join(f'"{f.name}": {f.sample(index)}' for f in chosen) + "}"

    def read_arguments(self, source: str) -> list[str]:
        """``["a=item.a", ...]``: every field copied off one object, as keywords."""
        return [f"{f.name}={source}.{f.name}" for f in self.fields]

    def as_context(self) -> dict[str, Any]:
        return {
            "fields": list(self.fields),
            "uniques": list(self.uniques),
            "example": self.example,
            "bare": not self.fields,
            "spec": self,
            "import_lines": import_lines,
            "tuple_source": tuple_source,
            "fit": fit,
            **self.extra,
        }


def _quoted(text: str) -> str:
    """A string literal in the quotes ruff format writes, so the output is formatted."""
    return json.dumps(text, ensure_ascii=False)


def _isort_key(name: str) -> tuple[int, str]:
    """Ruff's default order inside one ``from x import a, b``: constants, classes, the rest."""
    if name.isupper() and len(name) > 1:
        return (0, name.lower())
    if name[:1].isupper():
        return (1, name.lower())
    return (2, name.lower())


def tuple_source(names: Sequence[str], *, indent: str = "") -> str:
    """A tuple of string literals as ruff format writes it.

    One element stays on one line; more are one per line with a trailing comma,
    which ruff keeps exploded however short they are. A generated tuple that
    happens to fit on a line would otherwise be collapsed by the formatter and
    fail `ruff format --check` on the next module that is one name longer.
    """
    if len(names) == 1:
        return f"({_quoted(names[0])},)"
    inner = "".join(f"{indent}    {_quoted(name)},\n" for name in names)
    return f"(\n{inner}{indent})"


LINE_LENGTH = 100


def fit(head: str, items: Sequence[str], tail: str, *, indent: int = 0) -> str:
    """A call or a signature, on one line if it fits and one item per line if not.

    Written the way ruff format settles it, so a generated file is already
    formatted: a line that fits stays whole, and one that does not is exploded
    with a trailing comma, which the formatter then leaves alone. The first
    line carries no indentation -- the template already wrote it -- and the
    rest carry ``indent`` spaces.
    """
    one_line = f"{head}{', '.join(items)}{tail}"
    if indent + len(one_line) <= LINE_LENGTH or not items:
        return one_line
    pad = " " * indent
    body = "".join(f"{pad}    {item},\n" for item in items)
    return f"{head}\n{body}{pad}{tail}"


def import_lines(names: Iterable[str]) -> list[str]:
    """``from module import a, b`` lines from dotted names, in isort's order.

    The templates build every import that depends on the fields through this,
    so a module with a decimal and a date field gets ``from datetime import
    date`` merged into whatever else the file imports from the same place --
    the form ``ruff check`` accepts -- instead of a second line beside it.
    """
    grouped: dict[str, set[str]] = {}
    for dotted in names:
        module, _, name = dotted.rpartition(".")
        grouped.setdefault(module, set()).add(name)
    return [
        f"from {module} import {', '.join(sorted(found, key=_isort_key))}"
        for module, found in sorted(grouped.items())
    ]


def split_fields(text: str) -> list[str]:
    """Split on commas that are not inside parentheses: ``decimal(10,2)`` is one."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def parse_field(text: str) -> FieldSpec:
    """One ``name:type[?][=default]`` into a FieldSpec, or FieldSpecError."""
    name, colon, rest = text.strip().partition(":")
    type_text, equals, raw = rest.partition("=")
    # The default is the one place spaces are meaningful ("=two words").
    match = _TYPE.fullmatch(re.sub(r"\s+", "", type_text)) if colon else None
    if match is None:
        raise FieldSpecError(
            f"cannot read field {text.strip()!r}: write it as name:type, e.g. mes:str(7), "
            f"leida:bool=false or nota:text?"
        )
    name = name.strip()
    kind = match.group("type")
    args = match.group("args")
    nullable = match.group("nullable") is not None
    raw_default = raw if equals else None

    if not _NAME.match(name) or keyword.iskeyword(name):
        raise FieldSpecError(
            f"field name {name!r} is not a snake_case Python identifier: "
            f"use lowercase letters, digits and underscores, starting with a letter"
        )
    if name in RESERVED:
        raise FieldSpecError(
            f"field {name!r} is already on every generated entity (id, tenant_id and the "
            f"timestamps come from the mixins): drop it from --fields, or rename it"
        )
    if kind not in TYPES:
        raise FieldSpecError(
            f"field {name!r} has unknown type {kind!r}. Choose from: {', '.join(TYPES)}"
        )

    length = precision = scale = None
    if kind == "str":
        length = _parse_length(name, args)
    elif kind == "decimal":
        precision, scale = _parse_precision(name, args)
    elif args is not None:
        raise FieldSpecError(
            f"field {name!r}: {kind} takes no arguments, but got ({args}). "
            f"Only str(N) and decimal(P,S) have them"
        )

    spec = FieldSpec(
        name=name,
        type=kind,
        nullable=nullable,
        length=length,
        precision=precision,
        scale=scale,
    )
    if raw_default is None:
        return spec
    return FieldSpec(**{**spec.__dict__, "default": _parse_default(spec, raw_default)})


def _parse_length(name: str, args: str | None) -> int:
    if args is None:
        return DEFAULT_STR_LENGTH
    try:
        length = int(args)
    except ValueError:
        length = 0
    if length < 1:
        raise FieldSpecError(
            f"field {name!r}: str({args}) needs a positive length, e.g. str(7). "
            f"Use text for a string with no limit"
        )
    return length


def _parse_precision(name: str, args: str | None) -> tuple[int, int]:
    parts = [part.strip() for part in (args or "").split(",")]
    try:
        precision, scale = (int(parts[0]), int(parts[1])) if len(parts) == 2 else (0, -1)
    except ValueError:
        precision, scale = 0, -1
    if not (1 <= precision <= 1000 and 0 <= scale <= precision):
        raise FieldSpecError(
            f"field {name!r}: decimal needs a precision and a scale, e.g. decimal(10,2) -- "
            f"ten digits, two after the point. For amounts of money, money stores exact "
            f"minor units instead"
        )
    return precision, scale


def _parse_default(spec: FieldSpec, raw: str) -> str:
    """The default as a Python literal, checked against the field's type."""
    value = raw.strip()
    name = spec.name

    def wrong(expected: str) -> FieldSpecError:
        return FieldSpecError(f"field {name!r}: default {raw!r} is not {expected}")

    if spec.type in ("int", "bigint", "money"):
        try:
            return str(int(value))
        except ValueError:
            raise wrong(
                "an integer" + (" (minor units: 1050 is 10.50)" if spec.type == "money" else "")
            ) from None
    if spec.type == "bool":
        lowered = value.lower()
        if lowered in ("true", "false"):
            return "True" if lowered == "true" else "False"
        raise wrong("true or false")
    if spec.type == "float":
        try:
            return repr(float(value))
        except ValueError:
            raise wrong("a number") from None
    if spec.type == "decimal":
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise wrong("a decimal number") from None
        quantum = Decimal(1).scaleb(-(spec.scale or 0))
        return f'Decimal("{number.quantize(quantum)}")'
    if spec.type in ("str", "text"):
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if spec.length is not None and len(value) > spec.length:
            raise wrong(f"at most {spec.length} characters")
        return _quoted(value)
    raise FieldSpecError(
        f"field {name!r}: a {spec.type} takes no default. Make it nullable ({name}:{spec.type}?) "
        f"and set it in the service, where the request's tenant and zone are known"
    )


def parse_fields(text: str) -> tuple[FieldSpec, ...]:
    """The whole ``--fields`` value."""
    specs = tuple(parse_field(part) for part in split_fields(text))
    if not specs:
        raise FieldSpecError('--fields is empty: pass at least one, e.g. --fields "name:str(120)"')
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise FieldSpecError(f"field {spec.name!r} is declared twice in --fields")
        seen.add(spec.name)
    return specs


def parse_unique(
    values: Sequence[str], fields: Sequence[FieldSpec], table: str
) -> tuple[UniqueSpec, ...]:
    """Each ``--unique "a,b"`` into a UniqueSpec over fields that exist."""
    by_name = {f.name: f for f in fields}
    uniques: list[UniqueSpec] = []
    for value in values:
        names = [part.strip() for part in value.split(",") if part.strip()]
        if not names:
            raise FieldSpecError(
                '--unique is empty: name the fields, e.g. --unique "cartera_id,mes"'
            )
        missing = [name for name in names if name not in by_name]
        if missing:
            known = ", ".join(by_name) or "none -- --unique needs --fields"
            raise FieldSpecError(
                f"--unique names {', '.join(missing)}, which --fields does not declare. "
                f"Declared: {known}"
            )
        if len(set(names)) != len(names):
            raise FieldSpecError(f"--unique {value!r} names a field twice")
        unsupported = [name for name in names if by_name[name].type == "json"]
        if unsupported:
            raise FieldSpecError(
                f"--unique cannot include json field(s) {', '.join(unsupported)}: "
                f"PostgreSQL has no equality for json. Use a str field as the key"
            )
        uniques.append(UniqueSpec(tuple(by_name[name] for name in names), table))
    return tuple(uniques)


def module_fields(
    fields: str | None,
    unique: Sequence[str],
    *,
    bare: bool,
    table: str,
) -> ModuleFields:
    """What the three ways of calling `jfast new module` mean.

    No flags: the example module. ``--fields``: exactly those. ``--bare``: the
    structure with no fields at all -- for a module whose first field is not
    known yet, or one that will hold nothing but relations.
    """
    if bare and fields:
        raise FieldSpecError("--bare and --fields contradict each other: pass one or the other")
    if bare:
        if unique:
            raise FieldSpecError("--unique needs fields to be unique over; --bare has none")
        return ModuleFields()
    if fields is None:
        if unique:
            raise FieldSpecError(
                '--unique needs --fields, e.g. --fields "code:str(20)" --unique code'
            )
        example = parse_fields(EXAMPLE_FIELDS)
        return ModuleFields(example, parse_unique(EXAMPLE_UNIQUE, example, table), example=True)
    specs = parse_fields(fields)
    return ModuleFields(specs, parse_unique(unique, specs, table))
