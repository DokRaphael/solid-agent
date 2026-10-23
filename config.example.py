"""Copier ce fichier vers config.py, puis adapter les valeurs à votre machine."""

# Modèle
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
