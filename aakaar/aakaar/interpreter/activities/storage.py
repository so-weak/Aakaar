"""storage.* — copy bytes between managed storage and the local filesystem.

`storage.put` is the on-ramp: a worker stages a downloaded file on the
local filesystem and copies it into managed storage. `storage.get` is the
reverse, used when an activity needs to hand a file path to an external
tool.

Local-filesystem URIs are `file://`-prefixed in this driver. Workers
running on a single host can share the same filesystem; multi-host
deployments will need an actual S3-style backend (drop-in via Protocol).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.storage.object_store import make_uri, parse_uri

_FILE_SCHEME = "file://"


async def storage_put(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    key = inputs["key"]
    source_file_uri = inputs["source_file_uri"]
    if not source_file_uri.startswith(_FILE_SCHEME):
        raise ValueError(f"source_file_uri must be a file:// URI, got {source_file_uri!r}")
    source = Path(source_file_uri.removeprefix(_FILE_SCHEME))
    obj = ctx.object_store.put_file(str(ctx.tenant_id), key, source)
    return {"uri": obj.uri}


async def storage_get(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    uri = inputs["uri"]
    tenant_id, _ = parse_uri(uri)
    if tenant_id != str(ctx.tenant_id):
        raise PermissionError("URI tenant does not match run tenant")
    data = ctx.object_store.get(uri)
    # Stage the bytes to a temp file and return its file:// URI. delete=False
    # so the path outlives this handler; the caller owns its lifecycle.
    with tempfile.NamedTemporaryFile(delete=False) as fd:
        fd.write(data)
        tmp_name = fd.name
    return {"file_uri": f"{_FILE_SCHEME}{tmp_name}"}


# Helper used when wiring downloads (PR 5) — kept here so the URI
# construction stays consistent with the other storage primitives.
def make_storage_uri(tenant_id: str, key: str) -> str:
    return make_uri(tenant_id, key)


# Local file uri helper — symmetric with the parsing above. Used elsewhere
# to construct local URIs from temp files.
def make_local_file_uri(path: Path | str) -> str:
    return f"{_FILE_SCHEME}{Path(path).resolve()}"


def parse_local_file_uri(uri: str) -> Path:
    if not uri.startswith(_FILE_SCHEME):
        raise ValueError(f"not a file:// uri: {uri!r}")
    return Path(uri.removeprefix(_FILE_SCHEME))


def register_into(reg: ActivityRegistry) -> None:
    reg.register("storage.put", storage_put)
    reg.register("storage.get", storage_get)


# Quiet a lint about the unused shutil import — kept for future use by
# multipart upload activities.
_ = shutil
