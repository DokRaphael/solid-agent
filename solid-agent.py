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
                iteration = 0
                agent_status = "running"
                while iteration < config.MAX_ITERATIONS :
                    try:
                        print(f"[DEBUG] Messages : {len(messages)}")
                        print(f"[DEBUG] Payload : {len(json.dumps(payload))} caractères")
                        start = time.perf_counter()
                        response = requests.post(
                            config.LLM_URL,
                            json=payload,
                            timeout=config.LLM_TIMEOUT
                        )
                        response.raise_for_status()
                    except requests.exceptions.ConnectionError:
                            print ("Impossible de contacter Qwen")
                            agent_status = "failed"
                            break
                    except requests.exceptions.HTTPError :
                        print ("not OK")
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
                        data = response.json()
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
                            py_arguments = None
                            raw_observation = None 
                            tool_name = tool_call["function"]["name"]
                            arguments = tool_call["function"]["arguments"]
                            print(f"[Qwen] {tool_name} → {arguments}")
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
                            raw_size = 0
                            if isinstance(result, dict):
                                observation = json.dumps(result)
                                raw_size = len(observation)
                            else:
                                text_parts = [
                                    item.text
                                    for item in result.content
                                    if item.type == "text"
                                ]

                                if text_parts:
                                    observation = "\n".join(text_parts)
                                    raw_size = len(observation)
                                    raw_observation = observation
                                    if tool_name == "list_commands":
                                        commands = json.loads(observation)
                                        observation = json.dumps([
                                            {
                                                "id": command["id"],
                                                "enabled": command["enabled"]
                                            }
                                            for command in commands
                                        ])
                                else:
                                    observation = json.dumps({
                                        "status": "error" if result.is_error else "success",
                                        "message": "Résultat MCP non textuel ; contenu non transmis à Qwen",
                                        "content_types": [item.type for item in result.content]
                                    })
                                    raw_size = len(observation)
                                
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
                                "arguments": py_arguments,
                                "result": raw_observation
                            })
                            messages.append(tool_message)
                            print(f"[CONTEXT] Brut : {raw_size} → Envoyé : {len(observation)} caractères")
                            print(f"[DEBUG] Observation : {len(observation)} caractères")
                if agent_status == "running":
                    agent_status = "max_iterations"
                print("Agent status:", agent_status)

asyncio.run(main())
