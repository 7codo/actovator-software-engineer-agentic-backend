from langchain.chat_models import init_chat_model
from typing import Literal
from pydantic import BaseModel

from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER

# Add explicit list of supported providers and model IDs for completion
Provider = Literal[
    "google_genai",
   
]

ModelId = Literal[
    "gemini-3-flash-preview",
   
]

class _Model(BaseModel):
    model_id: ModelId = DEFAULT_MODEL_ID
    provider: Provider = DEFAULT_MODEL_PROVIDER

def build_model(
    *, 
    model_id: ModelId = DEFAULT_MODEL_ID, 
    provider: Provider = DEFAULT_MODEL_PROVIDER
):
    # Validate input using Pydantic
    conf = _Model(model_id=model_id, provider=provider)
    return init_chat_model(f"{conf.provider}:{conf.model_id}")

