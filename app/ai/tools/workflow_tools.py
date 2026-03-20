from langchain.tools import tool, ToolRuntime
from langgraph.types import Command
from ag_ui_langgraph.agent import ToolMessage
from langgraph.types import Command

@tool
def assign_prd_saving_completed(feature_path: str) -> str:
    """
    Assign PRD Saving Completed: Marks the PRD generator process as done and informs other workflow nodes.

    Args:
        feature_path (str): The path to the saved PRD file in this format: .actovator/features/[feature-name]/prd.md

    Returns:
        str: A confirmation message indicating where the PRD was saved.
    """
    return Command(
        messages=[
            (
                "end",
                f"The PRD has been saved in {feature_path} and the PRD generation process is complete.",
            )
        ]
    )


@tool
def start_verification_process(
    user_task: str, execution_result: str, runtime: ToolRuntime
) -> dict:
    """
    Routes inputs to the Verification Agent for post-execution validation.

    Call this tool immediately after the Code Editor Agent (You) completes your task.
    It packages the original task and the agent's execution result so the Verification Agent can independently confirm whether the changes were applied correctly.

    Args:
        user_task (str): The original task given to the Code Editor Agent (You). Used by the Verification Agent to derive expected state.
        execution_result (str): The full output produced by the Code Editor Agent (You). Used as the source of claims to verify against actual system state.

    Routes to verification agent while updating state.
    """

    return Command(
        update={
            "user_task": user_task,
            "execution_result": execution_result.strip(),
            "messages": [
                ToolMessage(
                    content="Routed to verification agent",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        },
        goto="verification_node",
        graph=Command.PARENT,
    )
