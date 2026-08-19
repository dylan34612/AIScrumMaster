"""LangChain tool-calling loop for meeting analysis."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from agenticscrum.ado.client import AdoClient
from agenticscrum.config import Settings
from agenticscrum.llm.client import build_chat_model
from agenticscrum.llm.prompt import JSON_REPAIR_PROMPT, SYSTEM_PROMPT
from agenticscrum.llm.tools import build_ado_tools
from agenticscrum.models import TeamMember
from agenticscrum.schemas import LLMOutputSchema


@dataclass(frozen=True)
class ToolCallRecord:
    """Captured tool-call audit record."""

    tool_name: str
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    duration_ms: int


@dataclass(frozen=True)
class AgentResult:
    """Validated LLM proposals and tool-call audit records."""

    output: LLMOutputSchema
    tool_calls: list[ToolCallRecord]
    request_prompt: str
    raw_response_text: str
    normalized_json: dict[str, Any]


class LLMAnalysisError(RuntimeError):
    """Raised when the LLM returns invalid output or the call fails."""

    def __init__(
        self,
        message: str,
        *,
        request_prompt: str,
        raw_response_text: str = "",
        normalized_json: dict[str, Any] | None = None,
        tool_calls: list[ToolCallRecord] | None = None,
    ) -> None:
        super().__init__(message)
        self.request_prompt = request_prompt
        self.raw_response_text = raw_response_text
        self.normalized_json = normalized_json or {}
        self.tool_calls = tool_calls or []


async def analyze_meeting(
    settings: Settings,
    ado: AdoClient,
    meeting_title: str,
    meeting_date: date,
    meeting_notes: str,
    grounding_catalog: list[dict[str, Any]],
    roster: list[TeamMember],
) -> AgentResult:
    """Run the LLM tool loop and return validated proposed changes."""

    model = build_chat_model(settings)
    tools = build_ado_tools(ado)
    model_with_tools = model.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}
    prompt = SYSTEM_PROMPT.format(
        meeting_title=meeting_title,
        meeting_date=meeting_date.isoformat(),
        meeting_notes=meeting_notes,
        grounding_catalog_json=json.dumps(grounding_catalog, default=str),
        team_roster_json=json.dumps(
            [
                {"displayName": member.display_name, "email": member.email}
                for member in roster
                if member.active
            ],
            default=str,
        ),
        effort_scale=", ".join(str(item) for item in settings.ado_effort_scale),
        area_path=settings.ado_area_path,
    )
    messages: list[Any] = [SystemMessage(content=prompt), HumanMessage(content="Analyze now.")]
    records: list[ToolCallRecord] = []
    final_text = ""

    try:
        for _step in range(settings.llm_max_tool_steps + 1):
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
                output_payload = {"result": result}
                records.append(
                    ToolCallRecord(
                        tool_name=tool_name,
                        input_payload=args,
                        output_payload=output_payload,
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
            messages.append(HumanMessage(content="Return final JSON now."))
            final_response = await model.ainvoke(messages)
            messages.append(final_response)
            final_text = str(final_response.content)

        final_text, normalized, parsed = await parse_llm_output_with_repair(
            model,
            messages,
            final_text,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            max_attempts=settings.llm_schema_repair_max_attempts,
        )
        return AgentResult(
            output=parsed,
            tool_calls=records,
            request_prompt=prompt,
            raw_response_text=final_text,
            normalized_json=normalized,
        )
    except Exception as exc:
        # Capture prompt + any partial output for UI debugging.
        message = str(exc)
        normalized: dict[str, Any] = {}
        if final_text:
            try:
                normalized, _parsed = parse_llm_output_debug(
                    final_text,
                    meeting_title=meeting_title,
                    meeting_date=meeting_date,
                )
            except Exception:
                normalized = {}
        raise LLMAnalysisError(
            message,
            request_prompt=prompt,
            raw_response_text=final_text,
            normalized_json=normalized,
            tool_calls=records,
        ) from exc


async def revise_payload(
    settings: Settings,
    request_text: str,
    original_payload: dict[str, Any],
) -> dict[str, Any]:
    """Ask the LLM to revise one proposal payload using free-form instructions."""

    model = build_chat_model(settings)
    response = await model.ainvoke(
        [
            SystemMessage(
                content=(
                    "You revise one Azure DevOps proposal payload. Return only valid JSON "
                    "for the revised single proposedChanges item."
                )
            ),
            HumanMessage(
                content=(
                    f"Request: {request_text}\n\n"
                    f"Original payload:\n{json.dumps(original_payload, indent=2, default=str)}"
                )
            ),
        ]
    )
    return json.loads(str(response.content))


REVISE_PAYLOAD_TOOL_PROMPT = """You are fixing an Azure DevOps work item change payload so it can be applied successfully.

You will be given:
- the original payload JSON
- a fix request (usually an ADO validation error)

You may call Azure DevOps tools to fetch:
- the current work item (to get its type/state/fields)
- the allowed states for a work item type

Rules:
- Return ONLY a valid JSON object representing the revised payload.
- Do NOT include prose, markdown fencing, or explanations.
- Keep changes minimal: only adjust fields required to satisfy the error.
- If a field is rejected as read-only, remove it from fieldUpdates.
- If System.State is invalid:
  - If this is NOT a StateTransition proposal, remove System.State from fieldUpdates.
  - If this IS a StateTransition proposal, choose a valid state from ado_get_work_item_type_states.
- Never include automation watermarks (no "Agentic Scrum", no "Approved by", no "meeting notes/transcript" phrases).

Fix request:
{request_text}

Original payload JSON:
{original_payload_json}
"""


REFINE_PAYLOAD_TOOL_PROMPT = """You are refining an Azure DevOps proposal payload to make it clearer, more accurate, and more professional.

You will be given:
- the proposal metadata (changeType, workItemType, targetWorkItemId, title, rationale)
- the current payload JSON
- the judge assessment (risk, flags, reasons)

You may call Azure DevOps tools to fetch:
- the current work item (to ensure accuracy)
- comments (to avoid duplicating existing info)
- allowed states (if you are editing newState/System.State)

Rules:
- Return ONLY a valid JSON object representing the revised payload (the same shape as the original payload).
- Do NOT include prose, markdown fencing, or explanations.
- Keep changes minimal and targeted to the issues the judge raised.
- Do NOT add new scope (no new fields) unless necessary for clarity/ADO validation.
- Never include automation watermarks (no "Agentic Scrum", no "Approved by", no "meeting notes/transcript" phrases).
- For Comment proposals: the commentText must contain concrete details (decisions, blockers, next steps, specific changes),
  and must NOT be generic.

Proposal metadata:
{proposal_json}

Judge assessment:
{judge_json}

Current payload JSON:
{original_payload_json}
"""


async def refine_payload_with_tools(
    settings: Settings,
    ado: AdoClient,
    *,
    proposal: dict[str, Any],
    judge: dict[str, Any],
    original_payload: dict[str, Any],
) -> dict[str, Any]:
    """Refine a proposal payload using LLM tool calling (ADO read tools)."""

    model = build_chat_model(settings)
    tools = build_ado_tools(ado)
    model_with_tools = model.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}

    prompt = REFINE_PAYLOAD_TOOL_PROMPT.format(
        proposal_json=json.dumps(proposal, indent=2, default=str),
        judge_json=json.dumps(judge, indent=2, default=str),
        original_payload_json=json.dumps(original_payload, indent=2, default=str),
    )
    messages: list[Any] = [SystemMessage(content=prompt), HumanMessage(content="Refine now.")]

    final_text = ""
    for _step in range(settings.llm_max_tool_steps + 1):
        response = await model_with_tools.ainvoke(messages)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            final_text = str(response.content)
            break
        for call in tool_calls:
            tool_name = call["name"]
            args = dict(call.get("args") or {})
            result = await tools_by_name[tool_name].ainvoke(args)
            messages.append(
                ToolMessage(
                    content=json.dumps(result, default=str),
                    tool_call_id=call["id"],
                    name=tool_name,
                )
            )

    if not final_text:
        final_response = await model.ainvoke(
            messages + [HumanMessage(content="Return final JSON now. Do not call tools.")]
        )
        final_text = str(final_response.content)

    text = final_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        data = data["payload"]
    if not isinstance(data, dict):
        raise ValueError("Refined payload was not a JSON object")
    return data


async def revise_payload_with_tools(
    settings: Settings,
    ado: AdoClient,
    *,
    request_text: str,
    original_payload: dict[str, Any],
) -> dict[str, Any]:
    """Revise a proposal payload using LLM tool calling (ADO read tools)."""

    model = build_chat_model(settings)
    tools = build_ado_tools(ado)
    model_with_tools = model.bind_tools(tools)
    tools_by_name = {tool.name: tool for tool in tools}

    prompt = REVISE_PAYLOAD_TOOL_PROMPT.format(
        request_text=request_text,
        original_payload_json=json.dumps(original_payload, indent=2, default=str),
    )
    messages: list[Any] = [SystemMessage(content=prompt), HumanMessage(content="Revise now.")]

    final_text = ""
    for _step in range(settings.llm_max_tool_steps + 1):
        response = await model_with_tools.ainvoke(messages)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            final_text = str(response.content)
            break
        for call in tool_calls:
            tool_name = call["name"]
            args = dict(call.get("args") or {})
            result = await tools_by_name[tool_name].ainvoke(args)
            messages.append(
                ToolMessage(
                    content=json.dumps(result, default=str),
                    tool_call_id=call["id"],
                    name=tool_name,
                )
            )

    if not final_text:
        final_response = await model.ainvoke(
            messages + [HumanMessage(content="Return final JSON now. Do not call tools.")]
        )
        final_text = str(final_response.content)

    text = final_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        data = data["payload"]
    if not isinstance(data, dict):
        raise ValueError("Revised payload was not a JSON object")
    return data


def parse_llm_output(
    raw_text: str,
    meeting_title: str | None = None,
    meeting_date: date | None = None,
) -> LLMOutputSchema:
    """Parse and validate LLM output, tolerating fenced JSON if present."""

    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("LLM response JSON root must be an object")
        data = normalize_llm_output(data, meeting_title, meeting_date)
        return LLMOutputSchema.model_validate(data)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValueError(f"LLM response was not valid JSON: {exc}") from exc


_PLACEHOLDER_RATIONALES = {
    "captured from meeting notes for review and tracking.",
    "captured from meeting notes.",
    "captured for review and tracking.",
    "captured meeting discussion.",
}

_GENERIC_CREATE_TITLES = {
    "create new work item",
    "create new pbi",
    "create new product backlog item",
    "create new bug",
    "create new task",
    "create new feature",
    "create new epic",
    "new work item",
    "new pbi",
    "new product backlog item",
    "new bug",
    "new task",
    "new feature",
    "new epic",
}


def _normalize_title_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def is_generic_create_title(title: object) -> bool:
    """Return whether a title is a non-descriptive create placeholder."""

    text = _normalize_title_text(title)
    if not text:
        return True
    lowered = text.lower()
    if lowered in _GENERIC_CREATE_TITLES:
        return True
    if lowered.startswith("create new ") and len(lowered) <= 48:
        return True
    if re.fullmatch(r"new\s+(pbi|bug|task|feature|epic|work item)", lowered):
        return True
    return False


def _title_from_source_quote(quote: object, *, max_len: int = 120) -> str | None:
    if not isinstance(quote, str):
        return None
    text = " ".join(quote.strip().split())
    if not text:
        return None
    # Prefer the first substantive sentence or clause.
    for part in re.split(r"(?<=[.!?])\s+|\s+[-–—]\s+", text):
        candidate = _normalize_title_text(part)
        if len(candidate) < 12:
            continue
        if len(candidate) > max_len:
            candidate = candidate[: max_len - 1].rstrip() + "…"
        return candidate
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text if len(text) >= 12 else None


def resolve_create_title(item: dict[str, Any]) -> str:
    """Resolve a descriptive title for a Create proposal."""

    field_updates = item.get("fieldUpdates")
    field_updates_dict = field_updates if isinstance(field_updates, dict) else {}

    candidates: list[str] = []
    for raw in (
        item.get("title"),
        field_updates_dict.get("System.Title"),
        (item.get("newWorkItem") or {}).get("title") if isinstance(item.get("newWorkItem"), dict) else None,
    ):
        text = _normalize_title_text(raw)
        if text and not is_generic_create_title(text):
            candidates.append(text)

    if candidates:
        return candidates[0]

    rationale = _normalize_title_text(item.get("rationale"))
    if rationale and rationale.lower() not in _PLACEHOLDER_RATIONALES:
        return rationale[:120]

    from_quote = _title_from_source_quote(item.get("sourceQuote"))
    if from_quote:
        return from_quote

    work_item_type = _normalize_title_text(item.get("workItemType")) or "PBI"
    return f"New {work_item_type} from meeting notes"


def parse_llm_output_debug(
    raw_text: str,
    meeting_title: str | None = None,
    meeting_date: date | None = None,
) -> tuple[dict[str, Any], LLMOutputSchema]:
    """Parse LLM output and return (normalized_json, parsed_schema)."""

    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM response JSON root must be an object")
    data = normalize_llm_output(data, meeting_title, meeting_date)
    parsed = LLMOutputSchema.model_validate(data)
    return data, parsed


_SCHEMA_PARSE_ERRORS = (ValidationError, json.JSONDecodeError, ValueError, TypeError)


async def parse_llm_output_with_repair(
    model: Any,
    messages: list[Any],
    raw_text: str,
    *,
    meeting_title: str | None,
    meeting_date: date | None,
    max_attempts: int,
) -> tuple[str, dict[str, Any], LLMOutputSchema]:
    """Parse LLM output, asking the model to repair schema/JSON errors as needed."""

    attempts = max(0, int(max_attempts))
    text = raw_text
    last_error: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            normalized, parsed = parse_llm_output_debug(
                text,
                meeting_title=meeting_title,
                meeting_date=meeting_date,
            )
            return text, normalized, parsed
        except _SCHEMA_PARSE_ERRORS as exc:
            last_error = exc
            if attempt >= attempts:
                break
            messages.append(repair_instruction(error=exc, previous_response=text))
            response = await model.ainvoke(messages)
            messages.append(response)
            text = str(response.content)

    assert last_error is not None
    raise last_error


def normalize_llm_output(
    data: dict[str, Any],
    meeting_title: str | None,
    meeting_date: date | None,
) -> dict[str, Any]:
    """Fill harmless missing top-level metadata from known meeting context."""

    def normalize_work_item_type(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        lowered = text.lower().replace("_", " ").replace("-", " ")
        lowered_compact = lowered.replace(" ", "")
        if lowered in {"pbi", "product backlog item"} or lowered_compact == "productbacklogitem":
            return "PBI"
        if lowered in {"feature"}:
            return "Feature"
        if lowered in {"epic"}:
            return "Epic"
        if lowered in {"bug"}:
            return "Bug"
        if lowered in {"task"}:
            return "Task"
        return None

    def normalize_change_type(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        lowered = text.lower().replace("_", " ").replace("-", " ")
        lowered_compact = lowered.replace(" ", "")
        mapping = {
            "create": "Create",
            "update": "Update",
            "statetransition": "StateTransition",
            "state transition": "StateTransition",
            "assign": "Assign",
            "comment": "Comment",
        }
        return mapping.get(lowered) or mapping.get(lowered_compact)

    def ensure_change_fields(item: dict[str, Any]) -> dict[str, Any]:
        # Key aliases
        if "changeType" not in item and "change_type" in item:
            item["changeType"] = item.pop("change_type")
        if "workItemType" not in item and "work_item_type" in item:
            item["workItemType"] = item.pop("work_item_type")

        normalized_change = normalize_change_type(item.get("changeType"))
        if normalized_change:
            item["changeType"] = normalized_change

        if "targetWorkItemId" not in item:
            for alt in ("workItemId", "work_item_id", "id"):
                if alt in item:
                    item["targetWorkItemId"] = item.pop(alt)
                    break

        if "commentText" not in item:
            for alt in ("comment", "text", "body"):
                if alt in item and isinstance(item.get(alt), str):
                    item["commentText"] = item.pop(alt)
                    break

        if "fieldUpdates" not in item and "field_updates" in item:
            item["fieldUpdates"] = item.pop("field_updates")

        change_type = str(item.get("changeType") or "").strip()

        # Create proposals sometimes come back as nested objects.
        new_item = item.get("newWorkItem")
        if change_type == "Create" and isinstance(new_item, dict):
            # Flatten title/type
            if not item.get("title") and isinstance(new_item.get("title"), str):
                item["title"] = new_item.get("title")

            # Flatten common fields into fieldUpdates
            field_updates = dict(item.get("fieldUpdates") or {})
            area_path = new_item.get("areaPath")
            if isinstance(area_path, str) and area_path and "System.AreaPath" not in field_updates:
                field_updates["System.AreaPath"] = area_path

            description = new_item.get("description")
            if isinstance(description, str) and description and "System.Description" not in field_updates:
                field_updates["System.Description"] = description

            ac = new_item.get("acceptanceCriteria")
            if isinstance(ac, str) and ac and "Microsoft.VSTS.Common.AcceptanceCriteria" not in field_updates:
                field_updates["Microsoft.VSTS.Common.AcceptanceCriteria"] = ac

            effort = new_item.get("effort")
            if effort is not None and "Microsoft.VSTS.Scheduling.Effort" not in field_updates:
                field_updates["Microsoft.VSTS.Scheduling.Effort"] = effort

            story_points = new_item.get("storyPoints")
            if story_points is not None and "Microsoft.VSTS.Scheduling.StoryPoints" not in field_updates:
                field_updates["Microsoft.VSTS.Scheduling.StoryPoints"] = story_points

            if field_updates and item.get("fieldUpdates") != field_updates:
                item["fieldUpdates"] = field_updates

            # Assignee
            if not item.get("newAssignee") and new_item.get("assignedTo") is not None:
                item["newAssignee"] = new_item.get("assignedTo")

            # Parent
            if not item.get("parentWorkItemId") and new_item.get("parentWorkItemId") is not None:
                item["parentWorkItemId"] = new_item.get("parentWorkItemId")

        # Coerce ADO-native / alias work item type names onto the schema enum.
        raw_type = item.get("workItemType")
        inferred_type = normalize_work_item_type(raw_type)
        if inferred_type:
            item["workItemType"] = inferred_type
        elif not raw_type and change_type == "Create" and isinstance(new_item, dict):
            inferred_type = normalize_work_item_type(new_item.get("type"))
            if inferred_type:
                item["workItemType"] = inferred_type

        # Defaults for required fields so schema validation is resilient.
        if not item.get("workItemType"):
            # workItemType is not used for non-create API calls; default is safe.
            item["workItemType"] = "PBI"

        if change_type == "Create":
            resolved_title = resolve_create_title(item)
            item["title"] = resolved_title
            field_updates = dict(item.get("fieldUpdates") or {})
            existing_title = _normalize_title_text(field_updates.get("System.Title"))
            if not existing_title or is_generic_create_title(existing_title):
                field_updates["System.Title"] = resolved_title
            item["fieldUpdates"] = field_updates
        elif not item.get("title"):
            target = item.get("targetWorkItemId")
            if target is not None:
                item["title"] = f"{change_type or 'Update'} on #{target}"
            else:
                item["title"] = change_type or "Proposal"

        if not item.get("rationale"):
            item["rationale"] = "Captured from meeting notes for review and tracking."

        # Ensure confidenceScore is present.
        if "confidenceScore" not in item:
            item["confidenceScore"] = 60

        return item

    # Some models occasionally return close-but-not-exact keys. Normalize common variants
    # so ingestion doesn't fail on minor schema drift.
    if "proposedChanges" not in data and "proposed_changes" not in data:
        for alt in ("changes", "proposals", "items"):
            if alt in data:
                data["proposedChanges"] = data.pop(alt)
                break
    if "proposedChanges" not in data and "proposed_changes" not in data:
        data["proposedChanges"] = []

    if "unmatchedDiscussion" not in data and "unmatched_discussion" not in data:
        for alt in ("unmatched", "unmatchedTopics", "unmatched_topics", "unmatchedDiscussions"):
            if alt in data:
                data["unmatchedDiscussion"] = data.pop(alt)
                break
    if "unmatchedDiscussion" not in data and "unmatched_discussion" not in data:
        data["unmatchedDiscussion"] = []

    def is_meaningful_comment_text(value: object) -> bool:
        if not isinstance(value, str):
            return False
        text = " ".join(value.strip().split())
        if not text:
            return False
        lowered = text.lower()
        # Never allow automation/meta watermarks into ADO comments.
        if "agentic scrum" in lowered:
            return False
        if "approved by" in lowered:
            return False
        if "meeting notes" in lowered or "transcript" in lowered:
            return False
        # Hard-block known placeholders / non-informational patterns.
        if "no details provided" in lowered:
            return False
        if lowered.startswith("captured meeting discussion"):
            return False
        if lowered in {
            "captured from meeting notes for review and tracking.",
            "captured from meeting notes.",
            "captured for review and tracking.",
            "captured meeting discussion.",
        }:
            return False
        # Require a minimal amount of substance.
        words = [w for w in re.findall(r"[a-zA-Z]{3,}", text)]
        if len(words) < 10:
            return False
        if len(set(w.lower() for w in words)) < 8:
            return False
        return True

    # Normalize individual proposed changes.
    proposed = data.get("proposedChanges") or data.get("proposed_changes") or []
    if isinstance(proposed, list):
        normalized: list[dict[str, Any]] = []
        unmatched = data.get("unmatchedDiscussion") or data.get("unmatched_discussion") or []
        if not isinstance(unmatched, list):
            unmatched = []
        for item in proposed:
            if isinstance(item, dict):
                candidate = ensure_change_fields(dict(item))
                change_type = str(candidate.get("changeType") or "").strip()
                if change_type == "Comment" and not is_meaningful_comment_text(
                    candidate.get("commentText")
                ):
                    # Skip non-informational comments rather than spamming ADO.
                    topic = str(candidate.get("title") or "").strip() or "Unclear comment"
                    rationale = (
                        str(candidate.get("rationale") or "").strip()
                        or "Insufficient concrete detail to post a useful ADO comment."
                    )
                    unmatched.append({"topic": topic, "rationale": rationale})
                    continue
                normalized.append(candidate)
        data["proposedChanges"] = normalized
        data["unmatchedDiscussion"] = unmatched

    if meeting_title and not data.get("sourceMeeting"):
        data["sourceMeeting"] = meeting_title
    if meeting_date and not data.get("sourceMeetingDate"):
        data["sourceMeetingDate"] = meeting_date.isoformat()
    if not data.get("processedAt"):
        data["processedAt"] = datetime.now(timezone.utc).isoformat()
    if "sourceLoopUrl" not in data:
        data["sourceLoopUrl"] = None

    def normalize_unmatched_item(item: object) -> dict[str, Any] | None:
        if isinstance(item, str):
            topic = item.strip()
            if not topic:
                return None
            return {"topic": topic, "rationale": "No actionable ADO change was identified."}
        if not isinstance(item, dict):
            return None
        candidate = dict(item)

        topic = candidate.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            for alt in ("summary", "topicSummary", "topic_summary", "title", "name"):
                alt_val = candidate.get(alt)
                if isinstance(alt_val, str) and alt_val.strip():
                    candidate["topic"] = alt_val.strip()
                    break
        topic = candidate.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            candidate["topic"] = "Unmatched discussion"

        rationale = candidate.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            for alt in ("reason", "why", "notes", "rationaleText", "details"):
                alt_val = candidate.get(alt)
                if isinstance(alt_val, str) and alt_val.strip():
                    candidate["rationale"] = alt_val.strip()
                    break
        rationale = candidate.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            candidate["rationale"] = "No actionable ADO change was identified."

        return candidate

    unmatched = data.get("unmatchedDiscussion") or data.get("unmatched_discussion") or []
    if isinstance(unmatched, list):
        normalized_unmatched: list[dict[str, Any]] = []
        for item in unmatched:
            candidate = normalize_unmatched_item(item)
            if candidate:
                normalized_unmatched.append(candidate)
        data["unmatchedDiscussion"] = normalized_unmatched
    else:
        data["unmatchedDiscussion"] = []
    return data


def repair_instruction(
    *,
    error: Exception | str,
    previous_response: str = "",
) -> HumanMessage:
    """Return a message requesting JSON repair for a specific parse/validation error."""

    previous = (previous_response or "").strip()
    if len(previous) > 12000:
        previous = previous[:12000] + "\n…[truncated]"
    # Avoid str.format: pydantic errors often contain curly braces.
    content = JSON_REPAIR_PROMPT.replace("__ERROR__", str(error)).replace(
        "__PREVIOUS_RESPONSE__", previous or "(empty)"
    )
    return HumanMessage(content=content)
