"""
Azure OpenAI LLM client factory.

Returns a configured AzureChatOpenAI instance. Called once per node invocation
so the client is always fresh — no stale connection state between graph runs.
"""

from langchain_openai import AzureChatOpenAI

from backend.config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_API_VERSION,
    AZURE_OPENAI_CHAT_DEPLOYMENT,
)


def get_chat_llm(temperature: float = 0.2) -> AzureChatOpenAI:
    """
    Return a configured AzureChatOpenAI client.

    temperature=0.2: low but not zero — gives the LLM slight flexibility
    in phrasing its reasoning while keeping strategy decisions consistent.
    Set to 0.0 for fully deterministic output during testing.
    """
    return AzureChatOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_deployment=AZURE_OPENAI_CHAT_DEPLOYMENT,
        api_key=AZURE_OPENAI_API_KEY,
        api_version=AZURE_OPENAI_API_VERSION,
        temperature=temperature,
    )
