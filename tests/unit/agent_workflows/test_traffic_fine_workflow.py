"""Unit tests for the traffic-fine triage workflow orchestrator.

Drives :func:`triage_traffic_fine` with a fake Anthropic client (the model
calls ``record_fine_extraction`` then ``emit_decision``) and monkeypatched
persistence, asserting: the document is passed multimodally, an alert is raised
iff the deadline is within the window, and the row lands in the right status.
Also covers the runtime's ``attachments`` plumbing directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows import InMemoryDecisionRepo, PermissionTier, WorkflowAgent
from src.api.agent_workflows import traffic_fine as tf
from src.core.traffic_fines import repository

NOW = datetime(2026, 5, 30, 6, 0, 0, tzinfo=timezone.utc)
_PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
_DEPOT = UUID("00000000-0000-0000-0000-0000000000d1")
_ORG = UUID("00000000-0000-0000-0000-0000000000b1")
_USER = UUID("00000000-0000-0000-0000-0000000000a1")


# ── Fake Anthropic plumbing (mirrors tests/unit/test_agent_workflows_runtime.py) ──


def _tool_use(name: str, input_: dict[str, Any], id_: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, id=id_, input=input_)


def _response(blocks: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        content=blocks,
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _client_for(extraction: dict[str, Any]) -> SimpleNamespace:
    create = AsyncMock(
        side_effect=[
            _response([_tool_use(tf.RECORD_FINE_TOOL, extraction, "rec1")]),
            _response([_tool_use("emit_decision", {"summary": "done", "proposed_actions": []}, "e1")]),
        ]
    )
    return SimpleNamespace(messages=SimpleNamespace(create=create), _create=create)


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _FakeConn:
    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _FakeAcquire:
    async def __aenter__(self) -> _FakeConn:
        return _FakeConn()

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _FakePool:
    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(saved=None, statuses=[], alerts=[])

    async def fake_get_fine(pool: Any, fid: UUID) -> dict[str, Any]:
        return {
            "id": fid,
            "status": "received",
            "raw_payload": _PDF,
            "content_type": "application/pdf",
            "depot_id": _DEPOT,
            "organization_id": _ORG,
            "uploaded_by": _USER,
        }

    async def fake_mark_status(pool: Any, fid: UUID, status: str, *, error_detail: Any = None) -> None:
        state.statuses.append((status, error_detail))

    async def fake_get_tz(pool: Any, did: UUID) -> Any:
        return None

    async def fake_save(conn: Any, fid: UUID, **kw: Any) -> None:
        state.saved = kw

    async def fake_raise(conn: Any, **kw: Any) -> UUID:
        state.alerts.append(kw)
        return uuid4()

    monkeypatch.setattr(repository, "get_fine", fake_get_fine)
    monkeypatch.setattr(repository, "mark_status", fake_mark_status)
    monkeypatch.setattr(repository, "get_depot_timezone", fake_get_tz)
    monkeypatch.setattr(repository, "save_triage_result", fake_save)
    monkeypatch.setattr(tf, "raise_fine_alert", fake_raise)
    monkeypatch.setattr(tf, "AsyncpgDecisionRepo", lambda pool: InMemoryDecisionRepo())
    return state


def _extraction(deadline_dt: datetime, **over: Any) -> dict[str, Any]:
    base = {
        "is_traffic_fine": True,
        "fine_reference": "B-7",
        "issuing_country": "Germany",
        "currency": "EUR",
        "full_amount": 150.0,
        "early_payment_amount": 120.0,
        "early_payment_deadline": deadline_dt.isoformat(),
        "iban": "DE89370400440532013000",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_within_window_raises_alert(patched: SimpleNamespace) -> None:
    client = _client_for(_extraction(NOW + timedelta(hours=36)))
    await tf.triage_traffic_fine(
        uuid4(), ts_pool=_FakePool(), static_pool=_FakePool(), anthropic_client=client, now=NOW
    )
    assert patched.saved["status"] == "alerted"
    assert len(patched.alerts) == 1
    # The document was sent multimodally: first create call's user message is a
    # content-block list whose first block is the document.
    messages = client._create.call_args_list[0].kwargs["messages"]
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_not_yet_does_not_alert(patched: SimpleNamespace) -> None:
    client = _client_for(_extraction(NOW + timedelta(days=10)))
    await tf.triage_traffic_fine(
        uuid4(), ts_pool=_FakePool(), static_pool=_FakePool(), anthropic_client=client, now=NOW
    )
    assert patched.saved["status"] == "parsed"
    assert patched.alerts == []


@pytest.mark.asyncio
async def test_not_a_fine_does_not_alert(patched: SimpleNamespace) -> None:
    client = _client_for(_extraction(NOW + timedelta(hours=12), is_traffic_fine=False))
    await tf.triage_traffic_fine(
        uuid4(), ts_pool=_FakePool(), static_pool=_FakePool(), anthropic_client=client, now=NOW
    )
    assert patched.saved["status"] == "no_alert"
    assert patched.alerts == []


def test_runtime_attachments_become_content_list() -> None:
    """The runtime change: attachments -> content list (doc first); none -> str."""
    agent = WorkflowAgent(
        anthropic_client=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock())),
        decision_repo=InMemoryDecisionRepo(),
    )
    workflow = tf.build_traffic_fine_workflow()
    auth = AuthContext(
        user_id=_USER, organization_id=_ORG, role="customer_admin", visible_depot_ids=[_DEPOT]
    )
    block = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0="},
    }
    out = agent._format_user_message(
        depot_id=_DEPOT,
        workflow=workflow,
        auth_context=auth,
        user_input=None,
        permission_tier=PermissionTier.INFORM,
        attachments=[block],
    )
    assert isinstance(out, list)
    assert out[0] == block
    assert out[-1]["type"] == "text"

    plain = agent._format_user_message(
        depot_id=_DEPOT,
        workflow=workflow,
        auth_context=auth,
        user_input=None,
        permission_tier=PermissionTier.INFORM,
    )
    assert isinstance(plain, str)
