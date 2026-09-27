import os

from langchain_openai import ChatOpenAI


def get_llm() -> ChatOpenAI:
    # Anything that speaks the OpenAI chat API works here: OpenAI, Ollama,
    # vLLM, LM Studio, Groq... Defaults point at a local Ollama.
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.getenv("LLM_API_KEY", "ollama"),
        temperature=0,
        timeout=180,
    )
