"""Pydantic-model detection and surface predicates for the PG linter."""

from __future__ import annotations

import ast

_BASEMODEL_ROOTS = frozenset({"BaseModel", "RootModel"})
"""Seed names recognised as Pydantic model roots."""

_PYDANTIC_DECORATORS = frozenset(
    {
        "field_validator",
        "model_validator",
        "field_serializer",
        "model_serializer",
        "computed_field",
    }
)
"""Pydantic V2 decorator names whose presence marks a class as using surface."""


def _base_name(base: ast.expr) -> str | None:
    """Return the simple name of a class base, unwrapping subscripts/attributes.

    Args:
        base: A class base expression node.

    Returns:
        The bare identifier (``BaseModel``, ``RootModel``, a local subclass
        name, or an ``obj.attr`` attr), or ``None`` when *base* is not a
        name/attribute/subscript shape.
    """
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return None


def resolves_to_name(node: ast.expr, names: frozenset[str]) -> bool:
    """Return True when *node* resolves to a bare identifier in *names*.

    Unwraps ``ast.Name`` (``id``), ``ast.Attribute`` (``attr``),
    ``ast.Subscript`` (recurses into ``value``), and ``ast.Call`` (recurses
    into ``func``). For ``Attribute``/``Subscript``/``Call``, the wrapped
    expression is also searched so a nested target (e.g.
    ``Annotated[TypeAdapter[int], ...]``) is recognised.

    Args:
        node: An AST expression node.
        names: Target bare-identifier set.

    Returns:
        True when any reachable identifier in *names* appears in *node*.
    """
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr in names or resolves_to_name(node.value, names)
    if isinstance(node, ast.Subscript):
        return resolves_to_name(node.value, names)
    if isinstance(node, ast.Call):
        return resolves_to_name(node.func, names)
    return False


def collect_basemodel_names(tree: ast.Module) -> frozenset[str]:
    """Return class names transitively subclassing BaseModel/RootModel.

    Args:
        tree: A parsed module.

    Returns:
        A frozenset whose members are every class name in *tree* that
        (transitively) has ``BaseModel`` or ``RootModel`` among its bases,
        plus the two root names themselves.
    """
    known: set[str] = set(_BASEMODEL_ROOTS)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name not in known:
                for base in node.bases:
                    if _base_name(base) in known:
                        known.add(node.name)
                        changed = True
                        break
    return frozenset(known)


def is_basemodel_class(node: ast.ClassDef, known: frozenset[str]) -> bool:
    """Return True when *node* subclasses a known BaseModel/RootModel name.

    Args:
        node: A class definition.
        known: The resolved set of BaseModel/RootModel subclass names (from
            ``collect_basemodel_names``).

    Returns:
        True when any base of *node* resolves to a name in *known*.
    """
    return any(_base_name(base) in known for base in node.bases)


def is_rootmodel_class(node: ast.ClassDef) -> bool:
    """Return True when *node* directly subclasses RootModel (bare or subscripted).

    Args:
        node: A class definition.

    Returns:
        True when any base of *node* resolves to the name ``RootModel``.
    """
    return any(_base_name(base) == "RootModel" for base in node.bases)


def _is_pydantic_decorator(dec: ast.expr) -> bool:
    """Return True when *dec* is (or wraps) a Pydantic V2 decorator.

    Handles bare ``@field_validator``, attribute form ``@pydantic.field_validator``,
    and the called form ``@field_validator('x')`` by unwrapping ``Call.func``.
    """
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id in _PYDANTIC_DECORATORS
    if isinstance(target, ast.Attribute):
        return target.attr in _PYDANTIC_DECORATORS
    return False


def _annotation_uses_annotated(ann: ast.expr) -> bool:
    """Return True when *ann* is an ``Annotated[...]`` subscript."""
    name = ann.value if isinstance(ann, ast.Subscript) else ann
    if isinstance(name, ast.Name):
        return name.id == "Annotated"
    if isinstance(name, ast.Attribute):
        return name.attr == "Annotated"
    return False


def _calls_field(value: ast.expr | None) -> bool:
    """Return True when *value* is a ``Field(...)`` call (bare or attribute form)."""
    if isinstance(value, ast.Call):
        func = value.func
        if isinstance(func, ast.Name) and func.id == "Field":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "Field":
            return True
    return False


def uses_pydantic_surface(node: ast.ClassDef) -> bool:
    """Return True when the class body uses any Pydantic-only feature.

    The check inspects direct class-body items only (per clarification Q2:
    method internals are not re-walked). A class trips the surface when its
    body contains any of:

    - an ``AnnAssign`` or ``Assign`` to ``model_config`` or
      ``__pydantic_extra__``
    - an ``Annotated[...]`` field annotation
    - a ``Field(...)`` default value
    - a ``model_*``-prefixed method (per clarification Q1: broad prefix)
    - a method carrying a Pydantic V2 decorator

    Args:
        node: A class definition.

    Returns:
        True when any direct body item marks Pydantic surface use.
    """
    for item in node.body:
        if isinstance(item, ast.AnnAssign):
            target = item.target
            if isinstance(target, ast.Name) and target.id in {
                "model_config",
                "__pydantic_extra__",
            }:
                return True
            if item.annotation is not None and _annotation_uses_annotated(
                item.annotation
            ):
                return True
            if _calls_field(item.value):
                return True
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name) and t.id == "model_config":
                    return True
            if _calls_field(item.value):
                return True
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("model_"):
                return True
            if any(_is_pydantic_decorator(d) for d in item.decorator_list):
                return True
    return False
