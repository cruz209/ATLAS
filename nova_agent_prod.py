"""
ATLAS Nova Agent - Production Edition (Windows Compatible)
- Manual connection to ATLAS proxy (Zeroconf disabled)
- Supports confirmation token retry flow
- Production error handling
- Complete agent → ATLAS → tool flow
"""
from __future__ import annotations

import os
import json
import time
from typing import Any, Dict, List, Optional

import boto3
import httpx


# ============================================================================
# Configuration
# ============================================================================

MODEL_ID = os.environ.get("NOVA_MODEL_ID", "amazon.nova-pro-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ACTOR_ID = os.environ.get("ATLAS_ACTOR_ID", "agent.nova")


# ============================================================================
# Tool Definitions (Presented to Nova)
# ============================================================================

TOOLS = [
    {
        "toolSpec": {
            "name": "list_documents",
            "description": (
                "List documents with optional filters. "
                "Use this for broad browsing or discovery. "
                "Returns metadata for documents matching the criteria."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Optional project filter. Empty means all projects."
                        },
                        "older_than_days": {
                            "type": "integer",
                            "description": "Only show documents older than N days. 0 means no age filter."
                        },
                        "status_filter": {
                            "type": "string",
                            "enum": ["", "Active", "Archived", "Deleted"],
                            "description": "Filter by document status. Empty means all statuses."
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "get_document",
            "description": (
                "Get a single document by entity ID. "
                "Use when referencing a specific doc:... identifier."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_id": {
                            "type": "string",
                            "description": "Document entity ID, e.g. doc:Q4_Legal_Contract"
                        }
                    },
                    "required": ["doc_id"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "archive_document",
            "description": (
                "Archive documents by ID (reversible operation). "
                "Use this to move documents out of active status while preserving them."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of document IDs to archive"
                        }
                    },
                    "required": ["doc_ids"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "delete_document",
            "description": (
                "Delete documents by ID (potentially irreversible). "
                "ATLAS will enforce retention policies and may downgrade to archive. "
                "High-sensitivity documents require human approval."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "doc_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of document IDs to delete"
                        },
                        "permanent": {
                            "type": "boolean",
                            "description": "If true, request permanent deletion (subject to ATLAS policies)"
                        }
                    },
                    "required": ["doc_ids"]
                }
            }
        }
    }
]


# ============================================================================
# ATLAS Client
# ============================================================================

class ATLASClient:
    """Client for calling ATLAS-governed tools."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=30.0)

    def call_tool(
        self,
        tool_name: str,
        params: Dict[str, Any],
        actor: str = ACTOR_ID,
        confirmation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Call ATLAS-governed tool.

        Returns:
            {
                "status": "OK" | "PENDING_APPROVAL" | "BLOCKED",
                "decision": "ALLOW" | "ESCALATE" | "BLOCK" | "DOWNGRADE",
                "reasons": [...],
                "result": {...} or None,
                "approval_url": "..." or None,
                "confirmation_token": "..." or None,
            }
        """
        payload = {
            "actor": actor,
            "params": params,
        }

        if confirmation_token:
            payload["confirmation_token"] = confirmation_token

        try:
            response = self.client.post(
                f"{self.base_url}/tool/{tool_name}",
                json=payload,
            )

            # Handle HTTP errors
            if response.status_code >= 500:
                return {
                    "error": True,
                    "status": "ERROR",
                    "detail": f"ATLAS proxy error: {response.text}",
                }

            if response.status_code == 403:
                # BLOCK decision
                detail = response.json().get("detail", {})
                return {
                    "error": True,
                    "status": "BLOCKED",
                    "decision": "BLOCK",
                    "reasons": detail.get("reasons", ["Action blocked by ATLAS"]),
                    "audit_id": detail.get("audit_id"),
                }

            return response.json()

        except httpx.RequestError as e:
            return {
                "error": True,
                "status": "ERROR",
                "detail": f"Network error: {str(e)}",
            }

    def close(self):
        """Close HTTP client."""
        self.client.close()


# ============================================================================
# Agent Logic
# ============================================================================

def build_system_prompt() -> str:
    """System prompt for Nova agent."""
    return (
        "You are a helpful autonomous assistant with access to document management tools. "
        "All your tool calls are governed by ATLAS, a runtime safety layer.\n\n"
        
        "ATLAS Decisions:\n"
        "- ALLOW: Action approved and executed\n"
        "- DOWNGRADE: Safer alternative executed (e.g., delete→archive)\n"
        "- BLOCK: Action rejected due to policy violation\n"
        "- ESCALATE: Human approval required\n\n"
        
        "When you receive ESCALATE:\n"
        "1. You'll get an approval_url and confirmation_token\n"
        "2. Tell the user a human must approve at the URL\n"
        "3. Wait for user confirmation, then retry with the token\n\n"
        
        "When you receive DOWNGRADE:\n"
        "1. ATLAS executed a safer alternative\n"
        "2. Explain what happened and continue\n\n"
        
        "When you receive BLOCK:\n"
        "1. Explain why the action was blocked\n"
        "2. Suggest alternatives if appropriate\n\n"
        
        "Always rely on actual tool results. Never assume success without confirmation."
    )


def print_tool_result(tool_name: str, params: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Pretty-print tool call result."""
    print(f"\n{'─'*70}")
    print(f"TOOL → {tool_name}({json.dumps(params, indent=2)})")
    print(f"{'─'*70}")

    decision = result.get("decision", "UNKNOWN")
    status = result.get("status", "UNKNOWN")

    # Color coding
    if decision == "ALLOW":
        icon = "✅"
    elif decision == "DOWNGRADE":
        icon = "⬇️"
    elif decision == "BLOCK":
        icon = "🚫"
    elif decision == "ESCALATE":
        icon = "⚠️"
    else:
        icon = "❓"

    print(f"{icon} DECISION: {decision}")
    print(f"   STATUS: {status}")

    if result.get("reasons"):
        print(f"   REASONS:")
        for r in result["reasons"]:
            print(f"      • {r}")

    if result.get("transformed_tool"):
        print(f"   TRANSFORMED: {result['transformed_tool']}")

    if result.get("approval_url"):
        print(f"   APPROVAL URL: {result['approval_url']}")

    print(f"{'─'*70}\n")


def main():
    """Main agent loop."""
    print(f"\n{'='*70}")
    print(f"  ATLAS Nova Agent (Windows Mode)")
    print(f"  Model: {MODEL_ID}")
    print(f"  Actor: {ACTOR_ID}")
    print(f"{'='*70}\n")

    # Manual connection (Windows mode - Zeroconf disabled)
    atlas_url = os.environ.get("ATLAS_BASE_URL", "http://localhost:9000")
    print(f"[Manual] Connecting to ATLAS at: {atlas_url}")

    # Test connection
    try:
        test_client = httpx.Client(timeout=5.0)
        resp = test_client.get(f"{atlas_url}/health")
        if resp.status_code == 200:
            print(f"[Manual] ✅ Connected to ATLAS proxy\n")
        else:
            print(f"\n❌ ERROR: ATLAS proxy not responding")
            print(f"   Start it first: python atlas_proxy_prod.py\n")
            test_client.close()
            return
        test_client.close()
    except Exception as e:
        print(f"\n❌ ERROR: Cannot connect to ATLAS at {atlas_url}")
        print(f"   Make sure proxy is running: python atlas_proxy_prod.py")
        print(f"   Error: {e}\n")
        return

    # Initialize ATLAS client
    atlas = ATLASClient(atlas_url)

    # Initialize Bedrock
    print(f"[Bedrock] Initializing {MODEL_ID}...")
    brt = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    system = [{"text": build_system_prompt()}]
    messages: List[Dict[str, Any]] = []

    # Pending confirmations (for ESCALATE retry)
    pending_confirmations: Dict[str, str] = {}  # tool_call_id → confirmation_token

    print("\n" + "─"*70)
    print("READY. Example prompts:")
    print("  • List all documents")
    print("  • Delete old documents from 2022")
    print("  • Delete doc:Q4_Legal_Contract")
    print("  • Archive everything older than 6 months")
    print("─"*70)

    while True:
        user_input = input("\n> ").strip()

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit", "q"):
            break

        messages.append({
            "role": "user",
            "content": [{"text": user_input}]
        })

        # Conversation loop with tool use
        for turn in range(8):  # Max 8 turns per user message
            try:
                resp = brt.converse(
                    modelId=MODEL_ID,
                    messages=messages,
                    system=system,
                    toolConfig={"tools": TOOLS},
                    inferenceConfig={
                        "maxTokens": 1000,
                        "temperature": 0.2
                    },
                )
            except Exception as e:
                print(f"\n❌ Bedrock error: {e}")
                break

            out_msg = resp["output"]["message"]
            messages.append(out_msg)

            # Extract tool uses
            tool_uses = [
                c["toolUse"]
                for c in out_msg.get("content", [])
                if "toolUse" in c
            ]

            # If no tool calls, print assistant response and break
            if not tool_uses:
                text_parts = [
                    c.get("text", "")
                    for c in out_msg.get("content", [])
                    if "text" in c
                ]
                assistant_text = "".join(text_parts)

                if assistant_text.strip():
                    print(f"\n🤖 ASSISTANT: {assistant_text}")

                break

            # Execute tool calls through ATLAS
            tool_results_content = []

            for tool_use in tool_uses:
                tool_name = tool_use["name"]
                tool_use_id = tool_use["toolUseId"]
                params = tool_use.get("input", {}) or {}

                # Check if we have a confirmation token for retry
                confirmation_token = pending_confirmations.pop(tool_use_id, None)

                # Call ATLAS
                atlas_result = atlas.call_tool(
                    tool_name=tool_name,
                    params=params,
                    actor=ACTOR_ID,
                    confirmation_token=confirmation_token,
                )

                print_tool_result(tool_name, params, atlas_result)

                # Handle ESCALATE: save token for retry
                if atlas_result.get("decision") == "ESCALATE":
                    token = atlas_result.get("confirmation_token")
                    if token:
                        pending_confirmations[tool_use_id] = token
                        print(f"💾 Saved confirmation token for retry")

                # Prepare tool result for Nova
                tool_results_content.append({
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"text": json.dumps(atlas_result)}],
                        "status": "error" if atlas_result.get("error") else "success",
                    }
                })

            # Feed tool results back to model
            messages.append({
                "role": "user",
                "content": tool_results_content
            })

    # Cleanup
    atlas.close()
    print("\n[Agent] Shutdown complete")


if __name__ == "__main__":
    main()