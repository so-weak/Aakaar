"""Tests for the LocalFs object store."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aakar.storage import LocalFsObjectStore, ObjectNotFound, ObjectStoreError
from aakar.storage.object_store import make_uri, parse_uri


def test_put_get_round_trip(tmp_path: Path) -> None:
    store = LocalFsObjectStore(tmp_path)
    obj = store.put("t1", "reports/may.pdf", b"hello world")
    assert obj.tenant_id == "t1"
    assert obj.key == "reports/may.pdf"
    assert obj.size == len(b"hello world")
    assert obj.sha256 == hashlib.sha256(b"hello world").hexdigest()

    assert store.get(obj.uri) == b"hello world"
    again = store.stat(obj.uri)
    assert again.size == obj.size
    assert again.sha256 == obj.sha256


def test_put_file_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"abc123")
    store = LocalFsObjectStore(tmp_path / "store")
    obj = store.put_file("t1", "a/b.bin", src)
    assert store.get(obj.uri) == b"abc123"


def test_uri_parsing() -> None:
    u = make_uri("t1", "a/b/c.txt")
    assert u == "aakar://t/t1/a/b/c.txt"
    assert parse_uri(u) == ("t1", "a/b/c.txt")

    with pytest.raises(ValueError):
        parse_uri("s3://bucket/key")
    with pytest.raises(ValueError):
        parse_uri("aakar://t/t1")  # missing key


def test_traversal_blocked(tmp_path: Path) -> None:
    store = LocalFsObjectStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("t1", "../escape", b"x")
    with pytest.raises(ValueError):
        store.put("t1", "/etc/passwd", b"x")


def test_tenant_isolation(tmp_path: Path) -> None:
    store = LocalFsObjectStore(tmp_path)
    obj_a = store.put("ta", "shared/key", b"A")
    obj_b = store.put("tb", "shared/key", b"B")
    assert store.get(obj_a.uri) == b"A"
    assert store.get(obj_b.uri) == b"B"


def test_missing_uri_raises(tmp_path: Path) -> None:
    store = LocalFsObjectStore(tmp_path)
    with pytest.raises(ObjectNotFound):
        store.get(make_uri("t1", "nope"))


def test_list(tmp_path: Path) -> None:
    store = LocalFsObjectStore(tmp_path)
    store.put("t1", "a/x.txt", b"x")
    store.put("t1", "a/y.txt", b"y")
    store.put("t1", "b/z.txt", b"z")
    out = store.list("t1", prefix="a/")
    assert {o.key for o in out} == {"a/x.txt", "a/y.txt"}


def test_invalid_tenant_id(tmp_path: Path) -> None:
    store = LocalFsObjectStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("../bad", "k", b"")
    with pytest.raises(ValueError):
        store.put("t/with/slash", "k", b"")


def test_put_file_missing_source(tmp_path: Path) -> None:
    store = LocalFsObjectStore(tmp_path)
    with pytest.raises(ObjectStoreError):
        store.put_file("t1", "k", tmp_path / "does-not-exist")
