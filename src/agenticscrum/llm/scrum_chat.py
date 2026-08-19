"""Scrum-master style chat helper with optional ADO tool calling."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy.orm import Session

from agenticscrum.ado.client import AdoClient
from agenticscrum.config import Settings
from agenticscrum.llm.client import build_chat_model
from agenticscrum.llm.tools import build_ado_tools
from agenticscrum.models import ChatMessage, ChatRole, ChatSession, LLMCallLog


SCRUM_CHAT_PROMPT = """You are Agentic Scrum, a practical AI scrum master for a Kanban team.

Speak like a helpful teammate in chat: concise, actionable, and board-aware.
You help the user:
- understand current work status and risks
- keep work items clean (title/description/AC/estimates/assignees/states)
- turn clear action requests into concrete next steps

You may call Azure DevOps read-only tools when you need fresh facts.
If you don't have enough information, ask 1-2 focused questions.

Be concise. Prefer bullets. When you reference an item, include its ID (e.g. #12345).

When pending/failed proposals exist, mention useful next steps
(e.g. "2 failed applies — want me to Fix with AI?").

Context:
- ADO org/project: {ado_org}/{ado_project}
- Area path: {ado_area_path}
- Effort scale: {effort_scale}
- Autopilot: {autopilot_status}
- Pending proposals: {pending_count}
- Failed proposals: {failed_count}
- Awaiting assignee approval: {awaiting_count}
{summary_block}
"""


@dataclass(frozen=True)
class ChatToolCallRecord:
    tool_name: str
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    duration_ms: int


def _to_langchain_messages(
    system_prompt: str,
    history: list[ChatMessage],
    max_messages: int = 20,
) -> list[Any]:
    msgs: list[Any] = [SystemMessage(content=system_prompt)]
    for msg in history[-max_messages:]:
        if msg.role == ChatRole.USER:
            msgs.append(HumanMessage(content=msg.content))
        elif msg.role == ChatRole.ASSISTANT:
            msgs.append(AIMessage(content=msg.content))
        elif msg.role == ChatRole.SYSTEM:
            msgs.append(SystemMessage(content=msg.content))
    return msgs


async def maybe_summarize_session(
    session: Session,
    settings: Settings,
    chat_session: ChatSession,
    history: list[ChatMessage],
    *,
    threshold: int = 24,
) -> None:
    """Roll older turns into ChatSession.summary when history grows large."""

    if len(history) < threshold:
        return
    older = history[:-12]
    if not older:
        return
    transcript = "\n".join(f"{m.role.value}: {m.content}" for m in older)
    try:
        model = build_chat_model(settings)
        response = await model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Summarize this scrum chat for future context. "
                        "Keep decisions, open questions, and work item IDs. Max 12 bullets."
                    )
                ),
                HumanMessage(content=transcript[:12000]),
            ]
        )
        new_summary = str(response.content or "").strip()
        if new_summary:
            prior = (chat_session.summary or "").strip()
            chat_session.summary = (
                f"{prior}\n\n{new_summary}".strip() if prior else new_summary
            )[:8000]
            session.flush()
    except Exception:
        return


async def scrum_master_reply(
    *,
    settings: Settings,
    ado: AdoClient | None,
    history: list[ChatMessage],
    pending_count: int,
    failed_count: int,
    awaiting_count: int,
    session_summary: str | None = None,
) -> tuple[str, list[ChatToolCallRecord]]:
    """Generate one assistant reply for the chat UI."""

    summary_block = ""
    if session_summary and session_summary.strip():
        summary_block = f"\nEarlier conversation summary:\n{session_summary.strip()}\n"

    model = build_chat_model(settings)
    system_prompt = SCRUM_CHAT_PROMPT.format(
        ado_org=settings.ado_org,
        ado_project=settings.ado_project,
        ado_area_path=settings.ado_area_path,
        effort_scale=", ".join(str(v) for v in settings.ado_effort_scale),
        autopilot_status="enabled" if settings.app_autopilot_enabled else "disabled",
        pending_count=pending_count,
        failed_count=failed_count,
        awaiting_count=awaiting_count,
        summary_block=summary_block,
    )
    messages = _to_langchain_messages(system_prompt, history)

    if ado is None:
        response = await model.ainvoke(messages)
        return str(response.content).strip(), []

    tools = build_ado_tools(ado)
    model_with_tools = model.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}
    records: list[ChatToolCallRecord] = []
    final_text = ""

    for _step in range(settings.llm_max_tool_steps + 1):
        response = await model_with_tools.ainvoke(messages)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            final_text = str(response.content).strip()
            break
        for call in tool_calls:
            tool_name = call["name"]
            args = dict(call.get("args") or {})
            start = time.perf_counter()
            result = await tools_by_name[tool_name].ainvoke(args)
            duration_ms = int((time.perf_counter() - start) * 1000)
            records.append(
                ChatToolCallRecord(
                    tool_name=tool_name,
                    input_payload=args,
                    output_payload={"result": result},
                    duration_ms=duration_ms,
                )
            )
            messages.append(
                ToolMessage(
                    content=json.dumps(result, default=str),
                    tool_call_id=call["id"],
                    name=tool_name,
                )
            )

    if not final_text:
        final_response = await model.ainvoke(
            messages
            + [
                HumanMessage(
                    content="Provide your final answer now. Do not call tools."
                )
            ]
        )
        final_text = str(final_response.content).strip()

    return final_text, records


def persist_chat_tool_records(
    session: Session,
    *,
    chat_session_id: int,
    chat_message_id: int | None,
    records: list[ChatToolCallRecord],
) -> None:
    """Persist chat tool-call audit rows."""

    from agenticscrum.models import ToolCallLog

    for record in records:
        session.add(
            ToolCallLog(
                chat_session_id=chat_session_id,
                chat_message_id=chat_message_id,
                tool_name=record.tool_name,
                input_payload=record.input_payload,
                output_payload=record.output_payload,
                duration_ms=record.duration_ms,
            )
        )


def persist_chat_llm_call(
    session: Session,
    settings: Settings,
    *,
    chat_session_id: int,
    response_text: str,
    error_message: str | None = None,
    duration_ms: int = 0,
) -> None:
    """Persist a lightweight chat LLM audit row."""

    session.add(
        LLMCallLog(
            chat_session_id=chat_session_id,
            operation="scrum_chat",
            model_name=settings.llm_model,
            request_prompt="",
            response_text=response_text[:20000],
            error_message=error_message,
            duration_ms=duration_ms,
        )
    )
