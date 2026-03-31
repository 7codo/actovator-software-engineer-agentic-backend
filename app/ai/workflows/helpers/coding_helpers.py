import re
import json
from langchain.agents import create_agent
from typing import Optional
from pydantic import BaseModel
from app.ai.llm.models import build_model
from langgraph.graph.message import MessagesState
from langchain_core.messages import HumanMessage, BaseMessage
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER


READ_ONLY_TOOLS = [
    "read_file",
    "list_dir",
    "find_file",
    "search_for_pattern",
    "get_symbols_overview",
    "find_symbol",
    "find_referencing_symbols",
]

WRITE_TOOLS = [
    "create_text_file",
    "replace_content",
    "delete_lines",
    "replace_lines",
    "insert_at_line",
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "rename_symbol",
]

GIT_TOOLS = [
    "get_symbols_overview",
    "find_symbol",
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "rename_symbol",
]


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def extract_json_content(message: BaseMessage) -> str:
    content = message.content

    # Normalize multi-part provider responses (e.g. Gemini)
    if isinstance(content, list):
        content = next(
            (block["text"] for block in content if block.get("type") == "text"),
            "",
        )

    content = content.strip()

    # Priority 1: markdown fence — validate before returning
    match = _JSON_FENCE_RE.search(content)
    if match:
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass  # fence content wasn't valid JSON — fall through

    # Priority 2: try the whole string directly
    try:
        json.loads(content)
        return content
    except json.JSONDecodeError:
        pass

    # Priority 3: scan for every '{' and try to parse outward from it.
    # Prefer the longest valid match (outermost object wins over a nested one).
    best: str | None = None
    for start in (i for i, ch in enumerate(content) if ch == "{"):
        # Walk backwards from the end to find the matching close brace
        for end in range(len(content), start, -1):
            if content[end - 1] != "}":
                continue
            candidate = content[start:end]
            try:
                json.loads(candidate)
                if best is None or len(candidate) > len(best):
                    best = candidate
                break  # longest match from this start found; try the next '{'
            except json.JSONDecodeError:
                continue

    if best is not None:
        return best

    # Nothing parsed — return raw content and let the caller surface the error
    return content


class AgentState(MessagesState):
    sandbox_id: str
    model_id: Optional[str]
    model_provider: Optional[str]
    retry_count: int = 0
    verification_report: Optional[str] = None
    context_report: Optional[str] = None
    e2e_testing_report: Optional[str] = None
    user_message: Optional[HumanMessage] = None
    executor_report: Optional[str] = None


async def _run_subagent(
    *,
    state: AgentState,
    agent_name: str,
    system_prompt: str,
    messages: list[BaseMessage] = [],
    structured_output: Optional[BaseModel] = None,
    tools: list,
) -> dict:
    model = build_model(
        model_id=state.get("model_id") or DEFAULT_MODEL_ID,
        provider=state.get("model_provider") or DEFAULT_MODEL_PROVIDER,
    )

    if structured_output:
        agent = create_agent(
            model,
            system_prompt=system_prompt,
            tools=tools,
            response_format=structured_output,
            name=agent_name,
        )
    else:
        agent = create_agent(
            model,
            system_prompt=system_prompt,
            tools=tools,
            name=agent_name,
        )

    result = await agent.ainvoke({"messages": messages})

    return result
