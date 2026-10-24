# Solid Agent

A command-line Python agent that connects an LLM to Solidcraft's CAD tools through MCP (Model Context Protocol).

I built this project **from scratch** to understand how an agent works and interacts with an MCP server. The agent loop is implemented directly in Python, covering tool discovery, model requests, tool execution, and feeding results back into the conversation.

## How it works

Given a natural-language request, the model selects which tools to call. The agent executes those calls through MCP and returns the results to the model, which can continue taking actions or provide an answer. Conversation history is kept throughout the session, with an iteration limit for each request.

## Getting started

You need Python, `solidcraft-cli` on your PATH, a running Solidcraft instance, and a Chat Completions-compatible API with tool calling support.

```bash
pip install -r requirements.txt
cp config.example.py config.py
```

Edit `config.py` to set your API URL, model, Solidcraft connection address, and allowed directory, then run:

```bash
Solidcraft --control 7878 &
python solid-agent.py
```

Enter a request in the terminal, or type `exit` to quit. Your `config.py` stays local and is ignored by Git.
