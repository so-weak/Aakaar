"""Runtime reference resolution.

The DAG validator already verifies that every `${alias.path}` ref points to
an upstream node and (when registry is supplied) to a declared output field.
At runtime, the interpreter walks the inputs again and substitutes refs
with concrete values from the run env.

The env is a dict[node_id, outputs_dict]. Aliases (set via `outputs_as`)
are resolved through `alias_to_id` produced by the executor's index.
"""

from __future__ import annotations

from typing import Any

from aakaar.shared.dag.refs import is_ref, parse_ref


class UnresolvedRef(KeyError):
    """A ref pointed at an alias not in the env, or a path that bottomed out."""


def resolve_inputs(
    inputs: Any,
    *,
    env: dict[str, dict[str, Any]],
    alias_to_id: dict[str, str],
) -> Any:
    """Recursively walk `inputs`, replacing refs with concrete values.

    Returns a new structure; doesn't mutate the input.
    """
    if isinstance(inputs, str) and is_ref(inputs):
        ref = parse_ref(inputs)
        node_id = alias_to_id.get(ref.alias)
        if node_id is None:
            raise UnresolvedRef(f"alias {ref.alias!r} not in env")
        outputs = env.get(node_id)
        if outputs is None:
            raise UnresolvedRef(f"no outputs recorded for node {node_id!r}")
        if not ref.path:
            return outputs
        cur: Any = outputs
        for seg in ref.path:
            if isinstance(cur, dict):
                if seg not in cur:
                    raise UnresolvedRef(
                        f"path {'.'.join(ref.path)} bottomed out at {seg!r} for "
                        f"alias {ref.alias!r}"
                    )
                cur = cur[seg]
            else:
                raise UnresolvedRef(
                    f"cannot traverse non-dict at {seg!r} for alias {ref.alias!r}"
                )
        return cur
    if isinstance(inputs, dict):
        return {k: resolve_inputs(v, env=env, alias_to_id=alias_to_id) for k, v in inputs.items()}
    if isinstance(inputs, list):
        return [resolve_inputs(v, env=env, alias_to_id=alias_to_id) for v in inputs]
    return inputs
