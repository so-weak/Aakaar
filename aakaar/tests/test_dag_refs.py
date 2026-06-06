"""Tests for the ${alias.path} ref parser."""

from __future__ import annotations

import pytest

from aakaar.shared.dag.refs import RefError, is_ref, parse_ref, parse_refs


def test_is_ref() -> None:
    assert is_ref("${a}")
    assert is_ref("${a.b}")
    assert is_ref("${alias.field.nested}")
    assert not is_ref("hello ${a.b} world")  # embedding not supported in v1
    assert not is_ref("${1bad}")
    assert not is_ref("$a.b")
    assert not is_ref(123)


def test_parse_ref_paths() -> None:
    r = parse_ref("${alias.field.sub}")
    assert r.alias == "alias"
    assert r.path == ("field", "sub")
    assert r.head == "field"

    plain = parse_ref("${alias}")
    assert plain.alias == "alias"
    assert plain.path == ()
    assert plain.head == ""


def test_parse_ref_rejects_garbage() -> None:
    with pytest.raises(RefError):
        parse_ref("nope")


def test_parse_refs_walks_nested_inputs() -> None:
    inputs = {
        "url": "https://example.com",
        "session": "${n1.session}",
        "headers": {"x-trace": "${n0.id}"},
        "tags": ["literal", "${n2.value}"],
    }
    found = parse_refs(inputs)
    paths = sorted(p for p, _ in found)
    aliases = sorted(r.alias for _, r in found)
    assert paths == [("headers", "x-trace"), ("session",), ("tags", 1)]
    assert aliases == ["n0", "n1", "n2"]


def test_parse_refs_empty() -> None:
    assert parse_refs({}) == []
    assert parse_refs("plain string") == []
    assert parse_refs(42) == []
