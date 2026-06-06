"""Remote-only capability CONTRACTS.

These modules declare a capability's ref + input/output schema + tags but ship
NO local handler (``remote_only = True``). The schema lives here so the planner,
DAG validator, and placement resolver can reason about the capability; the
implementation runs on a remote agent. A "gui" tag means the capability needs an
interactive desktop session, which the placement resolver enforces.
"""
