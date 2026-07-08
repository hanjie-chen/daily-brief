from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Singapore")
RUN_HOUR = 8

HIGH_WEIGHT_KEYWORDS = [
    "AI coding",
    "coding agent",
    "AI agent",
    "LLM",
    "Claude",
    "OpenAI",
    "Anthropic",
    "ChatGPT",
    "Cursor",
    "Copilot",
    "MCP",
    "RAG",
    "AI developer tools",
]

MEDIUM_HIGH_WEIGHT_KEYWORDS = [
    "Gemini",
    "Google AI",
    "Meta AI",
    "xAI",
    "Mistral",
    "Perplexity",
    "AI workflow",
    "AI productivity",
    "assistant",
    "chatbot",
    "AI app",
    "AI tool",
    "AI automation",
]

MEDIUM_WEIGHT_KEYWORDS = [
    "AI",
    "inference",
    "fine-tuning",
    "eval",
    "AI benchmark",
    "LLM benchmark",
    "GPU",
    "embedding",
    "vector database",
]

WEAK_KEYWORDS = [
    "agent",
    "agents",
    "model",
    "workflow",
    "automation",
    "productivity",
    "training",
    "benchmark",
    "developer tools",
]

LOW_WEIGHT_KEYWORDS = [
    "funding",
    "acquisition",
    "regulation",
    "lawsuit",
]

ABBREVIATIONS = {"AI", "LLM", "RAG", "MCP", "GPU"}
