from langchain.chat_models import init_chat_model
from typing import Literal, Optional
from pydantic import BaseModel

from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER
from langchain_openrouter import ChatOpenRouter
from app.core.config import settings

model = ChatOpenRouter(
    model="minimax/minimax-m2.5",
    temperature=0,
    max_retries=7,
    api_key=settings.openrouter_api_key,
)

# Add explicit list of supported providers and model IDs for completion
Provider = Literal["google_genai",]

ModelId = Literal[
    "gemini-3.1-pro-previewgemini-3-pro-preview",
    "gemini-3-flash-preview",  # It's hallucianite
    "gemini-pro-latest",
]


class _Model(BaseModel):
    model_id: ModelId = DEFAULT_MODEL_ID
    provider: Provider = DEFAULT_MODEL_PROVIDER


def build_model(
    *, model_id: ModelId = DEFAULT_MODEL_ID, provider: Provider = DEFAULT_MODEL_PROVIDER
):
    # Validate input using Pydantic
    conf = _Model(model_id=model_id, provider=provider)
    return init_chat_model(
        model=conf.model_id, model_provider=conf.provider, temperature=0
    )


class State(BaseModel):
    model_provider: Optional[str] = None
    model_id: Optional[str] = None


def build_model_from_state(state: State):
    model_provider = state.get("model_provider", DEFAULT_MODEL_PROVIDER)
    model_id = state.get("model_id", DEFAULT_MODEL_ID)
    # model = build_model(provider=model_provider, model_id=model_id)
    # return model
    return model
