from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
import asyncio
import json 
import requests
import config
import time
server_params = StdioServerParameters(
    command=config.MCP_COMMAND,
    args=[
        "mcp",
        "--connect", config.MCP_ADDRESS,
        "--allow-path", config.MCP_ALLOWED_PATH
    ]
)

async def main() :
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
            allowed_tools = {tool.name for tool in result.tools}
            tool_definition = []
            
            for tool in result.tools :
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
            while True:
                user_request = input("What are we making ?")
                if user_request.strip().lower() == "exit":
                    break
                messages.append({
                    "role":"user",
                    "content":user_request
                })   
                print("[DEBUG] Dernière demande :", messages[-1])
                print("[DEBUG] Messages avant agent :", len(messages))
                agent_status = await run_agent(
                    session,
                    messages,
                    payload,
                    allowed_tools,
                    tool_history
                )
                if agent_status == "completed":
                    context_size = len(json.dumps(messages))

                    if context_size > config.CONTEXT_COMPACTION_THRESHOLD:
                        await compact_history(session, messages)

def call_llm(payload):
    response = requests.post(
        config.LLM_URL,
        json=payload,
        timeout=config.LLM_TIMEOUT
    )
    response.raise_for_status()
    return response.json()

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

    success = (
        result.get("status") != "error"
        if isinstance(result, dict)
        else not result.is_error
    )
    if tool_name == "describe_command" and success:
        command = json.loads(observation)

        observation = json.dumps({
            "id": command["id"],
            "input": command["input"],
            "params": command["params"]
        })
    if tool_name == "list_commands" and success:
        commands = json.loads(observation)

        observation = json.dumps([
            {
                "id": command["id"],
                "enabled": command["enabled"]
            }
            for command in commands
        ])
    return raw_observation, observation

async def execute_tool(session, tool_name, arguments, allowed_tools):
    try :
        py_arguments = json.loads(arguments)
    except (json.JSONDecodeError,TypeError) :
        result = {
            "status":"error",
            "message":"argument issue: "+arguments
        }
    else :
        if tool_name in allowed_tools:
            try:
                result = await session.call_tool(
                    name=tool_name,
                    arguments=py_arguments
                )
            except TypeError as exc:
                result = {
                    "status":"error",
                    "message":"argument issue: "+str(exc)
                }
        else :
            result = {
                "status": "error",
                "message": "Outil inconnu : "+tool_name
            }
    return result

async def run_agent(session, messages, payload, allowed_tools, tool_history):
    iteration = 0
    agent_status = "running"
    while iteration < config.MAX_ITERATIONS :
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
            data = call_llm(payload)
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

                if isinstance(result, dict):
                    success = result.get("status") != "error"
                    
                else:
                    success = not result.is_error

                print(f"[MCP] {tool_name} → {'succès' if success else 'erreur'}")
                if not success:
                    print(f"[MCP ERROR] {observation[:1000]}")
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": observation
                }
                tool_history.append({
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": raw_observation
                })
                messages.append(tool_message)
                print(f"[CONTEXT] Brut : {raw_size} → Envoyé : {len(observation)} caractères")
                print(f"[DEBUG] Observation : {len(observation)} caractères")
    if agent_status == "running":
        agent_status = "max_iterations"
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

    return json.loads(result.content[0].text)

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


async def compact_history(session, messages):
    cad_state = await get_cad_state(session)
    compact_state = compact_cad_state(cad_state)
    system_message = {
        "role": "system",
        "content": (
            config.SYSTEM_PROMPT
            + "\n\nÉtat actuel du modèle SolidCraft :\n"
            + json.dumps(compact_state, ensure_ascii=False)
        )
    }

    user_indices = [
        index
        for index, message in enumerate(messages)
        if message["role"] == "user"
    ]

    if user_indices:
        start = user_indices[-1]
        recent_messages = messages[start:]
    else:
        recent_messages = []

    compact_messages = [
        system_message,
        *recent_messages
    ]
    print(f"[COMPACTION] {len(messages)} → {len(compact_messages)} messages")
    print("[COMPACTION] État CAD :", len(json.dumps(compact_state)), "caractères")
    print(json.dumps(compact_state, indent=2, ensure_ascii=False))
    original_size = len(json.dumps(messages))
    compact_size = len(json.dumps(compact_messages))

    print(f"[COMPACTION] Taille : {original_size} → {compact_size} caractères")
    if compact_size < original_size:
        messages[:] = compact_messages
asyncio.run(main())


