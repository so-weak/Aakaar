"""Built-in action and control definitions.

These are the non-capability primitives the LLM is allowed to compose: browser
manipulation, HTTP, files, managed storage, and a couple of control nodes.

We deliberately ship a small set in v1. New primitives only land here when a
real workflow needs one — generality without need is how registries become
unmanageable.

v1 omits intentionally: control.branch and control.for_each. Linear and
parallel DAGs cover the early use cases; iteration and branching arrive once
the interpreter has the supporting machinery.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.shared.registry.registry import Registry
from aakaar.shared.registry.types import ActionDefinition, ControlDefinition


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- browser --------------------------------------------------------


class _OpenSessionIn(_Strict):
    profile: str | None = Field(
        default=None,
        description="Optional named browser profile to load (e.g. cookies/storage).",
    )


class _OpenSessionOut(_Strict):
    session: str = Field(description="Opaque handle for subsequent browser.* calls.")


class _SessionOnlyIn(_Strict):
    session: str


class _NavigateIn(_Strict):
    session: str
    url: str


class _WaitForIn(_Strict):
    session: str
    selector: str
    timeout_ms: int = Field(default=30000, ge=0, le=600000)


class _FillIn(_Strict):
    session: str
    selector: str
    value: str


class _FillSecretIn(_Strict):
    session: str
    selector: str
    capability_ref: str = Field(
        description="Capability ref whose grant holds the secret (e.g. cap.web_login)."
    )
    account_alias: str = Field(description="Which credential alias on that grant.")
    secret_name: str = Field(description="Which key inside the secret bundle, e.g. 'password'.")


class _ClickIn(_Strict):
    session: str
    selector: str


class _ClickByTextIn(_Strict):
    session: str
    text: str = Field(
        description=(
            "Visible text of the element to click (link, button, or any "
            "element). Case-insensitive substring match. Use this for "
            "navigation links and logout buttons whose CSS selector you "
            "don't know."
        )
    )


class _SelectIn(_Strict):
    session: str
    selector: str
    value: str


class _SetFieldIn(_Strict):
    session: str
    label: str = Field(
        description=(
            "Visible label text of the form field (e.g. 'Switch Type', "
            "'Cycle Number'). Case-insensitive substring match."
        )
    )
    value: str = Field(
        description=(
            "Value to set. For <select>: the option's value or visible "
            "label. For radio groups: the option's label (e.g. 'Yes' / "
            "'No'). For text/date inputs: the literal value."
        )
    )


class _UploadIn(_Strict):
    session: str
    selector: str
    file_uri: str


class _DownloadIn(_Strict):
    session: str
    trigger_selector: str | None = None
    url: str | None = None


class _DownloadOut(_Strict):
    file_uri: str


class _ExtractIn(_Strict):
    session: str
    selector: str
    attribute: str = Field(default="text", description="`text`, `html`, or a DOM attribute name.")


class _ExtractOut(_Strict):
    value: str


class _ScreenshotOut(_Strict):
    image_uri: str


class _Empty(_Strict):
    pass


def _browser_defs() -> list[ActionDefinition]:
    return [
        ActionDefinition(
            ref="browser.open_session",
            description="Open a fresh browser session. Returns a session handle.",
            input_schema=_OpenSessionIn,
            output_schema=_OpenSessionOut,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.navigate",
            description="Navigate the session to a URL.",
            input_schema=_NavigateIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.wait_for",
            description="Wait until a CSS selector is present in the DOM.",
            input_schema=_WaitForIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.fill",
            description="Type a value into the element matching `selector`.",
            input_schema=_FillIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.fill_secret",
            description=(
                "Fill an input with a vault-stored secret resolved by "
                "(capability_ref, account_alias, secret_name). Use this when "
                "you need to compose a multi-step login (captcha, MFA) and "
                "still keep credentials out of the DAG."
            ),
            input_schema=_FillSecretIn,
            output_schema=_Empty,
            tags=("browser", "secret"),
        ),
        ActionDefinition(
            ref="browser.click",
            description="Click the element matching `selector`.",
            input_schema=_ClickIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.click_by_text",
            description=(
                "Click an element by its visible text (link, button, or "
                "any element). Prefer this over `browser.click` for nav "
                "links and logout buttons whose CSS selectors aren't "
                "verified. Tries link → button → any-text in order."
            ),
            input_schema=_ClickByTextIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.select",
            description="Select an option in a <select> element by value.",
            input_schema=_SelectIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.set_field",
            description=(
                "Set a form control by its visible label. Auto-dispatches "
                "over <select>, <input>, and radio groups — prefer this "
                "over `browser.fill` / `browser.select` / `browser.click` "
                "when you don't have a verified CSS selector for the "
                "field. Removes selector hallucination for multi-field "
                "form workflows."
            ),
            input_schema=_SetFieldIn,
            output_schema=_Empty,
            tags=("browser", "form"),
        ),
        ActionDefinition(
            ref="browser.upload",
            description="Attach a file (by managed storage URI) to a file input.",
            input_schema=_UploadIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.download",
            description=(
                "Trigger and capture a download. Provide either `trigger_selector` "
                "(an element to click) or `url` (a direct link). Returns the saved "
                "managed-storage URI."
            ),
            input_schema=_DownloadIn,
            output_schema=_DownloadOut,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.extract",
            description="Read text or an attribute from a matched element.",
            input_schema=_ExtractIn,
            output_schema=_ExtractOut,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.screenshot",
            description="Capture a full-page screenshot. Returns a managed-storage URI.",
            input_schema=_SessionOnlyIn,
            output_schema=_ScreenshotOut,
            tags=("browser",),
        ),
        ActionDefinition(
            ref="browser.close_session",
            description="Close the browser session and release the worker.",
            input_schema=_SessionOnlyIn,
            output_schema=_Empty,
            tags=("browser",),
        ),
    ]


# ---------- http -----------------------------------------------------------


class _HttpRequestIn(_Strict):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    timeout_ms: int = Field(default=30000, ge=0, le=600000)


class _HttpRequestOut(_Strict):
    status: int
    headers: dict[str, str]
    body: Any


def _http_defs() -> list[ActionDefinition]:
    return [
        ActionDefinition(
            ref="http.request",
            description="Make an HTTP request. Use this only when a capability is not available.",
            input_schema=_HttpRequestIn,
            output_schema=_HttpRequestOut,
            tags=("http",),
        ),
    ]


# ---------- file -----------------------------------------------------------


class _ParseCsvIn(_Strict):
    file_uri: str
    delimiter: str = ","
    has_header: bool = True


class _RowsOut(_Strict):
    rows: list[dict[str, Any]]


class _WriteCsvIn(_Strict):
    rows: list[dict[str, Any]]
    file_uri: str


class _FileUriOut(_Strict):
    file_uri: str


class _ReadLocalIn(_Strict):
    path: str = Field(
        description=(
            "Absolute local filesystem path on the API host. The host "
            "must have AAKAAR_ALLOW_LOCAL_PATHS=true; otherwise the call "
            "fails. Use this only when the user explicitly references a "
            "file already on disk (e.g. ~/Downloads/report.csv)."
        )
    )


class _ReadLocalOut(_Strict):
    file_uri: str = Field(description="Managed-storage URI of the ingested file.")
    filename: str = Field(description="Basename of the original local file.")
    size: int = Field(description="File size in bytes.")


def _file_defs() -> list[ActionDefinition]:
    return [
        ActionDefinition(
            ref="file.parse_csv",
            description="Parse a CSV by managed-storage URI into a list of row dicts.",
            input_schema=_ParseCsvIn,
            output_schema=_RowsOut,
            tags=("file",),
        ),
        ActionDefinition(
            ref="file.write_csv",
            description="Write rows to a CSV at the given managed-storage URI.",
            input_schema=_WriteCsvIn,
            output_schema=_FileUriOut,
            tags=("file",),
        ),
        ActionDefinition(
            ref="file.read_local",
            description=(
                "Ingest a local filesystem file into managed storage and "
                "return its `aakaar://` URI. Pair with `cap.file_upload` to "
                "upload a file the user has on disk. Requires "
                "AAKAAR_ALLOW_LOCAL_PATHS=true on the API host."
            ),
            input_schema=_ReadLocalIn,
            output_schema=_ReadLocalOut,
            tags=("file", "ingestion"),
        ),
    ]


# ---------- storage --------------------------------------------------------


class _StoragePutIn(_Strict):
    key: str
    source_file_uri: str


class _StorageGetIn(_Strict):
    uri: str


class _StorageUriOut(_Strict):
    uri: str


def _storage_defs() -> list[ActionDefinition]:
    return [
        ActionDefinition(
            ref="storage.put",
            description="Copy a file into managed storage at `key` (tenant-scoped). Returns its URI.",
            input_schema=_StoragePutIn,
            output_schema=_StorageUriOut,
            tags=("storage",),
        ),
        ActionDefinition(
            ref="storage.get",
            description="Materialize a managed-storage URI to a local file. Returns the local URI.",
            input_schema=_StorageGetIn,
            output_schema=_FileUriOut,
            tags=("storage",),
        ),
    ]


# ---------- time -----------------------------------------------------------


class _TimeNowOut(_Strict):
    ist_date: str = Field(description="Today's date in IST as yyyy-mm-dd.")
    ist_datetime: str = Field(description="ISO-8601 datetime in IST.")
    utc_date: str = Field(description="Today's date in UTC as yyyy-mm-dd.")
    utc_datetime: str = Field(description="ISO-8601 datetime in UTC.")


def _time_defs() -> list[ActionDefinition]:
    return [
        ActionDefinition(
            ref="time.now",
            description=(
                "Return the current date and datetime in both IST and UTC. "
                "Use `${node.ist_date}` when a workflow needs 'today' so "
                "saved DAGs don't carry a stale literal date."
            ),
            input_schema=_Empty,
            output_schema=_TimeNowOut,
            tags=("time",),
        ),
    ]


# ---------- control --------------------------------------------------------


class _WaitIn(_Strict):
    seconds: float = Field(ge=0)


class _HumanPromptIn(_Strict):
    message: str = Field(description="Prompt shown to the user in the chat panel.")
    expects: Literal["text", "otp", "confirm"] = "text"
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class _HumanPromptOut(_Strict):
    response: str


def _control_defs() -> list[ControlDefinition]:
    return [
        ControlDefinition(
            ref="control.wait",
            description="Pause for `seconds` before continuing.",
            input_schema=_WaitIn,
            output_schema=_Empty,
        ),
        ControlDefinition(
            ref="human.prompt",
            description=(
                "Pause the run and ask the user for input via the chat panel. Use this "
                "for captchas, OTPs, or any human-in-the-loop confirmation."
            ),
            input_schema=_HumanPromptIn,
            output_schema=_HumanPromptOut,
        ),
    ]


# ---------- public ---------------------------------------------------------


def build_default_registry() -> Registry:
    """Return a Registry preloaded with all built-in primitives.

    Capabilities are added separately by the capabilities loader (which scans
    the `capabilities/` package at startup).
    """
    reg = Registry()
    reg.add_many(_browser_defs())
    reg.add_many(_http_defs())
    reg.add_many(_file_defs())
    reg.add_many(_storage_defs())
    reg.add_many(_time_defs())
    reg.add_many(_control_defs())
    return reg
