# Aakar

Multi-tenant NL → DAG workflow platform.

The LLM emits a DAG; the runtime executes it. Capabilities are registry-backed and
tenant-granted. Credentials live in the registry/vault — never in chat, never in the DAG.

## Layout

```
aakar/
  shared/
    dag/         DAG schema, ref resolution, validator
    registry/    capability/action/control definitions + registry
    planner/     planner response shapes (dag | clarify | missing)
  storage/       ObjectStorage (LocalFs) + VectorStore (Faiss)
  db/            SQLAlchemy models + migrations
  tests/
```

This is the v0 foundation: pure types, validator, storage drivers, schema. No LLM,
no Temporal, no API yet — those land in subsequent PRs.

## Running tests

```
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```
