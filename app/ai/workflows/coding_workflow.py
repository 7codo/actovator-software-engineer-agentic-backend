from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph, MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langchain_core.prompts import PromptTemplate
from langchain.agents import create_agent

from app.ai.tools.mcp_tools import execute_specific_tool, filtered_tools
from app.ai.tools.sandbox_tools import build_sandbox_tools
from app.ai.prompts import (
    EDITING_PROMPT,
    MINI_DOCS_CREATION_PROMPT,
    PlANNER_PROMPT,
    RESEARCH_PROMPT,
)
from app.ai.utils import build_model, ModelId, Provider
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER, PROJECT_PATH
from ai.tools.models_tools import get_coding_agent_known_package_version
from ai.tools.changelogs_tools import build_changelog_tools
from .testing_workflow import testing_graph


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State(MessagesState):
    sandbox_id: str
    user_story: str
    node_names: list[str] = []
    model_provider: str | None = None
    model_id: str | None = None
    mini_docs: str | None = None
    research_report: str | None = None
    plan: str | None = None
    coding_messages: list[BaseMessage] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filter_out_last_human_message(messages: list[BaseMessage]) -> list[BaseMessage]:
    last_human_idx = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        None,
    )
    if last_human_idx is None:
        return messages
    return messages[:last_human_idx] + messages[last_human_idx + 1:]

def _build_model_from_state(state: State, model_provider: Provider | None = None, model_id: ModelId | None = None):
    return build_model(
        provider=model_provider or state.get("model_provider", DEFAULT_MODEL_PROVIDER),
        model_id=model_id or state.get("model_id", DEFAULT_MODEL_ID),
    )


def _require_sandbox_id(state: State, step_name: str) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError(f"sandbox_id is required for {step_name}")
    return sandbox_id


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

async def init_step(state: State) -> dict:
    user_story = state.get("user_story")
    print("user_story", user_story)
    return {"sandbox_id": state.get("sandbox_id")}


async def mini_docs_creation_step(state: State) -> dict:
    sandbox_id = _require_sandbox_id(state, "mini_docs_creation_step")
    user_story = state.get("user_story")
    model = _build_model_from_state(state)

    sandbox_tools = build_sandbox_tools(sandbox_id)
    packages = await sandbox_tools["read_file"](path=f"{PROJECT_PATH}/package.json")

    changelog_tools = build_changelog_tools(sandbox_id)
    system_message = PromptTemplate.from_template(MINI_DOCS_CREATION_PROMPT).format(
        packages=packages,
    )

    docs_agent = create_agent(
        system_prompt=system_message,
        tools=[
            changelog_tools["search_changelogs"],
            get_coding_agent_known_package_version,
        ],
        model=model,
    )

    result = await docs_agent.ainvoke({"messages": [HumanMessage(content=user_story)]})
    mini_docs = result["messages"][-1].content
    updated_messages = _filter_out_last_human_message(result["messages"])
    return {
        "messages": updated_messages,
        "mini_docs": mini_docs,
        "node_names": [state.get("node_names", []), "Auditer Agent"]
        
    }


async def codebase_research_step(state: State) -> dict:
    sandbox_id = _require_sandbox_id(state, "codebase_research_step")
    user_story = state.get("user_story")

    # Fix: use _build_model_from_state instead of hardcoding the model id
    model = _build_model_from_state(state) # gemeni 3 pro follow instructions better than the lite or fast version # model_id="gemini-3-pro-preview", model_provider="google_genai"

    sandbox_tools = build_sandbox_tools(sandbox_id)
    packages = await sandbox_tools["read_file"](path=f"{PROJECT_PATH}/package.json")
    tech_stack = await sandbox_tools["read_file"](
        path=f"{PROJECT_PATH}/.actovator/features/tech_stack.json"
    )

    system_message = PromptTemplate.from_template(RESEARCH_PROMPT).format(
        packages=packages,
        tech_stack=tech_stack,
    )

    tools_result = await filtered_tools(
        sandbox_id,
        allowed_tools=[
            "find_referencing_symbols",
            "find_symbol",
            "get_symbols_overview",
            "find_file",
            "list_dir",
            "search_for_pattern",
        ],
    )

    research_agent = create_agent(
        system_prompt=system_message,
        tools=tools_result.tools,
        model=model,
    )

    result = await research_agent.ainvoke(
        {"messages": [HumanMessage(content=user_story)]}
    )
    updated_messages = _filter_out_last_human_message(result["messages"])
    return {
        "messages": updated_messages,
        "research_report": result["messages"][-1].content,
        "node_names": [state.get("node_names", []), "Research Agent"]
    }


async def planner_step(state: State) -> dict:
    user_story = state.get("user_story")
    
    model = _build_model_from_state(state)

    context = SystemMessage(
        content=f"{state.get('mini_docs', '')}\n\n{state.get('research_report', '')}"
    )

    response = await model.ainvoke([
        SystemMessage(content=PlANNER_PROMPT),
        context,
        HumanMessage(content=user_story),
    ])

    return {
        "messages": [*state.get("messages", []), response],
        "plan": response.content,
        "node_names": [state.get("node_names", []), "Planner Agent"]
    }


async def editing_step(state: State) -> dict:
    # Fix: use helper instead of direct attribute access
    sandbox_id = _require_sandbox_id(state, "editing_step")
    user_story = """{"id":"file-upload-system::US-003","name":"Implement Client-Side File Validation","column":"planned","featureName":"file-upload-system","storyId":"US-003","priority":3,"description":"As a user, I want the system to validate my files immediately so I don't waste time uploading invalid ones.","passes":false,"acceptanceCriteria":["Restrict files to a configurable limit of 10MB.","Restrict file types to .jpg, .png, and .pdf.","Rejected files trigger an immediate inline error message or toast notification.","Valid files are added to the queue while invalid ones are ignored."]}"""
    plan = """## Edit Plan

### Reconciliation Notes
- **React 19 Compatibility**: The Investigation Report implies standard component creation. I have ensured that `forwardRef` is avoided and any `useRef` calls utilize the mandatory initial argument (e.g., `null`) as per React 19 Mini Docs.
- **Lucide Icon Renaming**: The Investigation Report correctly identified `CloudUpload`, which aligns with the Mini Docs (renamed from `UploadCloud`).

---

### Edit Sequence

#### 1. MODIFY `package.json`
**Reason:** Add the required dependency for drag-and-drop functionality as specified in the acceptance criteria.
**Instructions:**
- Add `"react-dropzone": "^14.3.5"` to the `dependencies` object.

#### 2. CREATE `src/components/file-upload/drop-zone.tsx`
**Reason:** Implement the central UI component for file selection and drag-and-drop interactions.
**Instructions:**
- Import `useDropzone` from `react-dropzone`.
- Import `CloudUpload` from `lucide-react`.
- Define a `DropZoneProps` interface with an `onFilesAdded` callback function that receives `File[]`.
- Implement the component using the `useDropzone` hook.
- Destructure `getRootProps`, `getInputProps`, and `isDragActive` from the hook.
- Return a `div` that spreads `getRootProps()`. 
- Apply Tailwind classes for a dashed border (`border-2 border-dashed`), rounded corners, and padding.
- Implement visual feedback: change border and background color (e.g., `border-primary bg-secondary/50`) when `isDragActive` is true.
- Inside the div, include a hidden `input` spreading `getInputProps()`.
- Inside the div, render the `CloudUpload` icon and the call-to-action text: "Drag & drop files here, or click to select".
- Use the `onDrop` callback in `useDropzone` to trigger the `onFilesAdded` prop.

#### 3. MODIFY `src/app/page.tsx`
**Reason:** Integrate the `DropZone` component into the main application page and manage the local file state queue.
**Instructions:**
- Import `useState` from `react`.
- Import the `DropZone` component from `@/components/file-upload/drop-zone`.
- Define a state variable `files` (initially an empty array `[]`) using `useState<File[]>`.
- Create a handler function `handleFilesAdded` that takes `newFiles: File[]` and updates the state by appending them to the existing queue: `setFiles((prev) => [...prev, ...newFiles])`.
- In the JSX, render the `DropZone` component within a container, passing `handleFilesAdded` to the `onFilesAdded` prop.
- (Optional) Render a simple list of file names below the drop zone to verify the "local state queue" is working."""
    model = _build_model_from_state(state)

    tools_result = await filtered_tools(
        sandbox_id,
        # Fix: typo execluded_tools → excluded_tools
        excluded_tools=[
            "search_for_pattern",
            "active_language_server",
            "restart_language_server",
            "execute_shell_command",
        ],
    )

    editor_agent = create_agent(
        system_prompt=EDITING_PROMPT,
        tools=tools_result.tools,
        model=model,
    )

    result = await editor_agent.ainvoke([
        HumanMessage(
            content=f"{user_story}\n\nPlan:\n{plan}"
        ),
    ])
    updated_messages = _filter_out_last_human_message(result["messages"])
    return {
        "messages": updated_messages,
        "coding_messages": result["messages"],
        "node_names": [state.get("node_names", []), "Code Editor Agent"]
    }


async def testing_step(state: State) -> dict:
    messages = state.get("coding_messages", [])

    result = await testing_graph.ainvoke([
        *messages,
        HumanMessage(content=state.get("user_story")),
    ])
    updated_messages = _filter_out_last_human_message(result["messages"])
    return {"messages": updated_messages, "node_names": [state.get("node_names", []), "Tester Agent"]}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

coding_workflow = StateGraph(State)

coding_workflow.add_node("init_step", init_step)
coding_workflow.add_node("mini_docs_creation_step", mini_docs_creation_step)
coding_workflow.add_node("codebase_research_step", codebase_research_step)
coding_workflow.add_node("planner_step", planner_step)
coding_workflow.add_node("editing_step", editing_step)
coding_workflow.add_node("testing_step", testing_step)  # Fix: was defined but never added

# init → parallel branches
coding_workflow.add_edge(START, "init_step")
# coding_workflow.add_edge("init_step", "mini_docs_creation_step")
# coding_workflow.add_edge("init_step", "codebase_research_step")

# parallel branches → join at planner (LangGraph fan-in: planner waits for both)
# coding_workflow.add_edge("mini_docs_creation_step", "planner_step")
# coding_workflow.add_edge("codebase_research_step", "planner_step") # codebase_research_step takes longer than mini_docs_creation_step in the most cases

# planner → editing → testing → done
coding_workflow.add_edge("init_step", "editing_step")
# coding_workflow.add_edge("editing_step", "testing_step")
coding_workflow.add_edge("editing_step", END)

checkpointer = InMemorySaver()
coding_graph = coding_workflow.compile(checkpointer=checkpointer)