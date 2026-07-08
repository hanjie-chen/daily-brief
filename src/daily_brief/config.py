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

AI_MAX_ITEMS = 5
AI_MIN_SCORE = 6.0
AI_MIN_POINTS = 10
NON_AI_MAX_ITEMS = 1
NON_AI_POINTS_THRESHOLD = 300
NON_AI_COMMENTS_THRESHOLD = 150
HIGH_WEIGHT_BONUS_CAP = 6.0
MEDIUM_HIGH_WEIGHT_BONUS_CAP = 5.0
MEDIUM_WEIGHT_BONUS_CAP = 3.0
LOW_WEIGHT_BONUS_CAP = 1.0
