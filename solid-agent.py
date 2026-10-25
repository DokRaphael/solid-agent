from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.shared.exceptions import MCPError
import asyncio
import json 
import requests
import config
import time

async def main() :
    server_params = StdioServerParameters(
        command=config.MCP_COMMAND,
        args=[
            "mcp",
            "--connect", config.MCP_ADDRESS,
            "--allow-path", config.MCP_ALLOWED_PATH
        ]
    )
    async with stdio_client(server_params) as (read, write):
        print("Connexion stdio établie")
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Session MCP initialisée")
            cad_state = await get_cad_state(session)
            compact_state = compact_cad_state(cad_state)
            if cad_state.get("bodies"):
                print("[CAD] Body keys:", cad_state["bodies"][0].keys())

            if cad_state.get("params"):
                print("[CAD] Parameter keys:", cad_state["params"][0].keys())

            print("[CAD] État complet :", len(json.dumps(cad_state)), "caractères")
            print("[CAD] État compact :", len(json.dumps(compact_state)), "caractères")
            print("[CAD STATE] Nom :", cad_state.get("name"))
            print("[CAD STATE] Corps :", len(cad_state.get("bodies", [])))
            print("[CAD STATE] Paramètres :", len(cad_state.get("params", [])))
            result = await session.list_tools()

            available_tools = [
                tool
                for tool in result.tools
                if tool.name not in config.BLOCKED_TOOLS
            ]

            allowed_tools = {tool.name for tool in available_tools}
            tool_definition = []
            
            for tool in available_tools :
                tool_definition.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema
                    }
                })
            tool_history = []
            messages = [
            {
                "role":"system",
                "content":config.SYSTEM_PROMPT
            }]
            payload = {
                "model":config.LLM_MODEL,
                "messages":messages,
                "tools":tool_definition,
                "tool_choice": "auto"
            }
            print(f"[DEBUG] Outils MCP : {len(tool_definition)}")
            print(f"[DEBUG] Taille des schémas : {len(json.dumps(tool_definition))} caractères")
            request_id = 0
            while True:
                try:
                    user_request = await asyncio.to_thread(input, "What are we making ?")
                except EOFError:
                    break
                
                if user_request.strip().lower() == "exit":
                    break
                request_id += 1
                messages.append({
                    "role":"user",
                    "content":user_request
                })   
                print("[DEBUG] Dernière demande :", messages[-1])
                print("[DEBUG] Messages avant agent :", len(messages))
                await run_agent(
                    session,
                    messages,
                    payload,
                    allowed_tools,
                    tool_history,
                    request_id
                )

def call_llm(payload):
    response = requests.post(
        config.LLM_URL,
        json=payload,
        timeout=config.LLM_TIMEOUT
    )
    response.raise_for_status()
    return response.json()

def tool_succeeded(result):
    if isinstance(result, dict):
        return result.get("status") != "error" and result.get("ok") is not False
    return not result.is_error

def prepare_observation(result, tool_name):
    if isinstance(result, dict):
        observation = json.dumps(result)
    else:
        text_parts = [
            item.text
            for item in result.content
            if item.type == "text"
        ]

        if text_parts:
            observation = "\n".join(text_parts)
        else:
            observation = json.dumps({
                "status": "error" if result.is_error else "success",
                "message": "Résultat MCP non textuel",
                "content_types": [item.type for item in result.content]
            })
    raw_observation = observation

    if tool_succeeded(result) and tool_name in ("describe_command", "list_commands"):
        try:
            data = json.loads(observation)
            if tool_name == "describe_command" and isinstance(data, dict):
                observation = json.dumps({
                    "id": data["id"],
                    "input": data["input"],
                    "params": data["params"]
                })
            elif tool_name == "list_commands" and isinstance(data, list):
                observation = json.dumps([
                    {"id": command["id"], "enabled": command["enabled"]}
                    for command in data
                ])
        except (json.JSONDecodeError, KeyError, TypeError):
            # Une optimisation de taille ne doit pas empêcher de lire le résultat.
            observation = raw_observation
    return raw_observation, observation

async def execute_tool(session, tool_name, arguments, allowed_tools):
    try :
        py_arguments = json.loads(arguments)
        if not isinstance(py_arguments, dict):
            return {
                "status": "error",
                "message": "Les arguments doivent être un objet JSON"
            }
    except (json.JSONDecodeError,TypeError) as exc:
        result = {
            "status":"error",
            "message":f"Arguments JSON invalides : {exc}"
        }
    else :
        if tool_name == "execute":
            command = py_arguments.get("command")

            if not isinstance(command, str):
                return {
                    "status": "error",
                    "message": "Identifiant de commande invalide"
                }

            if command in config.BLOCKED_COMMANDS:
                return {
                    "status": "error",
                    "message": f"Commande interdite : {command}"
                }
        if tool_name == "batch":
            commands = py_arguments.get("commands")

            if not isinstance(commands, list):
                return {
                    "status": "error",
                    "message": "batch.commands doit être une liste"
                }

            for item in commands:
                if not isinstance(item, dict):
                    return {
                        "status": "error",
                        "message": "Commande batch invalide"
                    }

                command = item.get("command")

                if not isinstance(command, str):
                    return {
                        "status": "error",
                        "message": "Identifiant de commande invalide"
                    }

                if command in config.BLOCKED_COMMANDS:
                    return {
                        "status": "error",
                        "message": f"Commande interdite : {command}"
                    }
        if tool_name in allowed_tools:
            try:
                result = await session.call_tool(
                    name=tool_name,
                    arguments=py_arguments
                )
            except (TypeError, MCPError) as exc:
                result = {
                    "status":"error",
                    "message":str(exc)
                }
            
        else :
            result = {
                "status": "error",
                "message": "Outil inconnu : "+tool_name
            }
    return result

async def run_agent(session, messages, payload, allowed_tools, tool_history, request_id):
    iteration = 0
    agent_status = "running"
    while iteration < config.MAX_ITERATIONS :
        if payload_size(payload, messages) > config.CONTEXT_COMPACTION_THRESHOLD:
            await compact_history(session, messages, payload, tool_history, request_id)
            if payload_size(payload, messages) > config.CONTEXT_COMPACTION_THRESHOLD:
                print("[CONTEXT] Contexte encore trop volumineux après compaction ; appel LLM annulé.")
                agent_status = "context_limit"
                break
        try:
            print(f"[DEBUG] Messages : {len(messages)}")
            print(f"[DEBUG] Payload : {len(json.dumps(payload))} caractères")
            start = time.perf_counter()
            tool_chars = sum(
                len(message["content"])
                for message in messages
                if message["role"] == "tool"
            )

            print(f"[CONTEXT] Historique outils : {tool_chars} caractères")
            boundaries = [
                index
                for index, message in enumerate(messages)
                if message["role"] == "assistant"
                and not message.get("tool_calls")
                and message.get("content")
            ]

            print("[CONTEXT] Frontières possibles :", boundaries)
            data = await asyncio.to_thread(call_llm, payload)
        except requests.exceptions.ConnectionError:
                print ("Impossible de contacter Qwen")
                agent_status = "failed"
                break
        except requests.exceptions.HTTPError as exc:
            print("[HTTP ERROR]", exc)
            print("[HTTP BODY]", exc.response.text[:2000] if exc.response is not None else "")
            agent_status = "failed"
            break
        except requests.exceptions.Timeout:
            print ("timeout")
            print(f"[DEBUG] Timeout après {time.perf_counter() - start:.1f} s")
            agent_status = "failed"
            break
        else:
            print(f"[DEBUG] Réponse reçue en {time.perf_counter() - start:.1f} s")
            iteration+=1
            
            print("[DEBUG] finish_reason:", data["choices"][0]["finish_reason"])
            print("[DEBUG] usage:", data.get("usage"))                 
            assistant_message = data["choices"][0]["message"]
            toolcalls = assistant_message.get("tool_calls")
            if toolcalls is None:
                print(data["choices"][0]["message"]["content"])
                agent_status = "completed"
                messages.append(assistant_message)
                break
            else:
                print("Qwen more outils")
            messages.append(assistant_message)
            cycle_id = iteration
            for tool_call in toolcalls :
                
                tool_name = tool_call["function"]["name"]
                arguments = tool_call["function"]["arguments"]
                print(f"[Qwen] {tool_name} → {arguments}")
                result = await execute_tool(
                    session,
                    tool_name,
                    arguments,
                    allowed_tools
                )
                raw_observation, observation = prepare_observation(result, tool_name)
                raw_size = len(raw_observation)

                success = tool_succeeded(result)

                print(f"[MCP] {tool_name} → {'succès' if success else 'erreur'}")
                if not success:
                    print(f"[MCP ERROR] {observation[:1000]}")
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": observation
                }
                tool_history.append({
                    "cycle_id": cycle_id,
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": raw_observation,
                    "request_id": request_id,
                    "tool_call_id": tool_call["id"],
                    "success": success,
                })
                messages.append(tool_message)
                print(f"[CONTEXT] Brut : {raw_size} → Envoyé : {len(observation)} caractères")
                print(f"[DEBUG] Observation : {len(observation)} caractères")
    if agent_status == "running":
        agent_status = "max_iterations"

    print("[HISTORY] Cycles :", [
        (entry["request_id"], entry["cycle_id"], entry["tool"])
        for entry in tool_history
    ])
    operations = summarize_tool_history(tool_history)
    print("[HISTORY] Opérations :", operations)
    print("Agent status:", agent_status)
    return agent_status

async def get_cad_state(session):
    result = await session.call_tool(
        name="inspect_design",
        arguments={
            "measure": True,
            "sketches": False
        }
    )

    if not tool_succeeded(result):
        raise ValueError("Impossible de lire l'état CAD")
    raw_observation, _ = prepare_observation(result, "inspect_design")
    state = json.loads(raw_observation)
    if not isinstance(state, dict):
        raise ValueError("L'état CAD doit être un objet JSON")
    return state

def compact_cad_state(cad_state):
    
    
    return {
        "name": cad_state.get("name"),
        "units": cad_state.get("units"),
        "bodies": [
                {
                    "name": body["name"],
                    "bbox": body["bbox"],
                    "volume_mm3": body["volume_mm3"],
                    "faces": body["faces"],
                    "visible": body["visible"]
                }
                for body in cad_state.get("bodies", [])
            ],
        "params": [
                {
                    "name": param["name"],
                    "expression": param["expression"],
                    "value": param["value"],
                    "unit": param["unit"],
                    "error": param["error"]
                }
                for param in cad_state.get("params", [])
            ]
    }


def payload_size(payload, messages):
    return len(json.dumps({**payload, "messages": messages}))


def trim_tool_observations(messages, max_chars, tool_history):
    statuses = {
        entry["tool_call_id"]: entry.get("success")
        for entry in tool_history
        if "tool_call_id" in entry
    }
    trimmed = []
    for message in messages:
        content = message.get("content")
        if message["role"] == "tool" and isinstance(content, str) and len(content) > max_chars:
            success = statuses.get(message.get("tool_call_id"))
            status = "success" if success is True else "error" if success is False else "unknown"
            message = {
                **message,
                "content": json.dumps({
                    "status": status,
                    "truncated": True,
                    "original_chars": len(content),
                    "preview": content[:max_chars],
                }, ensure_ascii=False)
            }
        trimmed.append(message)
    return trimmed


async def compact_history(session, messages, payload, tool_history, request_id):
    original_size = payload_size(payload, messages)
    candidates = []
    user_indices = [index for index, message in enumerate(messages) if message["role"] == "user"]

    if user_indices:
        last_user_index = user_indices[-1]
        tool_cycles = [
            index
            for index in range(last_user_index + 1, len(messages))
            if messages[index]["role"] == "assistant" and messages[index].get("tool_calls")
        ]
        try:
            cad_state = compact_cad_state(await get_cad_state(session))
        except (MCPError, ValueError, TypeError, KeyError, IndexError, OSError) as exc:
            # Sans état CAD fiable, garder les échanges et seulement réduire les observations.
            print(f"[COMPACTION] État CAD indisponible : {exc}")
        else:
            operations = summarize_tool_history([
                entry for entry in tool_history if entry["request_id"] == request_id
            ])
            system_message = {
                "role": "system",
                "content": (
                    config.SYSTEM_PROMPT
                    + "\n\nÉtat CAD actuel :\n"
                    + json.dumps(cad_state, ensure_ascii=False)
                    + "\n\nOpérations précédentes réussies :\n"
                    + json.dumps(operations, ensure_ascii=False)
                )
            }
            # Ne jamais séparer un appel d'outil de ses réponses, ni supprimer le dernier cycle.
            keep_counts = range(min(3, len(tool_cycles)), 0, -1) if tool_cycles else [0]
            for keep in keep_counts:
                start = tool_cycles[-keep] if keep else last_user_index + 1
                candidates.append([system_message, messages[last_user_index], *messages[start:]])

    candidates.append(list(messages))
    best_messages = messages
    best_size = original_size
    for candidate in candidates:
        for limit in (None, 4000, 1000, 256):
            compacted = (
                candidate if limit is None
                else trim_tool_observations(candidate, limit, tool_history)
            )
            size = payload_size(payload, compacted)
            if size < best_size:
                best_messages, best_size = compacted, size
            if best_size <= config.CONTEXT_COMPACTION_THRESHOLD:
                break
        if best_size <= config.CONTEXT_COMPACTION_THRESHOLD:
            break

    if best_size < original_size:
        print(f"[COMPACTION] Payload : {original_size} → {best_size} caractères")
        messages[:] = best_messages
        return True
    return False


def summarize_tool_history(tool_history):
    operations = []
    for entry in tool_history:
        if entry.get("success") is not True:
            continue
        tool = entry.get("tool")
        if tool not in ("execute", "batch", "set_parameter"):
            continue
        try:
            arguments = json.loads(entry.get("arguments"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(arguments, dict):
            continue
        try:
            result = json.loads(entry.get("result"))
        except (json.JSONDecodeError, TypeError):
            result = None
        if isinstance(result, dict) and (result.get("status") == "error" or result.get("ok") is False):
            continue

        if tool == "execute":
            command = arguments.get("command")
            if isinstance(command, str):
                operations.append(command)
        elif tool == "batch":
            commands = arguments.get("commands")
            if isinstance(commands, list) and all(
                isinstance(command, dict) and isinstance(command.get("command"), str)
                for command in commands
            ):
                operations.extend(command["command"] for command in commands)
        elif tool == "set_parameter":
            operations.append("set_parameter")
    return operations


if __name__ == "__main__":
    asyncio.run(main())


