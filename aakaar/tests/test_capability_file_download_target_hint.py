"""target_hint discovery + HITL picker for cap.file_download.

Mirrors the captcha tests: a scripted FakeBrowserSession returns a
candidate list from `evaluate()`, we run the handler and assert it
clicks the right element. The HITL ambiguity path drives the SignalHub
the same way the captcha test does.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from aakaar.capabilities import load_into
from aakaar.capabilities.file_download import CAP_REF as DOWNLOAD_REF
from aakaar.capabilities.file_download.discovery import (
    Candidate,
    decide,
    rank_candidates,
)
from aakaar.capabilities.web_login import CAP_REF as LOGIN_REF
from aakaar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault
from aakaar.workers.browser import FakeBrowserPool, FakeBrowserSession
from tests._discovery_helpers import discovery_response as login_discovery_response


# Helper: the cap.file_download discovery JS contains the literal string
# "row_context" (a field name unique to its candidate shape). We use it
# as a marker so the FakeBrowserSession returns our scripted response.
# Picking a marker also present in the login discovery JS would cause
# login's evaluate() to match this response too — `row_context` is
# absent there.
_DOWNLOAD_DISCOVERY_MARKER = "row_context"


def _candidates(*items: dict) -> dict:
    """Build the JS-result shape that the runner expects."""
    return {_DOWNLOAD_DISCOVERY_MARKER: {"candidates": list(items), "count_total": len(items)}}


def _login_grant(tmp_path: Path):
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})
    return tenant_id, vault, vault_ref


# ---------- ranking + decision (unit) ----------------------------------------


def test_normalization_handles_em_dash_and_month_alias() -> None:
    """The user's hint 'Biller Transactions — May 2026' should match a
    candidate whose visible text is 'Biller Transactions - may 2026' or
    'Biller Txns May 26' — case, dash style, month abbreviation must
    not derail the match."""
    raw = [
        {"selector": "#a", "tag": "a", "role": "link",
         "text": "Biller Transactions - may 2026", "aria_label": None,
         "href": "/x", "row_context": None},
        {"selector": "#b", "tag": "a", "role": "link",
         "text": "Biller Txns May 26", "aria_label": None,
         "href": "/y", "row_context": None},
        {"selector": "#c", "tag": "a", "role": "link",
         "text": "Cards Settlement Apr 2026", "aria_label": None,
         "href": "/z", "row_context": None},
    ]
    ranked = rank_candidates(raw, target_hint="Biller Transactions — May 2026")
    assert [c.selector for c in ranked[:2]] == ["#a", "#b"]
    assert ranked[0].score >= ranked[1].score
    assert ranked[-1].selector == "#c"


def test_generic_button_text_lets_row_context_drive_match() -> None:
    """Regression for the NBBL Reports page shape: every card has a
    button whose text is literally 'Download CSV', the report title sits
    in the card's row_context, and a 'Transactions' filter pill lives
    elsewhere on the page. Without the generic-text down-weight, the
    filter pill's one-token match ('transactions') used to land inside
    the ambiguity window and trip the HITL picker. With the down-weight
    in place, the card whose row_context covers the hint wins clearly.
    """
    raw = [
        # Filter pill — text is the only signal it offers, scores ~0.13
        # against "Biller Transactions May 2026 csv report" (just the
        # one shared token). Not generic per se, so its text score is
        # kept; we want it to LOSE on its own merits anyway.
        {"selector": ".pill[data-cat='Transactions']", "tag": "button", "role": "button",
         "text": "Transactions", "aria_label": None,
         "href": None, "row_context": None},
        # The right answer: row_context contains the title; button text
        # is the generic "Download CSV".
        {"selector": "article.report-card:nth-of-type(1) button.download-btn",
         "tag": "button", "role": "button",
         "text": "Download CSV", "aria_label": None,
         "href": None,
         "row_context": "Transactions 12 rows Biller Transactions — May 2026 "
                        "All BBPS transactions processed via NBBL "
                        "Period May 2026 Updated 2026-05-07 09:15 IST Download CSV"},
        # A sibling card — also generic text, row_context shares fewer
        # hint tokens (different report).
        {"selector": "article.report-card:nth-of-type(2) button.download-btn",
         "tag": "button", "role": "button",
         "text": "Download CSV", "aria_label": None,
         "href": None,
         "row_context": "Settlement 12 rows Settlement Summary — May 2026 "
                        "Cycle-wise settlement summary Period May 2026 Download CSV"},
    ]
    ranked = rank_candidates(
        raw, target_hint="Biller Transactions May 2026 csv report"
    )
    assert ranked[0].selector.startswith("article.report-card:nth-of-type(1)")
    pick = decide(ranked)
    # Down-weight should clear the ambiguity: the right card wins
    # outright, no picker needed.
    assert pick.chosen is not None
    assert pick.chosen.selector.startswith("article.report-card:nth-of-type(1)")
    assert pick.ambiguous == []


def test_generic_text_without_row_context_still_scores() -> None:
    """If a generic-text button has no row_context to fall back on (e.g.
    a global 'Download all' floating button), we still score it against
    its text — otherwise we'd silently drop it from contention.
    """
    raw = [
        {"selector": "#floating-download", "tag": "button", "role": "button",
         "text": "Download", "aria_label": None, "href": None,
         "row_context": None},
    ]
    ranked = rank_candidates(raw, target_hint="Download report")
    # text "Download" shares the "download" token with the hint.
    # Without the down-weight applying (no row_context), the score is
    # non-zero so the candidate stays in contention.
    assert ranked and ranked[0].score > 0.0


def test_aria_label_survives_generic_text_downweight() -> None:
    """If the site author wired an accessible aria-label on a generic
    button, we still score against it — the down-weight only drops the
    visible-text score, not the aria signal.
    """
    raw = [
        {"selector": "#btn", "tag": "button", "role": "button",
         "text": "Download CSV",
         "aria_label": "Download Biller Transactions May 2026 CSV",
         "href": None,
         "row_context": "Some unrelated card text"},
    ]
    ranked = rank_candidates(
        raw, target_hint="Biller Transactions May 2026"
    )
    # aria_label is a full substring match → score 1.0.
    assert ranked[0].score == pytest.approx(1.0)


def test_decide_clear_winner() -> None:
    cs = [
        Candidate(selector="#a", tag="a", role="link",
                  text="Biller Transactions May 2026", aria_label=None,
                  href="/x", row_context=None, score=0.95),
        Candidate(selector="#b", tag="a", role="link",
                  text="Some Other Report", aria_label=None,
                  href="/y", row_context=None, score=0.10),
    ]
    pick = decide(cs)
    assert pick.chosen is not None and pick.chosen.selector == "#a"
    assert pick.ambiguous == []


def test_decide_ambiguous_shortlists_top_contenders() -> None:
    cs = [
        Candidate(selector="#a", tag="a", role="link", text="Biller May 2026",
                  aria_label=None, href=None, row_context=None, score=0.78),
        Candidate(selector="#b", tag="a", role="link", text="Biller May 2026 (v2)",
                  aria_label=None, href=None, row_context=None, score=0.74),
        Candidate(selector="#c", tag="a", role="link", text="Biller Settlements",
                  aria_label=None, href=None, row_context=None, score=0.40),
    ]
    pick = decide(cs)
    assert pick.chosen is None
    assert {c.selector for c in pick.ambiguous} == {"#a", "#b"}


def test_decide_no_match_when_top_score_too_low() -> None:
    cs = [
        Candidate(selector="#a", tag="a", role="link", text="Cards Settlement",
                  aria_label=None, href=None, row_context=None, score=0.15),
    ]
    pick = decide(cs)
    assert pick.none_match is True
    assert pick.chosen is None


# ---------- end-to-end via executor (with fake browser) ----------------------


@pytest.mark.asyncio
async def test_target_hint_clear_winner_clicks_through(tmp_path: Path) -> None:
    """Single matching candidate → handler clicks it via browser.download."""
    tenant_id, vault, vault_ref = _login_grant(tmp_path)

    payload = b"col,val\n1,2\n"
    sess = FakeBrowserSession(
        evaluate_responses={
            # Order matters: the fake matches by substring in insertion
            # order. Both JSes share the `bestSelector` helper, so the
            # file-download-specific marker (`candidates`) must be checked
            # before the login marker (`bestSelector`) — otherwise the
            # login response would also resolve the file_download
            # discovery call.
            **_candidates(
                {"selector": "#download-may", "tag": "a", "role": "link",
                 "text": "Biller Transactions — May 2026", "aria_label": None,
                 "href": "/reports/may", "row_context": None},
                {"selector": "#download-apr", "tag": "a", "role": "link",
                 "text": "Biller Transactions — April 2026", "aria_label": None,
                 "href": "/reports/apr", "row_context": None},
            ),
            **login_discovery_response(),
        },
        download_responses={"#download-may": ("biller-may-2026.csv", payload)},
    )
    pool = FakeBrowserPool(next_sessions=[sess])

    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
    object_store = LocalFsObjectStore(tmp_path / "objs")
    actx = ActivityContext(
        tenant_id=tenant_id, run_id=uuid.uuid4(),
        registry=registry, object_store=object_store, vault=vault,
        browser_pool=pool,
        granted_capabilities={
            LOGIN_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(id="login", kind=NodeKind.CAPABILITY, ref=LOGIN_REF,
                 inputs={"account_alias": "primary",
                         "login_url": "https://example.test/login"},
                 outputs_as="login"),
            Node(id="dl", kind=NodeKind.CAPABILITY, ref=DOWNLOAD_REF,
                 inputs={"session": "${login.session}",
                         "target_hint": "Biller Transactions May 2026"}),
        ],
        edges=[Edge.model_validate({"from": "login", "to": "dl"})],
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["dl"]["filename"] == "biller-may-2026.csv"
    assert object_store.get(outcome.outputs["dl"]["uri"]) == payload


@pytest.mark.asyncio
async def test_target_hint_ambiguous_pauses_hitl_then_clicks(tmp_path: Path) -> None:
    """When two candidates score within margin, the handler opens a
    SignalHub prompt with a numbered list. The user replies with the
    index; the handler clicks that candidate."""
    tenant_id, vault, vault_ref = _login_grant(tmp_path)

    sess = FakeBrowserSession(
        evaluate_responses={
            # Two candidates with nearly-identical text → ambiguous.
            **_candidates(
                {"selector": "#a", "tag": "a", "role": "link",
                 "text": "Biller Transactions May 2026", "aria_label": None,
                 "href": "/a", "row_context": None},
                {"selector": "#b", "tag": "a", "role": "link",
                 "text": "Biller Transactions May 2026 (Snapshot)", "aria_label": None,
                 "href": "/b", "row_context": None},
            ),
            **login_discovery_response(),
        },
        download_responses={"#b": ("snapshot.csv", b"snap")},
    )
    pool = FakeBrowserPool(next_sessions=[sess])

    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
    actx = ActivityContext(
        tenant_id=tenant_id, run_id=uuid.uuid4(),
        registry=registry, object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault, browser_pool=pool,
        granted_capabilities={
            LOGIN_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)
    signals = SignalHub()
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=signals
    )

    dag = Dag(
        nodes=[
            Node(id="login", kind=NodeKind.CAPABILITY, ref=LOGIN_REF,
                 inputs={"account_alias": "primary",
                         "login_url": "https://example.test/login"},
                 outputs_as="login"),
            Node(id="dl", kind=NodeKind.CAPABILITY, ref=DOWNLOAD_REF,
                 inputs={"session": "${login.session}",
                         "target_hint": "Biller Transactions May 2026"}),
        ],
        edges=[Edge.model_validate({"from": "login", "to": "dl"})],
    )

    async def respond_with_pick() -> None:
        for _ in range(80):
            pending = signals.list_pending(actx.run_id)
            picker = next(
                (p for p in pending if p.node_id == "dl"), None
            )
            if picker is not None:
                # The user picks #2 (the snapshot variant).
                assert "Reply with a number" in picker.message
                assert "1." in picker.message and "2." in picker.message
                await signals.resolve(
                    run_id=actx.run_id, node_id=picker.node_id, response="2"
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("picker prompt never opened")

    outcome, _ = await asyncio.gather(executor.execute(dag, ctx), respond_with_pick())
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["dl"]["filename"] == "snapshot.csv"


@pytest.mark.asyncio
async def test_target_hint_no_match_fails_clearly(tmp_path: Path) -> None:
    """No candidate scores high enough — handler raises with a useful
    message rather than clicking something random."""
    tenant_id, vault, vault_ref = _login_grant(tmp_path)
    sess = FakeBrowserSession(
        evaluate_responses={
            **_candidates(
                {"selector": "#x", "tag": "a", "role": "link",
                 "text": "Cards Settlement Apr 2026", "aria_label": None,
                 "href": "/x", "row_context": None},
            ),
            **login_discovery_response(),
        },
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
    actx = ActivityContext(
        tenant_id=tenant_id, run_id=uuid.uuid4(),
        registry=registry, object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault, browser_pool=pool,
        granted_capabilities={
            LOGIN_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )

    dag = Dag(
        nodes=[
            Node(id="login", kind=NodeKind.CAPABILITY, ref=LOGIN_REF,
                 inputs={"account_alias": "primary",
                         "login_url": "https://example.test/login"},
                 outputs_as="login"),
            Node(id="dl", kind=NodeKind.CAPABILITY, ref=DOWNLOAD_REF,
                 inputs={"session": "${login.session}",
                         "target_hint": "Biller Transactions May 2026"}),
        ],
        edges=[Edge.model_validate({"from": "login", "to": "dl"})],
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "failed"
    msg = (outcome.error or {}).get("message", "")
    assert "no element on the page matches" in msg


def test_input_validation_rejects_two_or_zero() -> None:
    """Schema enforces exactly one of trigger_selector / url / target_hint."""
    from aakaar.capabilities.file_download import _Inputs

    with pytest.raises(ValueError):
        _Inputs(session="s")  # zero
    with pytest.raises(ValueError):
        _Inputs(session="s", trigger_selector="a", url="https://x")  # two
    with pytest.raises(ValueError):
        _Inputs(session="s", trigger_selector="a", target_hint="x")  # two
    # Each on its own is OK.
    assert _Inputs(session="s", trigger_selector="a").trigger_selector == "a"
    assert _Inputs(session="s", url="https://x").url == "https://x"
    assert _Inputs(session="s", target_hint="report").target_hint == "report"
