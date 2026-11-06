"""The single write path for coach data (KTD8).

MCP tools, jobs and tests write through this package. Ingest keeps its own
raw-table writes in coach/ingest/, and coach/db/migrate.py owns the schema.
tests/test_store_boundary.py enforces that no other module writes SQL.
"""
