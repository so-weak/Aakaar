"""Runtime ref resolution."""

from __future__ import annotations

import pytest

from aakar.interpreter.refs import UnresolvedRef, resolve_inputs


def test_simple_ref_substitution() -> None:
    env = {"n1": {"session": "s-123"}}
    inputs = {"session": "${n1.session}", "url": "https://x"}
    out = resolve_inputs(inputs, env=env, alias_to_id={"n1": "n1"})
    assert out == {"session": "s-123", "url": "https://x"}


def test_alias_resolution() -> None:
    env = {"n1": {"value": 42}}
    inputs = {"x": "${session_alias.value}"}
    out = resolve_inputs(inputs, env=env, alias_to_id={"n1": "n1", "session_alias": "n1"})
    assert out == {"x": 42}


def test_whole_object_ref() -> None:
    env = {"n1": {"a": 1, "b": 2}}
    out = resolve_inputs("${n1}", env=env, alias_to_id={"n1": "n1"})
    assert out == {"a": 1, "b": 2}


def test_nested_walk() -> None:
    env = {"src": {"value": "deep"}}
    inputs = {"headers": {"x-trace": "${src.value}"}, "list": ["a", "${src.value}"]}
    out = resolve_inputs(inputs, env=env, alias_to_id={"src": "src"})
    assert out == {"headers": {"x-trace": "deep"}, "list": ["a", "deep"]}


def test_unresolved_alias_raises() -> None:
    with pytest.raises(UnresolvedRef):
        resolve_inputs({"x": "${ghost.y}"}, env={}, alias_to_id={})


def test_path_bottoms_out() -> None:
    env = {"n1": {"a": "leaf"}}
    with pytest.raises(UnresolvedRef):
        resolve_inputs(
            {"x": "${n1.a.b}"}, env=env, alias_to_id={"n1": "n1"}
        )
