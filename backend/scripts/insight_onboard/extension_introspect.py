"""
Registry-extension introspection (AST-based).

Some OpenG2P registries ship a Python "extension" package that declares
SQLAlchemy model classes for the analytical entities — e.g.
`openg2p_registry_nsr_extension` defines `G2PRegisterIndividual` (with
`__tablename__ = "g2p_register_individuals"`) and inherits from base
mixins like `G2PRegister`, `G2PPerson`, `G2PGeo`.

When pointed at the extension (and optionally the registry-core base),
this module extracts three things that pure DB introspection can't see:

  1. **Authoritative analytical-table list** — only tables declared by a
     non-history/non-intake model class in the extension. This catches
     cases like `g2p_register_verifications` (a core workflow table that
     looks like a register table to DB introspection but has no
     extension model — so we know it's workflow).

  2. **Mixin attribution per column** — walking the MRO tells us
     `full_name` was declared in `G2PPerson` (identity mixin → PII),
     while `disability_status` came from the NSR domain mixin. The
     mixin label flows back as a `prior` to the column classifier.

  3. **Enum values from `Mapped[…Enum]` annotations** — these are pure
     Python and don't reach the DB. The catalog needs them so the LLM
     can filter on enum-typed columns with the exact declared values
     (`gender = 'F'` rather than `'FEMALE'`).

Why AST-based rather than `importlib.import_module()`:
  - Extension packages typically pull in the entire OpenG2P platform
    (FastAPI service base, iam_core, Minio client, …) which is too
    heavy a dependency tree just to read class metadata.
  - AST parsing has zero runtime side-effects, works without installing
    the extension's deps, and is robust against version drift in the
    surrounding service code.

We rely on a small set of structural assumptions only:
  - Model classes use SQLAlchemy 2.x declarative style:
    `name: Mapped[Type] = mapped_column(...)`.
  - Enum classes are plain Python `enum.Enum` (or `str, enum.Enum`)
    subclasses with `MEMBER = "value"` literal members.
  - `__tablename__ = "..."` is assigned as a class-level constant.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

_logger = logging.getLogger("insight_onboard.extension")


# -----------------------------------------------------------------------------
# Parsed metadata types
# -----------------------------------------------------------------------------
@dataclass
class ExtColumn:
    name: str
    declaring_class: str            # which mixin/base owns this column
    annotation: str                  # raw e.g. "Mapped[GenderEnum]"
    enum_class_name: Optional[str] = None
    enum_values: Optional[List[str]] = None

    @property
    def is_enum(self) -> bool:
        return self.enum_values is not None


@dataclass
class ExtModel:
    class_name: str
    tablename: str
    mixins: List[str]                 # base class names in declaration order
    columns: Dict[str, ExtColumn] = field(default_factory=dict)
    kind: str = "register"             # "register" | "history" | "intake_form"
    # Whether the model inherits from `G2PRegister` (the entity base in
    # openg2p_registry_core). Only those models hold real registrant
    # entity data; the other `g2p_register_*` tables (change_requests,
    # verifications, queues, etc.) are workflow/audit/operational.
    is_analytical_entity: bool = False

    @property
    def is_register(self) -> bool:
        return self.kind == "register"


@dataclass
class ExtMetadata:
    models: Dict[str, ExtModel] = field(default_factory=dict)
    enum_catalog: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def register_table_names(self) -> Set[str]:
        return {m.tablename for m in self.models.values() if m.is_register}

    @property
    def analytical_entity_tables(self) -> Set[str]:
        """The authoritative analytical-table set: register-kind models that
        inherit from `G2PRegister`. Excludes change-requests, verifications,
        queues, sections, UI/schema metadata, ingestion pipelines, etc."""
        return {
            m.tablename for m in self.models.values()
            if m.is_register and m.is_analytical_entity
        }

    def model_for_table(self, tablename: str) -> Optional[ExtModel]:
        return self.models.get(tablename)


# -----------------------------------------------------------------------------
# Loader
# -----------------------------------------------------------------------------
def load_extension_metadata(
    paths: Iterable[Path],
) -> ExtMetadata:
    """Parse all `.py` files under the given paths and assemble metadata.

    `paths` typically contains the extension package's `register_domain/models/`
    directory and the registry-core's `models/` directory. The order doesn't
    matter — class graph is built from the union."""
    # Walk every .py file, parse and collect:
    #   - class definitions (name, base names, tablename, column AST nodes)
    #   - enum definitions (string-valued enum members)
    class_records: Dict[str, _ClassRecord] = {}
    enum_catalog: Dict[str, List[str]] = {}

    files_seen = 0
    for root in paths:
        root = Path(root)
        if not root.exists():
            _logger.warning("extension path %s does not exist; skipping", root)
            continue
        for py_file in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError as exc:
                _logger.warning("could not parse %s: %s", py_file, exc)
                continue
            files_seen += 1
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    if _is_enum_class(node):
                        values = _extract_enum_values(node)
                        if values:
                            enum_catalog[node.name] = values
                    else:
                        rec = _classdef_to_record(node)
                        if rec is not None:
                            # If the same class name appears twice, prefer the
                            # one with more column info (or a tablename).
                            existing = class_records.get(rec.name)
                            if existing is None or _record_score(rec) > _record_score(existing):
                                class_records[rec.name] = rec

    _logger.info(
        "extension: parsed %d files, %d classes, %d enums",
        files_seen, len(class_records), len(enum_catalog),
    )

    # Resolve full column sets via MRO walks.
    metadata = ExtMetadata(enum_catalog=enum_catalog)
    for name, rec in class_records.items():
        if not rec.tablename:
            continue   # mixin / abstract base; not a table
        columns = _resolve_columns(rec, class_records, enum_catalog)
        kind = _classify_table_kind(rec, class_records)
        is_entity = _inherits_class(rec, class_records, target="G2PRegister")
        metadata.models[rec.tablename] = ExtModel(
            class_name=name,
            tablename=rec.tablename,
            mixins=rec.bases,
            columns=columns,
            kind=kind,
            is_analytical_entity=is_entity,
        )

    return metadata


def _inherits_class(
    rec: _ClassRecord,
    all_records: Dict[str, _ClassRecord],
    target: str,
) -> bool:
    """Recursive ancestor check: does `rec` inherit (directly or transitively)
    from a class named `target`? Used to distinguish `G2PRegisterIndividual`
    (extends `G2PRegister` — analytical entity) from `G2PRegisterVerification`
    (does NOT extend `G2PRegister` — workflow/audit)."""
    seen: Set[str] = set()

    def walk(name: str, depth: int = 0) -> bool:
        if name in seen or depth > 30:
            return False
        seen.add(name)
        if name == target:
            return True
        sub = all_records.get(name)
        if sub is None:
            return False
        return any(walk(b, depth + 1) for b in sub.bases)

    return any(walk(b) for b in rec.bases)


# -----------------------------------------------------------------------------
# Internal AST helpers
# -----------------------------------------------------------------------------
@dataclass
class _ClassRecord:
    name: str
    bases: List[str]
    tablename: Optional[str]
    column_nodes: Dict[str, ast.AnnAssign]    # column name → annotation AST


def _classdef_to_record(node: ast.ClassDef) -> Optional[_ClassRecord]:
    bases = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            bases.append(b.id)
        elif isinstance(b, ast.Attribute):
            # Handle `module.Class` bases by taking the last segment.
            bases.append(b.attr)
    tablename: Optional[str] = None
    columns: Dict[str, ast.AnnAssign] = {}
    for item in node.body:
        # __tablename__ = "..."
        if isinstance(item, ast.Assign):
            for tgt in item.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__tablename__":
                    if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                        tablename = item.value.value
        # name: Mapped[Type] = mapped_column(...)
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if _is_mapped_annotation(item.annotation):
                columns[item.target.id] = item
    if not columns and tablename is None:
        # Not a SQLAlchemy declarative class at all — skip.
        return None
    return _ClassRecord(name=node.name, bases=bases, tablename=tablename, column_nodes=columns)


def _record_score(rec: _ClassRecord) -> int:
    """Heuristic: prefer the record with more info when the same class name
    is seen in multiple files (rare but happens with re-exports)."""
    return (1 if rec.tablename else 0) * 100 + len(rec.column_nodes)


def _is_mapped_annotation(ann: ast.expr) -> bool:
    """True for `Mapped[...]` style annotations."""
    if isinstance(ann, ast.Subscript):
        value = ann.value
        if isinstance(value, ast.Name) and value.id == "Mapped":
            return True
        if isinstance(value, ast.Attribute) and value.attr == "Mapped":
            return True
    return False


def _unwrap_mapped(ann: ast.expr) -> Optional[str]:
    """Given a `Mapped[X]` annotation, return the inner class name (best-effort)."""
    if not isinstance(ann, ast.Subscript):
        return None
    inner = ann.slice
    if isinstance(inner, ast.Name):
        return inner.id
    if isinstance(inner, ast.Attribute):
        return inner.attr
    # `Mapped[Optional[X]]` etc — strip one more layer
    if isinstance(inner, ast.Subscript):
        sub = inner.slice
        if isinstance(sub, ast.Name):
            return sub.id
        if isinstance(sub, ast.Tuple):
            for elt in sub.elts:
                if isinstance(elt, ast.Name):
                    return elt.id
    return None


def _resolve_columns(
    rec: _ClassRecord,
    all_records: Dict[str, _ClassRecord],
    enum_catalog: Dict[str, List[str]],
) -> Dict[str, ExtColumn]:
    """Walk the linearised MRO of `rec` (depth-first over `bases`) and collect
    every column declared in any ancestor. The class closest to the leaf
    wins on name collisions — same semantics SQLAlchemy uses for declarative
    inheritance."""
    seen: Set[str] = set()
    out: Dict[str, ExtColumn] = {}

    def visit(class_name: str, depth: int = 0) -> None:
        if class_name in seen or depth > 30:  # cycle / depth guard
            return
        seen.add(class_name)
        sub = all_records.get(class_name)
        if sub is None:
            return
        for col_name, ann_node in sub.column_nodes.items():
            if col_name in out:
                continue  # leaf class already declared it; respect override
            annotation_src = ast.unparse(ann_node.annotation)
            inner_type = _unwrap_mapped(ann_node.annotation)
            enum_values = enum_catalog.get(inner_type) if inner_type else None
            out[col_name] = ExtColumn(
                name=col_name,
                declaring_class=class_name,
                annotation=annotation_src,
                enum_class_name=inner_type if enum_values else None,
                enum_values=enum_values,
            )
        for base in sub.bases:
            visit(base, depth + 1)

    # Start from the leaf class itself, then bases in declaration order.
    visit(rec.name)
    return out


def _classify_table_kind(rec: _ClassRecord, all_records: Dict[str, _ClassRecord]) -> str:
    """Tag whether this table is a register entity, a history mirror, or an
    intake-form submission. Driven by tablename pattern with class-name
    fallback — both signals agree in the OpenG2P registries we've seen."""
    tname = (rec.tablename or "").lower()
    cname = rec.name.lower()
    if "register_history_" in tname or "history" in cname:
        return "history"
    if tname.startswith("g2p_intake_form_") or "intakeform" in cname:
        return "intake_form"
    return "register"


# -----------------------------------------------------------------------------
# Enum parsing
# -----------------------------------------------------------------------------
def _is_enum_class(node: ast.ClassDef) -> bool:
    """True if the class extends `enum.Enum` (directly or via `str, enum.Enum`)."""
    for b in node.bases:
        base_text = ast.unparse(b)
        if re.search(r"\b[Ee]num\b", base_text):
            # Filter out SQLAlchemy `Enum` column-type usage — those wouldn't
            # appear as bases anyway. The `enum` module form is what we want.
            return True
    return False


def _extract_enum_values(node: ast.ClassDef) -> List[str]:
    """Pull literal string values from `MEMBER = "value"` assignments."""
    values: List[str] = []
    for item in node.body:
        if isinstance(item, ast.Assign):
            for tgt in item.targets:
                if isinstance(tgt, ast.Name) and isinstance(item.value, ast.Constant):
                    v = item.value.value
                    if isinstance(v, (str, int, float, bool)):
                        values.append(str(v))
    return values
