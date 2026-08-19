"""Second-pass LLM judge for proposal safety and confidence."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agenticscrum.ado.client import AdoClient
from agenticscrum.config import Settings
from agenticscrum.llm.client import build_chat_model
from agenticscrum.llm.tools import build_ado_tools
from agenticscrum.models import ProposedChange


PROPOSAL_JUDGE_PROMPT = """You are a second-pass reviewer for proposed Azure DevOps work item changes.

Your job is to judge whether the proposal is safe to auto-apply without human review,
and to provide an adjusted confidence score and risk level.

You may call Azure DevOps read-only tools to validate things like allowed states or the current work item.
Use tools only when needed; keep tool calls minimal.

Autopilot safety policy:
- Auto-apply is only allowed for:
  - Assign proposals (changeType=Assign) with a non-empty newAssignee and no other field updates.
  - StateTransition proposals (changeType=StateTransition) that are NOT closure states (not Done/Closed/Removed)
    and do not include additional field updates.
- Do NOT allow auto-apply for Create, Update, Comment, or any closure proposal.

If the payload attempts to update System.State via fieldUpdates when changeType is NOT StateTransition,
that is a red flag; mark autoApplyOk=false and flag it.

If changeType is StateTransition, validate newState against allowed states for that work item type.
If you are unsure of allowed states, call ado_get_work_item_type_states.

Return ONLY valid JSON matching this schema:
{{
  "autoApplyOk": true|false,
  "adjustedConfidence": 0-100,
  "riskLevel": "low"|"medium"|"high",
  "reasons": ["..."],
  "flags": ["..."]
}}

Proposal JSON:
{proposal_json}

Payload JSON:
{payload_json}
"""


class ProposalJudgeOutput(BaseModel):
    """Parsed judge output for one proposal."""

    model_config = ConfigDict(populate_by_name=True)

    auto_apply_ok: bool = Field(alias="autoApplyOk")
    adjusted_confidence: int = Field(alias="adjustedConfidence", ge=0, le=100)
    risk_level: Literal["low", "medium", "high"] = Field(alias="riskLevel")
    reasons: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    @field_validator("risk_level", mode="before")
    @classmethod
    def _normalize_risk(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("reasons", mode="before")
    @classmethod
    def _normalize_reasons(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @field_validator("flags", mode="before")
    @classmethod
    def _normalize_flags(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []


@dataclass(frozen=True)
class JudgeToolCallRecord:
    tool_name: str
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    duration_ms: int


@dataclass(frozen=True)
class JudgeResult:
    output: ProposalJudgeOutput
    tool_calls: list[JudgeToolCallRecord]
    request_prompt: str
    raw_response_text: str
    normalized_json: dict[str, Any]


class LLMJudgeError(RuntimeError):
    """Raised when the judge returns invalid output or the call fails."""

    def __init__(
        self,
        message: str,
        *,
        request_prompt: str,
        raw_response_text: str = "",
        normalized_json: dict[str, Any] | None = None,
        tool_calls: list[JudgeToolCallRecord] | None = None,
    ) -> None:
        super().__init__(message)
        self.request_prompt = request_prompt
        self.raw_response_text = raw_response_text
        self.normalized_json = normalized_json or {}
        self.tool_calls = tool_calls or []


def _strip_fences(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.strip("`")
        if value.lstrip().startswith("json"):
            value = value.lstrip()[4:]
    return value.strip()


def parse_judge_output(raw_text: str) -> tuple[dict[str, Any], ProposalJudgeOutput]:
    """Parse judge output and return (normalized_json, parsed_schema)."""

    text = _strip_fences(raw_text)
    data = json.loads(text)

    # Tolerate common wrapper keys.
    if isinstance(data, dict):
        for key in ("judgement", "judge", "result", "output", "assessment"):
            if key in data and isinstance(data.get(key), dict):
                data = data[key]
                break

    if not isinstance(data, dict):
        raise ValueError("Judge output was not a JSON object")

    # Key aliases
    if "autoApplyOk" not in data:
        for alt in ("auto_apply_ok", "autoApply", "autopilotOk", "autopilot_ok"):
            if alt in data:
                data["autoApplyOk"] = data.pop(alt)
                break
    if "adjustedConfidence" not in data:
        for alt in ("adjusted_confidence", "confidence", "score"):
            if alt in data:
                data["adjustedConfidence"] = data.pop(alt)
                break
    if "riskLevel" not in data:
        for alt in ("risk_level", "risk"):
            if alt in data:
                data["riskLevel"] = data.pop(alt)
                break

    if "reasons" not in data:
        data["reasons"] = []
    if "flags" not in data:
        data["flags"] = []

    parsed = ProposalJudgeOutput.model_validate(data)
    normalized = dict(data)
    return normalized, parsed


async def judge_proposal(
    *,
    settings: Settings,
    ado: AdoClient,
    proposal: ProposedChange,
) -> JudgeResult:
    """Run the judge tool loop and return a safety/confidence assessment."""

    model = build_chat_model(settings)
    tools = build_ado_tools(ado)
    model_with_tools = model.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}

    proposal_json = json.dumps(
        {
            "proposalId": proposal.id,
            "changeType": proposal.change_type.value,
            "workItemType": proposal.work_item_type.value,
            "targetWorkItemId": proposal.target_work_item_id,
            "title": proposal.title,
            "rationale": proposal.rationale,
            "sourceQuote": proposal.source_quote,
            "sourceMeetingTitle": proposal.source_meeting_title,
            "sourceMeetingDate": str(proposal.source_meeting_date),
        },
        indent=2,
        default=str,
    )
    payload_json = json.dumps(dict(proposal.proposed_payload or {}), indent=2, default=str)
    prompt = PROPOSAL_JUDGE_PROMPT.format(proposal_json=proposal_json, payload_json=payload_json)

    messages: list[Any] = [SystemMessage(content=prompt), HumanMessage(content="Judge now.")]
    records: list[JudgeToolCallRecord] = []
    final_text = ""

    try:
        for _step in range(min(4, settings.llm_max_tool_steps) + 1):
            response = await model_with_tools.ainvoke(messages)
            messages.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                final_text = str(response.content)
                break

            for call in tool_calls:
                tool_name = call["name"]
                args = dict(call.get("args") or {})
                start = time.perf_counter()
                result = await tools_by_name[tool_name].ainvoke(args)
                duration_ms = int((time.perf_counter() - start) * 1000)
                records.append(
                    JudgeToolCallRecord(
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
            messages.append(HumanMessage(content="Return final JSON now. Do not call tools."))
            final_response = await model.ainvoke(messages)
            messages.append(final_response)
            final_text = str(final_response.content)

        max_repairs = max(0, int(settings.llm_schema_repair_max_attempts))
        last_parse_error: Exception | None = None
        for attempt in range(max_repairs + 1):
            try:
                normalized, parsed = parse_judge_output(final_text)
                break
            except (ValidationError, json.JSONDecodeError, ValueError) as parse_exc:
                last_parse_error = parse_exc
                if attempt >= max_repairs:
                    raise
                previous = (final_text or "").strip()
                if len(previous) > 12000:
                    previous = previous[:12000] + "\n…[truncated]"
                messages.append(
                    HumanMessage(
                        content=(
                            "Your previous response was not valid JSON matching the "
                            "required judge schema.\n\n"
                            f"Parse / validation errors:\n{parse_exc}\n\n"
                            f"Previous response:\n{previous or '(empty)'}\n\n"
                            "Return ONLY corrected valid JSON. Do not include prose "
                            "or markdown fencing."
                        )
                    )
                )
                repair_response = await model.ainvoke(messages)
                messages.append(repair_response)
                final_text = str(repair_response.content)
        else:
            assert last_parse_error is not None
            raise last_parse_error

        if not parsed.reasons:
            parsed = ProposalJudgeOutput.model_validate(
                {**normalized, "reasons": ["No reasons provided by judge."]}
            )
        return JudgeResult(
            output=parsed,
            tool_calls=records,
            request_prompt=prompt,
            raw_response_text=final_text,
            normalized_json=normalized,
        )
    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
        normalized: dict[str, Any] = {}
        if final_text:
            try:
                normalized, _parsed = parse_judge_output(final_text)
            except Exception:
                normalized = {}
        raise LLMJudgeError(
            str(exc),
            request_prompt=prompt,
            raw_response_text=final_text,
            normalized_json=normalized,
            tool_calls=records,
        ) from exc
    except Exception as exc:
        raise LLMJudgeError(
            str(exc),
            request_prompt=prompt,
            raw_response_text=final_text,
            normalized_json={},
            tool_calls=records,
        ) from exc

