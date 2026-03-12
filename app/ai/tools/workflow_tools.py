from langchain.tools import tool
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
        messages=[("end", f"The PRD has been saved in {feature_path} and the PRD generation process is complete.")]
    )
