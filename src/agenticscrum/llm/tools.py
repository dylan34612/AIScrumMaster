"""LangChain tools backed by live Azure DevOps APIs."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agenticscrum.ado.client import AdoClient


def build_ado_tools(ado: AdoClient) -> list[BaseTool]:
    """Create Azure DevOps tools the LLM can call during analysis."""

    async def ado_query_active_ids() -> list[int]:
        """Return active Azure DevOps work item IDs for the configured team."""

        return await ado.query_active_ids()

    async def ado_batch_get(ids: list[int]) -> list[dict[str, Any]]:
        """Fetch Azure DevOps work item details for a list of IDs."""

        return await ado.batch_get(ids)

    async def ado_get_work_item(work_item_id: int) -> dict[str, Any]:
        """Fetch full Azure DevOps details for one work item."""

        return await ado.get_work_item(work_item_id)

    async def ado_get_work_item_comments(work_item_id: int) -> list[dict[str, Any]]:
        """Fetch comments for one Azure DevOps work item."""

        return await ado.get_comments(work_item_id)

    async def ado_get_work_item_type_states(work_item_type: str) -> list[dict[str, Any]]:
        """List allowed state values for a work item type."""

        return await ado.get_work_item_type_states(work_item_type)

    return [
        StructuredTool.from_function(
            coroutine=ado_query_active_ids,
            name="ado_query_active_ids",
            description="Return active Azure DevOps work item IDs for the configured team.",
        ),
        StructuredTool.from_function(
            coroutine=ado_batch_get,
            name="ado_batch_get",
            description="Fetch Azure DevOps work item details for a list of IDs.",
        ),
        StructuredTool.from_function(
            coroutine=ado_get_work_item,
            name="ado_get_work_item",
            description="Fetch full Azure DevOps details for one work item ID.",
        ),
        StructuredTool.from_function(
            coroutine=ado_get_work_item_comments,
            name="ado_get_work_item_comments",
            description="Fetch comments for one Azure DevOps work item ID.",
        ),
        StructuredTool.from_function(
            coroutine=ado_get_work_item_type_states,
            name="ado_get_work_item_type_states",
            description="List allowed state values for an Azure DevOps work item type (e.g., PBI, Feature, Epic).",
        ),
    ]
