# Model
LLM_URL = "http://localhost:8000/v1/chat/completions"
LLM_MODEL = "your-model"
LLM_TIMEOUT = 120  # secondes

# Agent
MAX_ITERATIONS = 10
SYSTEM_PROMPT = "Tu es un assistant spécialisé en CAO. "

# Serveur MCP
MCP_COMMAND = "solidcraft-cli"
MCP_ADDRESS = "127.0.0.1:7878"
MCP_ALLOWED_PATH = "/path/to/your/cad-project"

# Compaction
# Serialized payload size in characters, including tool schemas. Oversized requests
# are compacted before each LLM call; if they still exceed this budget, they stop.
CONTEXT_COMPACTION_THRESHOLD = 50000

#RESTRICT
BLOCKED_TOOLS = {
    "new_design",
}
BLOCKED_COMMANDS = {
    "file.new",
    "file.open",
    "file.close"
}
