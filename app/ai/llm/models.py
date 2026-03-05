from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import AzureChatOpenAI
from app.core.config import settings
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain_openai import ChatOpenAI

gemini_3_pro = ChatGoogleGenerativeAI(
    model="gemini-3-pro-preview",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=20,
    api_key=settings.google_api_key,
)


gemini_3_flash_preview = ChatGoogleGenerativeAI(
    model="gemini-3-flash-preview",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=20,
    api_key=settings.google_api_key,
)

gemini_flash_latest = ChatGoogleGenerativeAI(
    model="gemini-flash-latest",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
    api_key=settings.google_api_key,
)

gemini_2_5_pro = (
    ChatGoogleGenerativeAI(  ## It is very bad in generation following the structure
        model="gemini-2.5-pro",
        temperature=0,
        max_tokens=None,
        timeout=None,
        max_retries=2,
        api_key=settings.google_api_key,
    )
)

# gpt_5_1 = AzureChatOpenAI(  ## It miss following instructions
#     azure_endpoint=settings.azure_endpoint,
#     api_key=settings.azure_api_key,
#     api_version="2025-04-01-preview",
#     azure_deployment="gpt-5.1-chat",
#     name="gpt-5.1-chat",
#     temperature=1,
# )

glm5 = ChatNVIDIA(
  model="z-ai/glm5",
  api_key=settings.nvidia_api_key,
  temperature=1,
  top_p=1,
  max_tokens=16384,
  extra_body={"chat_template_kwargs":{"enable_thinking":True,"clear_thinking":False}},
)


minimax_m2_5 = ChatNVIDIA(
  model="minimaxai/minimax-m2.5",
  api_key=settings.nvidia_api_key, 
  temperature=1,
  top_p=0.95,
  max_tokens=8192,
)

gpt5 = ChatOpenAI(
    model="gpt-5",
    base_url="https://models.github.ai/inference",
    api_key=settings.github_token,
    # stream_usage=True,
    # temperature=None,
    # max_tokens=None,
    # timeout=None,
    # reasoning_effort="low",
    # max_retries=2,
    # api_key="...",  # If you prefer to pass api key in directly
    # base_url="...",
    # organization="...",
    # other params...
)



if __name__ == "__main__":
    for chunk in gpt5.stream([{"role":"user","content":"Hi"}]):
  
        if chunk.additional_kwargs and "reasoning_content" in chunk.additional_kwargs:
            print(chunk.additional_kwargs["reasoning_content"], end="")
        
        print(chunk.content, end="")