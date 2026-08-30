from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Singapore")
RUN_HOUR = 8

CORE_TOPIC_HIGH_WEIGHT_KEYWORDS = [
    "PostgreSQL",
    "SQLite",
    "MySQL",
    "MariaDB",
    "Redis",
    "MongoDB",
    "ClickHouse",
    "DuckDB",
    "Apache Cassandra",
    "Elasticsearch",
    "etcd",
    "TypeScript",
    "JavaScript",
    "Haskell",
    "OCaml",
    "Elixir language",
    "Erlang",
    "Kotlin",
    "Clojure",
    "WebAssembly",
    "WASM",
    "JVM",
    "LLVM",
    "GCC compiler",
    "GCC compilers",
    "Clang compiler",
    "Clang compilers",
    "Linux kernel",
    "Linux kernels",
    "FreeBSD",
    "OpenBSD",
    "NetBSD",
    "eBPF",
    "systemd",
    "Kubernetes",
    "containerd",
    "QEMU",
    "RISC-V",
    "ARM64",
    "x86",
    "NixOS",
    "HashiCorp Terraform",
    "OpenSSL",
    "WireGuard",
    "zero-knowledge proof",
    "zero-knowledge proofs",
    "post-quantum cryptography",
    "TLS",
    "AES",
    "SHA-256",
    "QUIC",
    "WebRTC",
    "IPv6",
    "HTTP/3",
    "DNS",
    "bytecode interpreter",
    "bytecode interpreters",
    "memory garbage collection",
    "type system",
    "type systems",
    "borrow checker",
    "borrow checkers",
    "formal verification",
    "Golang",
    "goroutine",
    "goroutines",
    "Rustlang",
    "SwiftUI",
    "Swift Package Manager",
    "C++",
    "C11",
    "C23",
    "glibc",
    "libc",
    "kernel panic",
    "kernel panics",
    "kernel module",
    "kernel modules",
    "shell script",
    "shell scripts",
    "POSIX shell",
    "POSIX shells",
    "Bash script",
    "Bash scripts",
    "CPython",
    "PyPI",
    "asyncio",
    "Ruby on Rails",
    "RubyGems",
    "JDK",
    "JDKs",
    "nixpkgs",
    "Dockerfile",
    "Dockerfiles",
    "Docker container",
    "Docker containers",
    "compiler optimization",
    "compiler optimizations",
    "applied cryptography",
]

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
    "GPT",
    "Codex",
    "Qwen",
    "Kimi",
    "Grok",
    "DeepSeek",
    "Fable",
    "Moonshot",
    "Gemini",
    "open weights",
    "open-weights",
    *CORE_TOPIC_HIGH_WEIGHT_KEYWORDS,
]

MEDIUM_HIGH_WEIGHT_KEYWORDS = [
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
CASE_SENSITIVE_KEYWORDS = {
    "GPT",
    "Codex",
    "Qwen",
    "Kimi",
    "Grok",
    "DeepSeek",
    "Fable",
    "Moonshot",
}

AI_MAX_ITEMS = 5
AI_MIN_SCORE = 6.0
AI_MIN_POINTS = 10
NON_AI_MAX_ITEMS = 2
NON_AI_POINTS_THRESHOLD = 300
NON_AI_COMMENTS_THRESHOLD = 150
EXPLORATION_CLASSIFIER_MAX_CANDIDATES = 25
ARTICLE_EVIDENCE_BONUS = 4.0
HIGH_WEIGHT_BONUS_CAP = 6.0
MEDIUM_HIGH_WEIGHT_BONUS_CAP = 5.0
MEDIUM_WEIGHT_BONUS_CAP = 3.0
LOW_WEIGHT_BONUS_CAP = 1.0
KEYWORD_BONUS_CAP = 5.0
TOPIC_BONUS_CAP = 1.0
TOPIC_KEYWORDS = {
    "AI coding",
    "coding agent",
    "AI agent",
    "AI developer tools",
    "AI workflow",
    "AI productivity",
    "AI automation",
}
