from __future__ import annotations
import os, json
import boto3
import httpx

ATLAS_BASE_URL = os.environ.get("ATLAS_BASE_URL", "http://localhost:9000")
MODEL_ID = os.environ.get("NOVA_MODEL_ID", "amazon.nova-pro-v1:0")

# Tool schema presented to Nova
TOOLS = [
  {
    "toolSpec": {
      "name": "list_documents",
      "description": "List documents with optional filters",
      "inputSchema": {
        "json": {
          "type": "object",
          "properties": {
            "project_id": {"type": "string"},
            "older_than_days": {"type": "integer"},
            "status_filter": {"type": "string"}
          }
        }
      }
    }
  },
  {
    "toolSpec": {
      "name": "archive_document",
      "description": "Archive documents by id",
      "inputSchema": {"json": {"type":"object","properties":{"doc_ids":{"type":"array","items":{"type":"string"}}}}}
    }
  },
  {
    "toolSpec": {
      "name": "delete_document",
      "description": "Delete documents by id",
      "inputSchema": {"json": {"type":"object","properties":{"doc_ids":{"type":"array","items":{"type":"string"}}, "permanent":{"type":"boolean"}}}}
    }
  }
]

def call_atlas(tool_name: str, tool_input: dict, actor: str="agent") -> dict:
    import httpx
    with httpx.Client(timeout=20.0) as client:
        r = client.post(
            f"{ATLAS_BASE_URL}/tool/{tool_name}",
            json={"actor": actor, "params": tool_input},
        )
        ct = r.headers.get("content-type", "")

        if r.status_code >= 400:
            return {
                "error": True,
                "status_code": r.status_code,
                "detail": r.text
            }

        if "application/json" in ct:
            return r.json()

        return {"raw": r.text}

def main():
    brt = boto3.client("bedrock-runtime")
    system = [{"text": "You are a helpful assistant. Use tools to complete the user's request safely."}]

    print("ATLAS Bedrock Demo Agent")
    print("Type a prompt, or 'quit'.")

    messages = []
    while True:
        user = input("\n> ").strip()
        if not user:
            continue
        if user.lower() in ("quit","exit"):
            break

        messages.append({"role":"user", "content":[{"text": user}]})

        # Converse loop: let Nova decide tool calls; execute them via ATLAS; feed results back.
        for _ in range(6):
            resp = brt.converse(
                modelId=MODEL_ID,
                messages=messages,
                system=system,
                toolConfig={"tools": TOOLS},
                inferenceConfig={"maxTokens": 700, "temperature": 0.2},
            )
            out_msg = resp["output"]["message"]
            messages.append(out_msg)

            # If model responded with text-only, print and break
            tool_uses = []
            for c in out_msg.get("content", []):
                if "toolUse" in c:
                    tool_uses.append(c["toolUse"])
            if not tool_uses:
                # print assistant text
                txt = "".join([c.get("text","") for c in out_msg.get("content", [])])
                print(f"ASSISTANT: {txt}")
                break

            # Execute tools
            tool_results_content = []
            for tu in tool_uses:
                name = tu["name"]
                tool_input = tu.get("input", {}) or {}
                print(f"TOOL CALL -> {name}({json.dumps(tool_input)})")
                atlas_result = call_atlas(name, tool_input)
                print(f"ATLAS -> {json.dumps(atlas_result, indent=2)}")

                tool_results_content.append({
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"text": json.dumps(atlas_result)}],
                        "status": "success" if not atlas_result.get("error") else "error",
                    }
                })

            messages.append({"role":"user", "content": tool_results_content})

if __name__ == "__main__":
    main()
