"""Schema-service fail-secure tests."""

from openg2p_insight.services.schema_service import (
    Catalog,
    ColumnSpec,
    SchemaService,
    TableSpec,
)


def _svc(catalog: Catalog) -> SchemaService:
    svc = SchemaService.__new__(SchemaService)
    svc.catalog = catalog
    return svc


def test_empty_safe_views_returns_empty():
    """If safe_views is empty, the allow-list must be empty too — never fall
    back to 'tables without a PII column'."""
    catalog = Catalog(
        tables=[
            TableSpec(
                name="mv_summary",
                columns=[ColumnSpec(name="region_code", type="string", pii="safe")],
            ),
        ],
        safe_views=[],
    )
    assert _svc(catalog).catalog.safe_table_names() == []


def test_explicit_safe_views_only():
    catalog = Catalog(
        tables=[
            TableSpec(
                name="g2p_register_individuals",
                columns=[ColumnSpec(name="full_name", type="string", pii="pii")],
            ),
            TableSpec(
                name="mv_individuals_summary",
                columns=[ColumnSpec(name="region_code", type="string", pii="safe")],
            ),
        ],
        safe_views=["mv_individuals_summary", "v_individuals_safe"],
    )
    names = _svc(catalog).catalog.safe_table_names()
    assert names == ["mv_individuals_summary", "v_individuals_safe"]
    # The raw PII-bearing table is NEVER allow-listed even though it appears in `tables`.
    assert "g2p_register_individuals" not in names
