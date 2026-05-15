"""Test fakes for the cap.sftp_* capabilities.

We don't spin up a real SSH server in unit tests — the SFTP-side
surface area is small and stable, so a hand-rolled fake captures call
sequences and feeds back canned responses. Two facets:

  - `FakeSshConn` + `FakeSftpClient` substitute for asyncssh's
    `SSHClientConnection` / `SFTPClient`. The login handler patches
    `asyncssh.connect` to return a `FakeSshConn`; everything downstream
    consumes the attached `FakeSftpClient` directly.
  - `make_holder` builds an `SftpSessionHolder` already stashed in
    session_state, so list/read/write/transfer tests can drive their
    handlers without going through the login flow.

The fakes do not validate semantics asyncssh would (mode strings,
SFTP-protocol error codes); they only carry enough behavior for the
capability code paths we exercise. If a future test needs richer
behavior, add it here rather than building a parallel fake.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from aakar.capabilities._sftp_session import SftpSessionHolder, stash_key
from aakar.interpreter.activities.types import ActivityContext

# ---------- SFTP-side primitives -------------------------------------------


@dataclass
class FakeSftpAttrs:
    """Mirrors asyncssh.SFTPAttrs's fields we care about.

    `type` follows the asyncssh constants: 1=regular file, 2=dir,
    3=symlink. Setting `type=None` simulates an older asyncssh that
    only fills `permissions`; the kind detector falls back to the
    stat-style permission bits in that case.
    """

    type: int | None = 1
    size: int | None = None
    mtime: float | None = None
    permissions: int | None = None


@dataclass
class FakeSftpEntry:
    filename: str
    attrs: FakeSftpAttrs


class _FakeSftpFile:
    """Async-context-manager file handle backing FakeSftpClient.open()."""

    def __init__(self, content: bytes = b"") -> None:
        self._read_buf = content
        self._pos = 0
        self.write_buf = bytearray()

    async def read(self, n: int) -> bytes:
        chunk = self._read_buf[self._pos : self._pos + n]
        self._pos += len(chunk)
        return bytes(chunk)

    async def write(self, data: bytes) -> None:
        self.write_buf.extend(data)


class _OpenCm:
    """Async-CM wrapping a _FakeSftpFile so writes commit on close."""

    def __init__(self, client: FakeSftpClient, path: str, mode: str) -> None:
        self._client = client
        self._path = path
        self._mode = mode
        if "r" in mode:
            content = client.files.get(path)
            if content is None:
                # Match asyncssh: opening a non-existent file for read
                # surfaces an SFTPError. Use a plain OSError here — the
                # capability code catches Exception broadly.
                raise FileNotFoundError(path)
            self._file = _FakeSftpFile(content)
        else:
            self._file = _FakeSftpFile()
            client.opened_for_write.append((path, self._file))

    async def __aenter__(self) -> _FakeSftpFile:
        return self._file

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if "w" in self._mode:
            self._client.files[self._path] = bytes(self._file.write_buf)


@dataclass
class FakeSftpClient:
    """Minimum surface used by cap.sftp_list / _read / _write / _transfer.

    Test setup primes:
      - `listings`: path → list[FakeSftpEntry] for readdir()
      - `files`: path → bytes for open('rb'); also where writes land
      - `stats`: path → FakeSftpAttrs (or `Exception` to raise on stat())
      - `readdir_errors`: path → Exception to raise from readdir()
      - `rename_error`, `posix_rename_error`: optional preset failures
      - `posix_rename_available`: when False, `hasattr(client,
        'posix_rename')` is False (drops the attr entirely)
    """

    listings: dict[str, list[FakeSftpEntry]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    readdir_errors: dict[str, Exception] = field(default_factory=dict)
    rename_error: Exception | None = None
    posix_rename_error: Exception | None = None
    posix_rename_available: bool = True

    calls: list[tuple[str, Any, ...]] = field(default_factory=list)
    rename_calls: list[tuple[str, str]] = field(default_factory=list)
    posix_rename_calls: list[tuple[str, str]] = field(default_factory=list)
    makedirs_calls: list[tuple[str, bool]] = field(default_factory=list)
    opened_for_write: list[tuple[str, _FakeSftpFile]] = field(default_factory=list)
    exited: bool = False

    def __getattribute__(self, name: str) -> Any:
        # Per-instance hiding of `posix_rename` so `hasattr(client,
        # 'posix_rename')` evaluates False when the test sets
        # `posix_rename_available=False`. Mutating the class would
        # affect other tests in the same run; this scopes the toggle
        # to a single instance.
        if name == "posix_rename":
            avail = object.__getattribute__(self, "posix_rename_available")
            if not avail:
                raise AttributeError(name)
        return object.__getattribute__(self, name)

    async def readdir(self, path: str) -> list[FakeSftpEntry]:
        self.calls.append(("readdir", path))
        if path in self.readdir_errors:
            raise self.readdir_errors[path]
        if path not in self.listings:
            raise FileNotFoundError(path)
        return list(self.listings[path])

    async def stat(self, path: str) -> FakeSftpAttrs:
        self.calls.append(("stat", path))
        if path not in self.stats:
            raise FileNotFoundError(path)
        v = self.stats[path]
        if isinstance(v, Exception):
            raise v
        return v

    def open(self, path: str, mode: str) -> _OpenCm:
        self.calls.append(("open", path, mode))
        return _OpenCm(self, path, mode)

    async def makedirs(self, path: str, exist_ok: bool = False) -> None:
        self.calls.append(("makedirs", path, exist_ok))
        self.makedirs_calls.append((path, exist_ok))

    async def rename(self, src: str, dst: str) -> None:
        self.calls.append(("rename", src, dst))
        self.rename_calls.append((src, dst))
        if self.rename_error is not None:
            raise self.rename_error

    async def posix_rename(self, src: str, dst: str) -> None:
        self.calls.append(("posix_rename", src, dst))
        self.posix_rename_calls.append((src, dst))
        if self.posix_rename_error is not None:
            raise self.posix_rename_error

    def exit(self) -> None:
        self.exited = True


# ---------- SSH-side primitives --------------------------------------------


class _FakeHostKey:
    def __init__(self, fingerprint: str) -> None:
        self._fp = fingerprint

    def get_fingerprint(self, alg: str = "sha256") -> str:  # noqa: ARG002
        return self._fp


@dataclass
class FakeSshConn:
    """Stand-in for asyncssh.SSHClientConnection.

    `sftp` is what `start_sftp_client()` returns. Tests construct the
    fake SFTP client first, attach it via `conn.sftp = fake_sftp`, and
    monkeypatch `asyncssh.connect` to return this conn.
    """

    sftp: FakeSftpClient | None = None
    server_fingerprint: str | None = None
    start_sftp_error: Exception | None = None
    closed: bool = False
    waited_closed: bool = False

    def get_server_host_key(self) -> _FakeHostKey:
        return _FakeHostKey(self.server_fingerprint or "")

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited_closed = True

    async def start_sftp_client(self) -> FakeSftpClient:
        if self.start_sftp_error is not None:
            raise self.start_sftp_error
        assert self.sftp is not None, "FakeSshConn.sftp not configured"
        return self.sftp


# ---------- Holder + ActivityContext helpers -------------------------------


def make_holder(
    ctx: ActivityContext,
    *,
    sftp: FakeSftpClient | None = None,
    host: str = "sftp.example.test",
    port: int = 22,
    conn: FakeSshConn | None = None,
) -> tuple[str, SftpSessionHolder]:
    """Build an SftpSessionHolder, stash it in session_state, return
    (session_id, holder) so tests can assert on the holder's state
    after the run."""

    sftp = sftp or FakeSftpClient()
    if conn is None:
        conn = FakeSshConn(sftp=sftp)
    session_id = uuid.uuid4().hex
    holder = SftpSessionHolder(
        id=session_id,
        conn=conn,  # type: ignore[arg-type]
        sftp=sftp,  # type: ignore[arg-type]
        host=host,
        port=port,
    )
    ctx.session_state[stash_key(session_id)] = holder
    return session_id, holder


def make_activity_context(
    tmp_path: Any,
    *,
    granted: dict[str, dict[str, Any]] | None = None,
) -> ActivityContext:
    """Bare ActivityContext suitable for driving cap.sftp_* handlers
    directly. No browser pool, no signals — none of the SFTP caps need
    them."""
    from aakar.shared.registry import build_default_registry
    from aakar.storage import LocalFsObjectStore
    from aakar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        granted_capabilities=granted or {},
    )
