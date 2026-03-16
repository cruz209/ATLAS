"""
ATLAS Production Proxy v2 - Windows Compatible
- Transparent network intercept for agent tool calls
- Zeroconf disabled for Windows compatibility
- Complete confirmation token retry flow
- Progressive context retrieval (NEEDS_CONTEXT loop)
- Production-grade error handling and observability
"""
from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
from uuid import uuid4
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from atlas_core.actions import Action
from atlas_core.loop import run_once, build_default_context
from atlas_core.types import Decision
from services.world_state_db import WorldStateDB
from services import db


# ============================================================================
# Configuration
# ============================================================================

ATLAS_HOST = os.environ.get("ATLAS_HOST", "0.0.0.0")
ATLAS_PORT = int(os.environ.get("ATLAS_PORT", "9000"))
TOOL_BACKEND_URL = os.environ.get("TOOL_BACKEND_URL", "http://localhost:9001")
SECRET = os.environ.get("ATLAS_HMAC_SECRET", os.urandom(32).hex())


# ============================================================================
# Application Lifecycle Management
# ============================================================================

http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage ATLAS proxy lifecycle: startup/shutdown."""
    global http_client

    # Startup
    print(f"\n{'='*70}")
    print(f"  ATLAS Production Proxy v2 (Windows Mode)")
    print(f"  Listening on {ATLAS_HOST}:{ATLAS_PORT}")
    print(f"{'='*70}\n")

    # Initialize HTTP client for tool backend
    http_client = httpx.AsyncClient(timeout=30.0)

    # Zeroconf disabled for Windows compatibility
    print("[INFO] Zeroconf disabled (Windows compatibility mode)")
    print(f"[INFO] ATLAS available at: http://localhost:{ATLAS_PORT}")
    print(f"[INFO] Admin UI at: http://localhost:{ATLAS_PORT}/admin\n")

    yield

    # Shutdown
    if http_client:
        await http_client.aclose()

    print("\n[ATLAS] Proxy shutdown complete")


app = FastAPI(
    title="ATLAS Production Proxy",
    version="2.0",
    lifespan=lifespan,
)


# ============================================================================
# Approval Queue (Confirmation Token Flow)
# ============================================================================

class ApprovalQueue:
    """Manages pending escalations with HMAC-signed confirmation tokens."""

    def __init__(self):
        # In production: use Redis/PostgreSQL for durability
        self._pending: Dict[str, Dict[str, Any]] = {}

    def create(
        self,
        tool_name: str,
        params: Dict[str, Any],
        actor: str,
        reasons: list[str],
    ) -> tuple[str, str, str]:
        """
        Create approval request.
        Returns: (req_id, token, approval_url)
        """
        req_id = f"req_{uuid4().hex[:12]}"
        payload = json.dumps(
            {"tool": tool_name, "params": params, "actor": actor},
            sort_keys=True
        )

        exp = datetime.utcnow() + timedelta(minutes=15)
        token = self._sign_token(payload, exp)

        self._pending[req_id] = {
            "payload": payload,
            "tool_name": tool_name,
            "params": params,
            "actor": actor,
            "created": datetime.utcnow().isoformat(),
            "token": token,
            "reasons": reasons,
            "status": "PENDING",
        }

        approval_url = f"http://localhost:{ATLAS_PORT}/approve/{req_id}"
        return req_id, token, approval_url

    def get(self, req_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve approval request."""
        return self._pending.get(req_id)

    def approve(self, req_id: str) -> Optional[Dict[str, Any]]:
        """Mark as approved and return request details."""
        item = self._pending.get(req_id)
        if item:
            item["status"] = "APPROVED"
            item["approved_at"] = datetime.utcnow().isoformat()
        return item

    def reject(self, req_id: str) -> None:
        """Remove from queue (rejection)."""
        self._pending.pop(req_id, None)

    def verify_token(self, payload: str, token: str) -> bool:
        """Verify HMAC token is valid and not expired."""
        try:
            sig, exp_s = token.split(".", 1)
            exp = int(exp_s)
        except Exception:
            return False

        if time.time() > exp:
            return False

        msg = f"{payload}|{exp}".encode()
        expected = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    @staticmethod
    def _sign_token(payload: str, exp: datetime) -> str:
        """Generate HMAC-signed token with expiration."""
        msg = f"{payload}|{int(exp.timestamp())}".encode()
        sig = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
        return f"{sig}.{int(exp.timestamp())}"


approval_queue = ApprovalQueue()


# ============================================================================
# Request/Response Models
# ============================================================================

class ToolCall(BaseModel):
    """Tool invocation from agent."""
    actor: str = "agent.nova"
    params: Dict[str, Any] = Field(default_factory=dict)
    confirmation_token: Optional[str] = None  # For retry after ESCALATE


class ProxyResponse(BaseModel):
    """Standard ATLAS proxy response."""
    status: str  # OK | PENDING_APPROVAL | BLOCKED
    decision: str  # ALLOW | BLOCK | ESCALATE | DOWNGRADE | NEEDS_CONTEXT
    reasons: list[str]
    transformed_tool: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    approval_url: Optional[str] = None
    confirmation_token: Optional[str] = None
    audit_id: str
    decision_latency_ms: int
    world_version: int


# ============================================================================
# Utility Functions
# ============================================================================

def _normalize_param(v: Any) -> Any:
    """Sanitize agent-provided parameters."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s.lower() in ("null", "none"):
            return None
        if s in ('&quot;&quot;', '""'):
            return ""
        return s
    return v


def _redact_document(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Apply REDACT view to Restricted documents."""
    return {
        "entity_id": "REDACTED",
        "project_id": doc.get("project_id"),
        "sensitivity": doc.get("sensitivity"),
        "status": doc.get("status"),
        "trust_boundary": doc.get("trust_boundary"),
        "note": "ATLAS: Restricted document fields redacted per read policy",
    }


# ============================================================================
# Core Proxy Logic
# ============================================================================

@app.get("/health")
def health():
    """Health check endpoint."""
    return {
        "ok": True,
        "service": "ATLAS Proxy v2",
        "world_version": db.get_version(),
        "pending_approvals": len([x for x in approval_queue._pending.values() if x["status"] == "PENDING"]),
    }


@app.get("/")
def root():
    """Service info."""
    return {
        "service": "ATLAS Production Proxy v2",
        "version": "2.0",
        "capabilities": ["P0", "P1", "NEEDS_CONTEXT", "ESCALATE", "DOWNGRADE"],
        "endpoints": {
            "tool_intercept": "/tool/{tool_name}",
            "approval_ui": "/approve/{req_id}",
            "admin_ui": "/admin",
            "health": "/health",
        },
    }


@app.post("/tool/{tool_name}")
async def intercept_tool(
    tool_name: str,
    call: ToolCall,
    request: Request,
) -> ProxyResponse:
    """
    Transparent proxy intercept for agent tool calls.

    Flow:
    1. Check for confirmation token (retry after ESCALATE)
    2. Read tools: fast-path with redaction
    3. Mutation tools: full ATLAS gate (P0→P1→P2-P4)
    4. NEEDS_CONTEXT: progressive disclosure loop
    5. ESCALATE: create approval + token
    6. ALLOW/DOWNGRADE: forward to tool backend
    7. BLOCK: reject with reasons
    """
    t0 = time.perf_counter()
    audit_id = str(uuid4())
    params = call.params or {}

    # ========================================================================
    # CONFIRMATION TOKEN RETRY PATH
    # ========================================================================
    if call.confirmation_token:
        # Agent is retrying with token after human approval
        req_id = None
        for rid, item in approval_queue._pending.items():
            if item.get("token") == call.confirmation_token:
                req_id = rid
                break

        if not req_id:
            raise HTTPException(
                status_code=401,
                detail={"error": "Invalid or expired confirmation token"}
            )

        item = approval_queue.get(req_id)
        if item["status"] != "APPROVED":
            raise HTTPException(
                status_code=403,
                detail={"error": "Approval not granted", "status": item["status"]}
            )

        # Execute approved action
        forward_payload = {
            "doc_ids": params.get("doc_ids", []),
            "permanent": params.get("permanent", False),
        }

        resp = await http_client.post(
            f"{TOOL_BACKEND_URL}/tools/{tool_name}",
            json=forward_payload
        )

        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail={"upstream": resp.text})

        approval_queue.reject(req_id)  # Clear from queue

        db.audit_insert(
            audit_id=audit_id,
            ts=datetime.utcnow().isoformat(),
            actor=call.actor,
            tool_name=tool_name,
            decision="APPROVED_RETRY",
            reasons=f"Executed with confirmation token; original reasons: {item['reasons']}",
            final_tool=tool_name,
            executed=1,
            world_version=db.get_version(),
        )

        return ProxyResponse(
            status="OK",
            decision="ALLOW",
            reasons=["Executed after human approval"],
            result=resp.json(),
            audit_id=audit_id,
            decision_latency_ms=int((time.perf_counter() - t0) * 1000),
            world_version=db.get_version(),
        )

    # ========================================================================
    # READ TOOLS: Fast-path with redaction
    # ========================================================================
    if tool_name in ("list_documents", "get_document"):
        project_id = _normalize_param(params.get("project_id"))
        older_than_days = params.get("older_than_days", 0) or 0
        status_filter = _normalize_param(params.get("status_filter"))

        decision = "ALLOW"
        reasons = ["Read-only fast-path"]

        if tool_name == "list_documents":
            payload = {
                "project_id": project_id,
                "older_than_days": int(older_than_days),
                "status_filter": status_filter if status_filter else "",
            }
            resp = await http_client.post(
                f"{TOOL_BACKEND_URL}/tools/list_documents",
                json=payload
            )
        else:
            doc_id = _normalize_param(params.get("doc_id") or params.get("entity_id"))
            payload = {"doc_id": doc_id}
            resp = await http_client.post(
                f"{TOOL_BACKEND_URL}/tools/get_document",
                json=payload
            )

        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail={"upstream": resp.text})

        result = resp.json()

        # Apply REDACT view to Restricted documents
        if tool_name == "list_documents":
            docs = result.get("documents", [])
            out_docs = []
            for d in docs:
                if isinstance(d, dict) and d.get("sensitivity") == "Restricted":
                    decision = "ALLOW_WITH_REDACTION"
                    reasons.append("Redacted: Restricted sensitivity")
                    out_docs.append(_redact_document(d))
                else:
                    out_docs.append(d)
            result["documents"] = out_docs
        else:
            d = result.get("document")
            if isinstance(d, dict) and d.get("sensitivity") == "Restricted":
                decision = "ALLOW_WITH_REDACTION"
                reasons.append("Redacted: Restricted sensitivity")
                result["document"] = _redact_document(d)

        db.audit_insert(
            audit_id=audit_id,
            ts=datetime.utcnow().isoformat(),
            actor=call.actor,
            tool_name=tool_name,
            decision=decision,
            reasons="; ".join(reasons),
            final_tool=tool_name,
            executed=1,
            world_version=result.get("world_version", db.get_version()),
        )

        return ProxyResponse(
            status="OK",
            decision=decision,
            reasons=reasons,
            result=result,
            audit_id=audit_id,
            decision_latency_ms=int((time.perf_counter() - t0) * 1000),
            world_version=result.get("world_version", db.get_version()),
        )

    # ========================================================================
    # MUTATION TOOLS: Full ATLAS gate (P0→P1→P2-P4)
    # ========================================================================
    targets = params.get("doc_ids") or params.get("targets") or []
    action = Action(
        name=tool_name,
        actor=call.actor,
        targets=targets,
        params=params,
        irreversible=(tool_name == "delete_document"),
        confirmation_token=call.confirmation_token,
    )

    world = WorldStateDB()
    ctx = build_default_context(world)

    # Run ATLAS gate loop (handles NEEDS_CONTEXT progressive disclosure)
    outcome = run_once(world, ctx, action, max_rounds=5)
    decision = outcome.gate.decision
    reasons = outcome.gate.reasons

    decision_latency_ms = int((time.perf_counter() - t0) * 1000)

    # ========================================================================
    # ESCALATE: Create approval + token
    # ========================================================================
    if decision == Decision.ESCALATE:
        req_id, token, approval_url = approval_queue.create(
            tool_name=tool_name,
            params=params,
            actor=call.actor,
            reasons=reasons,
        )

        db.audit_insert(
            audit_id=audit_id,
            ts=datetime.utcnow().isoformat(),
            actor=call.actor,
            tool_name=tool_name,
            decision=str(decision),
            reasons="; ".join(reasons),
            final_tool="none",
            executed=0,
            world_version=world.version,
        )

        return ProxyResponse(
            status="PENDING_APPROVAL",
            decision=str(decision),
            reasons=reasons,
            approval_url=approval_url,
            confirmation_token=token,  # Agent uses this on retry
            audit_id=audit_id,
            decision_latency_ms=decision_latency_ms,
            world_version=world.version,
        )

    # ========================================================================
    # BLOCK: Reject
    # ========================================================================
    if decision == Decision.BLOCK:
        db.audit_insert(
            audit_id=audit_id,
            ts=datetime.utcnow().isoformat(),
            actor=call.actor,
            tool_name=tool_name,
            decision=str(decision),
            reasons="; ".join(reasons),
            final_tool="none",
            executed=0,
            world_version=world.version,
        )

        raise HTTPException(
            status_code=403,
            detail={
                "status": "BLOCKED",
                "decision": str(decision),
                "reasons": reasons,
                "audit_id": audit_id,
            }
        )

    # ========================================================================
    # ALLOW / DOWNGRADE: Forward to tool backend
    # ========================================================================
    final_tool = outcome.audit.final_action if outcome.audit.final_action != "none" else tool_name

    forward_payload = {
        "doc_ids": params.get("doc_ids", []),
        "permanent": params.get("permanent", False),
    }

    resp = await http_client.post(
        f"{TOOL_BACKEND_URL}/tools/{final_tool}",
        json=forward_payload
    )

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail={"upstream": resp.text})

    db.audit_insert(
        audit_id=audit_id,
        ts=datetime.utcnow().isoformat(),
        actor=call.actor,
        tool_name=tool_name,
        decision=str(decision),
        reasons="; ".join(reasons),
        final_tool=final_tool,
        executed=1,
        world_version=db.get_version(),
    )

    return ProxyResponse(
        status="OK",
        decision=str(decision),
        reasons=reasons,
        transformed_tool=final_tool if final_tool != tool_name else None,
        result=resp.json(),
        audit_id=audit_id,
        decision_latency_ms=decision_latency_ms,
        world_version=db.get_version(),
    )


# ============================================================================
# Approval UI (Human-in-the-Loop)
# ============================================================================

@app.get("/approve/{req_id}", response_class=HTMLResponse)
def approval_page(req_id: str):
    """Human approval interface for escalated actions."""
    item = approval_queue.get(req_id)
    if not item:
        return HTMLResponse("<h2>Approval request not found</h2>", status_code=404)

    if item["status"] != "PENDING":
        return HTMLResponse(f"<h2>Already {item['status'].lower()}</h2>", status_code=200)

    reasons_html = "".join(f"<li>{r}</li>" for r in item.get("reasons", []))

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>ATLAS Approval: {req_id}</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 800px;
                margin: 40px auto;
                padding: 20px;
                background: #0b1020;
                color: #e8eefc;
            }}
            .header {{
                border-bottom: 2px solid #1d66ff;
                padding-bottom: 16px;
                margin-bottom: 24px;
            }}
            .header h1 {{
                margin: 0;
                color: #1d66ff;
                font-size: 1.5rem;
            }}
            .section {{
                background: #111830;
                border: 1px solid #263150;
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 16px;
            }}
            .section h2 {{
                margin-top: 0;
                font-size: 0.9rem;
                text-transform: uppercase;
                letter-spacing: 0.1em;
                color: #9eb0d1;
            }}
            pre {{
                background: #0a0f1f;
                padding: 16px;
                border-radius: 8px;
                overflow: auto;
                font-size: 0.85rem;
            }}
            ul {{
                margin: 8px 0;
                padding-left: 20px;
            }}
            .actions {{
                display: flex;
                gap: 12px;
                margin-top: 24px;
            }}
            button {{
                padding: 12px 24px;
                border-radius: 8px;
                border: none;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
            }}
            button[name="decision"][value="approve"] {{
                background: #10b981;
                color: white;
            }}
            button[name="decision"][value="approve"]:hover {{
                background: #059669;
            }}
            button[name="decision"][value="reject"] {{
                background: #ef4444;
                color: white;
            }}
            button[name="decision"][value="reject"]:hover {{
                background: #dc2626;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>⚠️ ATLAS Approval Required</h1>
            <p style="color: #9eb0d1; margin: 8px 0 0;">Request ID: <code>{req_id}</code></p>
        </div>
        
        <div class="section">
            <h2>Action Details</h2>
            <p><strong>Tool:</strong> {item["tool_name"]}</p>
            <p><strong>Actor:</strong> {item["actor"]}</p>
            <p><strong>Created:</strong> {item["created"]}</p>
        </div>
        
        <div class="section">
            <h2>Reasons for Escalation</h2>
            <ul>{reasons_html}</ul>
        </div>
        
        <div class="section">
            <h2>Request Payload</h2>
            <pre>{json.dumps(item["params"], indent=2)}</pre>
        </div>
        
        <form method="post" class="actions">
            <button type="submit" name="decision" value="approve">✓ Approve & Execute</button>
            <button type="submit" name="decision" value="reject">✗ Reject</button>
        </form>
    </body>
    </html>
    """)


@app.post("/approve/{req_id}")
async def approval_submit(req_id: str, decision: str = Form(...)):
    """Process human approval decision."""
    item = approval_queue.get(req_id)
    if not item:
        raise HTTPException(status_code=404, detail="Approval request not found")

    if decision == "reject":
        approval_queue.reject(req_id)
        return JSONResponse({
            "status": "REJECTED",
            "req_id": req_id,
            "message": "Action rejected by human reviewer"
        })

    # Verify token
    if not approval_queue.verify_token(item["payload"], item["token"]):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Mark as approved
    approval_queue.approve(req_id)

    # Execute the approved action
    tool_name = item["tool_name"]
    params = item["params"]

    forward_payload = {
        "doc_ids": params.get("doc_ids", []),
        "permanent": params.get("permanent", False),
    }

    resp = await http_client.post(
        f"{TOOL_BACKEND_URL}/tools/{tool_name}",
        json=forward_payload
    )

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail={"upstream": resp.text})

    # Clear from queue
    approval_queue.reject(req_id)

    return JSONResponse({
        "status": "APPROVED_AND_EXECUTED",
        "req_id": req_id,
        "result": resp.json(),
        "message": "Action approved and executed successfully"
    })


# ============================================================================
# Admin UI
# ============================================================================

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    """Administrative dashboard for monitoring ATLAS."""
    # Get pending approvals
    pending = [
        item for item in approval_queue._pending.values()
        if item["status"] == "PENDING"
    ]

    # Get recent audit log
    con = db._conn()
    cur = con.cursor()
    cur.execute(
        "SELECT * FROM audit_log ORDER BY ts DESC LIMIT 50"
    )
    audit_entries = [dict(row) for row in cur.fetchall()]
    con.close()

    # Build HTML
    pending_html = ""
    for item in pending:
        req_id = [k for k, v in approval_queue._pending.items() if v == item][0]
        pending_html += f"""
        <div class="card">
            <div class="card-header">
                <strong>{item['tool_name']}</strong>
                <span class="badge badge-warning">PENDING</span>
            </div>
            <div class="card-body">
                <p><strong>Actor:</strong> {item['actor']}</p>
                <p><strong>Created:</strong> {item['created']}</p>
                <p><strong>Reasons:</strong> {', '.join(item['reasons'])}</p>
                <a href="/approve/{req_id}" class="btn">Review</a>
            </div>
        </div>
        """

    if not pending_html:
        pending_html = "<p style='color: #9eb0d1;'>No pending approvals</p>"

    audit_html = ""
    for entry in audit_entries[:20]:
        decision_class = entry['decision'].lower().replace('_', '-')
        audit_html += f"""
        <div class="audit-entry">
            <span class="badge badge-{decision_class}">{entry['decision']}</span>
            <strong>{entry['tool_name']}</strong> by {entry['actor']}
            <span style="color: #9eb0d1; font-size: 0.85rem;">({entry['ts']})</span>
        </div>
        """

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>ATLAS Admin Dashboard</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                margin: 0;
                padding: 0;
                background: #0b1020;
                color: #e8eefc;
            }}
            .header {{
                background: linear-gradient(135deg, #1d66ff 0%, #7c3aed 100%);
                padding: 24px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            }}
            .header h1 {{
                margin: 0;
                font-size: 1.8rem;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
                padding: 24px;
            }}
            .grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 24px;
                margin-bottom: 24px;
            }}
            .panel {{
                background: #111830;
                border: 1px solid #263150;
                border-radius: 12px;
                padding: 20px;
            }}
            .panel h2 {{
                margin-top: 0;
                font-size: 1rem;
                text-transform: uppercase;
                letter-spacing: 0.1em;
                color: #1d66ff;
                border-bottom: 1px solid #263150;
                padding-bottom: 12px;
            }}
            .card {{
                background: #0a0f1f;
                border: 1px solid #1a2440;
                border-radius: 8px;
                padding: 16px;
                margin-bottom: 12px;
            }}
            .card-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 8px;
            }}
            .badge {{
                padding: 4px 12px;
                border-radius: 12px;
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
            }}
            .badge-warning {{ background: #f59e0b; color: #000; }}
            .badge-allow {{ background: #10b981; color: #000; }}
            .badge-block {{ background: #ef4444; color: #fff; }}
            .badge-escalate {{ background: #f59e0b; color: #000; }}
            .badge-downgrade {{ background: #7c3aed; color: #fff; }}
            .badge-approved-retry {{ background: #10b981; color: #000; }}
            .btn {{
                display: inline-block;
                padding: 8px 16px;
                background: #1d66ff;
                color: white;
                text-decoration: none;
                border-radius: 6px;
                font-size: 0.9rem;
                margin-top: 8px;
            }}
            .audit-entry {{
                padding: 12px;
                border-bottom: 1px solid #1a2440;
                font-size: 0.9rem;
            }}
            .stats {{
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 16px;
            }}
            .stat {{
                text-align: center;
                background: #0a0f1f;
                padding: 16px;
                border-radius: 8px;
            }}
            .stat-value {{
                font-size: 2rem;
                font-weight: 700;
                color: #1d66ff;
            }}
            .stat-label {{
                font-size: 0.85rem;
                color: #9eb0d1;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🛡️ ATLAS Admin Dashboard</h1>
        </div>
        
        <div class="container">
            <div class="panel">
                <h2>System Status</h2>
                <div class="stats">
                    <div class="stat">
                        <div class="stat-value">{db.get_version()}</div>
                        <div class="stat-label">World Version</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value">{len(pending)}</div>
                        <div class="stat-label">Pending Approvals</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value">{len(audit_entries)}</div>
                        <div class="stat-label">Recent Audits</div>
                    </div>
                </div>
            </div>
            
            <div class="grid">
                <div class="panel">
                    <h2>Pending Approvals</h2>
                    {pending_html}
                </div>
                
                <div class="panel">
                    <h2>Recent Audit Log</h2>
                    {audit_html}
                </div>
            </div>
        </div>
    </body>
    </html>
    """)


# ============================================================================
# Run Server
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    # Initialize database
    db.init_db()

    # Check if we should seed demo data
    if db.get_version() == 0:
        print("[DB] Seeding demo world state...")
        db.seed_demo_world()

    uvicorn.run(
        app,
        host=ATLAS_HOST,
        port=ATLAS_PORT,
        log_level="info",
    )