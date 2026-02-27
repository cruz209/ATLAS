# from __future__ import annotations
#
# import os, json, time, uuid, hmac, hashlib
# from datetime import datetime, timedelta
# from typing import Any, Dict
#
# import httpx
# from fastapi import FastAPI, HTTPException, Request, Form
# from fastapi.responses import HTMLResponse
# from pydantic import BaseModel
#
# from atlas_core.actions import Action
# from atlas_core.loop import run_once
# from atlas_core.types import Decision
#
# from services.world_state_db import WorldStateDB
# from services import db
#
# TOOL_BACKEND_URL = os.environ.get("TOOL_BACKEND_URL", "http://localhost:9001")
# SECRET = os.environ.get("ATLAS_HMAC_SECRET", "dev-secret-change-me")
#
# app = FastAPI(title="ATLAS Proxy", version="0.1")
# CLIENT: httpx.AsyncClient | None = None
#
# @app.on_event("startup")
# async def _startup():
#     global CLIENT
#     CLIENT = httpx.AsyncClient(timeout=20.0)
#
# @app.on_event("shutdown")
# async def _shutdown():
#     global CLIENT
#     if CLIENT:
#         await CLIENT.aclose()
#         CLIENT = None
#
# # Pending escalation store (demo). In prod, durable store.
# PENDING: Dict[str, Dict[str, Any]] = {}
#
#
# class ToolCall(BaseModel):
#     actor: str = "agent"
#     params: Dict[str, Any] = {}
#
#
# def _norm_empty(v: Any) -> Any:
#     """Normalize empty-ish / HTML-escaped values coming from the model."""
#     if v is None:
#         return None
#     if isinstance(v, str):
#         s = v.strip()
#         if s == "":
#             return ""
#         s_low = s.lower()
#         if s_low in ("null", "none"):
#             return None
#         # common tool/HTML artifacts
#         if s in ("&quot;&quot;", '""'):
#             return ""
#         if s_low == "default":
#             return ""  # treat as "no filter"
#         return s
#     return v
#
#
# def _redact_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
#     """Redact sensitive fields for Restricted documents before the model sees them."""
#     return {
#         "entity_id": "REDACTED",
#         "project_id": doc.get("project_id"),
#         "sensitivity": doc.get("sensitivity"),
#         "status": doc.get("status"),
#         "trust_boundary": doc.get("trust_boundary"),
#         "is_present": doc.get("is_present"),
#         "note": "Restricted document fields redacted by ATLAS read policy",
#     }
#
#
# def _sign_token(payload: str, exp: datetime) -> str:
#     msg = f"{payload}|{int(exp.timestamp())}".encode()
#     sig = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
#     return f"{sig}.{int(exp.timestamp())}"
#
#
# def _verify_token(payload: str, token: str) -> bool:
#     try:
#         sig, exp_s = token.split(".", 1)
#         exp = int(exp_s)
#     except Exception:
#         return False
#     if time.time() > exp:
#         return False
#     msg = f"{payload}|{exp}".encode()
#     expected = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
#     return hmac.compare_digest(sig, expected)
#
#
# @app.get("/health")
# def health():
#     return {"ok": True}
#
#
# @app.post("/tool/{tool_name}")
# async def intercept(tool_name: str, call: ToolCall, request: Request) -> Dict[str, Any]:
#     t0 = time.perf_counter()
#     params = call.params or {}
#
#     # FAST-PATH: read tools (do NOT run v1 target-based gating)
#     # (v1 ContextEngine/admissibility assumes target-ish actions; list ops can hang/fail)
#     if tool_name in ("list_documents", "get_document"):
#         audit_id = str(uuid.uuid4())
#
#         # sanitize model-provided params
#         project_id = _norm_empty(params.get("project_id"))
#         older_than_days = params.get("older_than_days", 0) or 0
#         status_filter = _norm_empty(params.get("status_filter"))
#
#         decision = "ALLOW"
#         reasons = ["read-only fast-path"]
#
#         client = CLIENT
#         if client is None:
#             client = httpx.AsyncClient(timeout=20.0)
#
#         if tool_name == "list_documents":
#             payload = {
#                 "project_id": project_id,
#                 "older_than_days": int(older_than_days),
#                 "status_filter": status_filter if status_filter is not None else "",
#             }
#             resp = await client.post(f"{TOOL_BACKEND_URL}/tools/list_documents", json=payload)
#         else:
#             doc_id = _norm_empty(params.get("doc_id") or params.get("entity_id"))
#             payload = {"doc_id": doc_id}
#             resp = await client.post(f"{TOOL_BACKEND_URL}/tools/get_document", json=payload)
#         if resp.status_code >= 400:
#             raise HTTPException(status_code=502, detail={"upstream": resp.text})
#
#         result = resp.json()
#
#         # Redact Restricted docs BEFORE returning to model
#         if tool_name == "list_documents":
#             docs = result.get("documents", [])
#             out_docs = []
#             for d in docs:
#                 if isinstance(d, dict) and d.get("sensitivity") == "Restricted":
#                     decision = "ALLOW_WITH_REDACTION"
#                     reasons = ["read-only fast-path", "redacted: Restricted"]
#                     out_docs.append(_redact_doc(d))
#                 else:
#                     out_docs.append(d)
#             result["documents"] = out_docs
#         else:
#             d = result.get("document")
#             if isinstance(d, dict) and d.get("sensitivity") == "Restricted":
#                 decision = "ALLOW_WITH_REDACTION"
#                 reasons = ["read-only fast-path", "redacted: Restricted"]
#                 result["document"] = _redact_doc(d)
#
#         # AUDIT READS (persisted, not just returned)
#         db.audit_insert(
#             audit_id=audit_id,
#             ts=datetime.utcnow().isoformat(),
#             actor=call.actor,
#             tool_name=tool_name,
#             decision=decision,
#             reasons="; ".join(reasons),
#             final_tool=tool_name,
#             executed=1,
#             world_version=result.get("world_version", db.get_version()),
#         )
#
#         return {
#             "status": "OK",
#             "decision": decision,
#             "reasons": reasons,
#             "transformed_tool": None,
#             "result": result,
#             "audit_id": audit_id,
#             "decision_latency_ms": int((time.perf_counter() - t0) * 1000),
#         }
#
#     # v1 Action expects: name, actor, targets(list), params(dict)
#     targets = params.get("doc_ids") or params.get("targets") or []
#     action = Action(
#         name=tool_name,
#         actor=call.actor,
#         targets=targets,
#         params=params,
#     )
#
#     world = WorldStateDB()
#     from atlas_core.context_engine import ContextEngine
#     ctx = ContextEngine(world)
#
#     audit_id = str(uuid.uuid4())
#
#     # v1 run_once returns an outcome object
#     outcome = run_once(world, ctx, action)
#     decision = outcome.gate.decision
#     reasons = outcome.gate.reasons
#     final_tool = outcome.audit.final_action  # string tool name in v1 audit
#
#     decision_latency_ms = int((time.perf_counter() - t0) * 1000)
#
#     # ESCALATE: hold request, generate approval URL + token
#     if decision == Decision.ESCALATE:
#         req_id = f"req_{audit_id[:8]}"
#         payload = json.dumps({"tool": tool_name, "params": params, "actor": call.actor}, sort_keys=True)
#         exp = datetime.utcnow() + timedelta(minutes=10)
#         token = _sign_token(payload, exp)
#
#         PENDING[req_id] = {
#             "payload": payload,
#             "tool_name": tool_name,
#             "call": call.model_dump(),
#             "created": datetime.utcnow().isoformat(),
#             "token": token,
#             "reasons": reasons,
#         }
#
#         db.audit_insert(
#             audit_id=audit_id,
#             ts=datetime.utcnow().isoformat(),
#             actor=call.actor,
#             tool_name=tool_name,
#             decision=str(decision),
#             reasons="; ".join(reasons),
#             final_tool=final_tool,
#             executed=0,
#             world_version=world.version,
#         )
#
#         return {
#             "status": "PENDING_APPROVAL",
#             "decision": str(decision),
#             "reasons": reasons,
#             "approval_url": f"http://localhost:9000/approve/{req_id}",
#             "audit_id": audit_id,
#             "decision_latency_ms": decision_latency_ms,
#         }
#
#     # BLOCK: reject
#     if decision == Decision.BLOCK:
#         db.audit_insert(
#             audit_id=audit_id,
#             ts=datetime.utcnow().isoformat(),
#             actor=call.actor,
#             tool_name=tool_name,
#             decision=str(decision),
#             reasons="; ".join(reasons),
#             final_tool=final_tool,
#             executed=0,
#             world_version=world.version,
#         )
#         raise HTTPException(status_code=403, detail={"decision": str(decision), "reasons": reasons, "audit_id": audit_id})
#
#     # ALLOW or DOWNGRADE: forward to real tool backend (only mutation tools reach here)
#     forward_tool = final_tool
#
#     async with httpx.AsyncClient(timeout=20.0) as client:
#         payload = {
#             "doc_ids": params.get("doc_ids", []),
#             "permanent": params.get("permanent", False),
#         }
#         resp = await client.post(f"{TOOL_BACKEND_URL}/tools/{forward_tool}", json=payload)
#
#     if resp.status_code >= 400:
#         raise HTTPException(status_code=502, detail={"upstream": resp.text})
#
#     db.audit_insert(
#         audit_id=audit_id,
#         ts=datetime.utcnow().isoformat(),
#         actor=call.actor,
#         tool_name=tool_name,
#         decision=str(decision),
#         reasons="; ".join(reasons),
#         final_tool=forward_tool,
#         executed=1,
#         world_version=db.get_version(),
#     )
#
#     return {
#         "status": "OK",
#         "decision": str(decision),
#         "reasons": reasons,
#         "transformed_tool": forward_tool if forward_tool != tool_name else None,
#         "result": resp.json(),
#         "audit_id": audit_id,
#         "decision_latency_ms": decision_latency_ms,
#     }
#
#
# @app.get("/approve/{req_id}", response_class=HTMLResponse)
# def approve_page(req_id: str):
#     item = PENDING.get(req_id)
#     if not item:
#         return HTMLResponse("<h2>Not found</h2>", status_code=404)
#
#     reasons = "".join(f"<li>{r}</li>" for r in item.get("reasons", []))
#     return f"""
#     <html><body style="font-family:system-ui;max-width:800px;margin:40px auto">
#       <h2>Pending Approval: {req_id}</h2>
#       <p><b>Tool:</b> {item["tool_name"]}</p>
#       <p><b>Actor:</b> {item["call"].get("actor")}</p>
#       <p><b>Reasons:</b></p>
#       <ul>{reasons}</ul>
#       <pre style="background:#f6f6f6;padding:12px;border-radius:8px">{item["payload"]}</pre>
#       <form method="post">
#         <button name="decision" value="approve" style="padding:10px 16px;margin-right:10px">Approve</button>
#         <button name="decision" value="reject" style="padding:10px 16px">Reject</button>
#       </form>
#     </body></html>
#     """
#
#
# @app.post("/approve/{req_id}")
# async def approve_submit(req_id: str, decision: str = Form(...)):
#     item = PENDING.get(req_id)
#     if not item:
#         raise HTTPException(status_code=404, detail="not found")
#
#     if decision == "reject":
#         PENDING.pop(req_id, None)
#         return {"status": "REJECTED", "req_id": req_id}
#
#     payload = item["payload"]
#     token = item["token"]
#     if not _verify_token(payload, token):
#         raise HTTPException(status_code=401, detail="invalid/expired token")
#
#     call = item["call"]
#     tool_name = item["tool_name"]
#     params = (call or {}).get("params") or {}
#
#     # Approvals only exist for mutation tools in this demo
#     forward_payload = {
#         "doc_ids": params.get("doc_ids", []),
#         "permanent": params.get("permanent", False),
#     }
#
#     async with httpx.AsyncClient(timeout=20.0) as client:
#         resp = await client.post(f"{TOOL_BACKEND_URL}/tools/{tool_name}", json=forward_payload)
#
#     if resp.status_code >= 400:
#         raise HTTPException(status_code=502, detail={"upstream": resp.text})
#
#     PENDING.pop(req_id, None)
#     return {"status": "APPROVED_AND_EXECUTED", "req_id": req_id, "result": resp.json()}

from __future__ import annotations

import os, json, time, uuid, hmac, hashlib
from datetime import datetime, timedelta
from typing import Any, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from atlas_core.actions import Action
from atlas_core.loop import run_once, build_default_context
from atlas_core.types import Decision

from services.world_state_db import WorldStateDB
from services import db

TOOL_BACKEND_URL = os.environ.get("TOOL_BACKEND_URL", "http://localhost:9001")
SECRET = os.environ.get("ATLAS_HMAC_SECRET", "dev-secret-change-me")

app = FastAPI(title="ATLAS Proxy", version="0.1")
CLIENT: httpx.AsyncClient | None = None

# Pending escalation store (demo). In prod, durable store.
PENDING: Dict[str, Dict[str, Any]] = {}


@app.on_event("startup")
async def _startup():
    global CLIENT
    CLIENT = httpx.AsyncClient(timeout=20.0)


@app.on_event("shutdown")
async def _shutdown():
    global CLIENT
    if CLIENT:
        await CLIENT.aclose()
        CLIENT = None


class ToolCall(BaseModel):
    actor: str = "agent"
    params: Dict[str, Any] = {}


def _norm_empty(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return ""
        s_low = s.lower()
        if s_low in ("null", "none"):
            return None
        if s in ("&quot;&quot;", '""'):
            return ""
        if s_low == "default":
            return ""
        return s
    return v


def _redact_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entity_id": "REDACTED",
        "project_id": doc.get("project_id"),
        "sensitivity": doc.get("sensitivity"),
        "status": doc.get("status"),
        "trust_boundary": doc.get("trust_boundary"),
        "is_present": doc.get("is_present"),
        "note": "Restricted document fields redacted by ATLAS read policy",
    }


def _sign_token(payload: str, exp: datetime) -> str:
    msg = f"{payload}|{int(exp.timestamp())}".encode()
    sig = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"{sig}.{int(exp.timestamp())}"


def _verify_token(payload: str, token: str) -> bool:
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


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/tool/{tool_name}")
async def intercept(tool_name: str, call: ToolCall, request: Request) -> Dict[str, Any]:
    t0 = time.perf_counter()
    params = call.params or {}

    # -------------------------------------------------------------------------
    # FAST-PATH READ TOOLS (no target-based gating)
    # -------------------------------------------------------------------------
    if tool_name in ("list_documents", "get_document"):
        audit_id = str(uuid.uuid4())

        project_id = _norm_empty(params.get("project_id"))
        older_than_days = int(params.get("older_than_days", 0) or 0)
        status_filter = _norm_empty(params.get("status_filter"))

        decision = "ALLOW"
        reasons = ["read-only fast-path"]

        client = CLIENT
        if client is None:
            client = httpx.AsyncClient(timeout=20.0)

        if tool_name == "list_documents":
            payload = {
                "project_id": project_id,
                "older_than_days": older_than_days,
                # default to "" (no filter) rather than forcing "Active"
                "status_filter": status_filter if status_filter is not None else "",
            }
            resp = await client.post(f"{TOOL_BACKEND_URL}/tools/list_documents", json=payload)
        else:
            doc_id = _norm_empty(params.get("doc_id") or params.get("entity_id"))
            payload = {"doc_id": doc_id}
            resp = await client.post(f"{TOOL_BACKEND_URL}/tools/get_document", json=payload)

        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail={"upstream": resp.text})

        result = resp.json()

        # Redact Restricted docs BEFORE returning to model
        if tool_name == "list_documents":
            docs = result.get("documents", [])
            out_docs = []
            for d in docs:
                if isinstance(d, dict) and d.get("sensitivity") == "Restricted":
                    decision = "ALLOW_WITH_REDACTION"
                    reasons = ["read-only fast-path", "redacted: Restricted"]
                    out_docs.append(_redact_doc(d))
                else:
                    out_docs.append(d)
            result["documents"] = out_docs
        else:
            d = result.get("document")
            if isinstance(d, dict) and d.get("sensitivity") == "Restricted":
                decision = "ALLOW_WITH_REDACTION"
                reasons = ["read-only fast-path", "redacted: Restricted"]
                result["document"] = _redact_doc(d)

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

        return {
            "status": "OK",
            "decision": decision,
            "reasons": reasons,
            "transformed_tool": None,
            "result": result,
            "audit_id": audit_id,
            "decision_latency_ms": int((time.perf_counter() - t0) * 1000),
        }

    # -------------------------------------------------------------------------
    # MUTATION TOOLS (ATLAS gate loop + context engine)
    # -------------------------------------------------------------------------
    targets = params.get("doc_ids") or params.get("targets") or []
    action = Action(
        name=tool_name,
        actor=call.actor,
        targets=targets,
        params=params,
    )

    world = WorldStateDB()
    ctx = build_default_context(world)

    audit_id = str(uuid.uuid4())

    outcome = run_once(world, ctx, action)
    decision = outcome.gate.decision
    reasons = outcome.gate.reasons

    decision_latency_ms = int((time.perf_counter() - t0) * 1000)

    # ESCALATE: hold request, generate approval URL + token
    if decision == Decision.ESCALATE:
        req_id = f"req_{audit_id[:8]}"
        payload = json.dumps({"tool": tool_name, "params": params, "actor": call.actor}, sort_keys=True)
        exp = datetime.utcnow() + timedelta(minutes=10)
        token = _sign_token(payload, exp)

        PENDING[req_id] = {
            "payload": payload,
            "tool_name": tool_name,
            "call": call.model_dump(),
            "created": datetime.utcnow().isoformat(),
            "token": token,
            "reasons": reasons,
        }

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

        return {
            "status": "PENDING_APPROVAL",
            "decision": str(decision),
            "reasons": reasons,
            "approval_url": f"http://localhost:9000/approve/{req_id}",
            "audit_id": audit_id,
            "decision_latency_ms": decision_latency_ms,
        }

    # BLOCK: reject
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
            detail={"decision": str(decision), "reasons": reasons, "audit_id": audit_id},
        )

    # ALLOW or DOWNGRADE: forward to tool backend
    # NOTE: if DOWNGRADE happened inside run_once, your audit.final_action will be "archive_document"
    forward_tool = outcome.audit.final_action if outcome.audit.final_action != "none" else tool_name

    client = CLIENT
    if client is None:
        client = httpx.AsyncClient(timeout=20.0)

    forward_payload = {
        "doc_ids": params.get("doc_ids", []),
        "permanent": params.get("permanent", False),
    }

    resp = await client.post(f"{TOOL_BACKEND_URL}/tools/{forward_tool}", json=forward_payload)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail={"upstream": resp.text})

    db.audit_insert(
        audit_id=audit_id,
        ts=datetime.utcnow().isoformat(),
        actor=call.actor,
        tool_name=tool_name,
        decision=str(decision),
        reasons="; ".join(reasons),
        final_tool=forward_tool,
        executed=1,
        world_version=db.get_version(),
    )

    return {
        "status": "OK",
        "decision": str(decision),
        "reasons": reasons,
        "transformed_tool": forward_tool if forward_tool != tool_name else None,
        "result": resp.json(),
        "audit_id": audit_id,
        "decision_latency_ms": decision_latency_ms,
    }


@app.get("/approve/{req_id}", response_class=HTMLResponse)
def approve_page(req_id: str):
    item = PENDING.get(req_id)
    if not item:
        return HTMLResponse("<h2>Not found</h2>", status_code=404)

    reasons = "".join(f"<li>{r}</li>" for r in item.get("reasons", []))
    return f"""
    <html><body style="font-family:system-ui;max-width:800px;margin:40px auto">
      <h2>Pending Approval: {req_id}</h2>
      <p><b>Tool:</b> {item["tool_name"]}</p>
      <p><b>Actor:</b> {item["call"].get("actor")}</p>
      <p><b>Reasons:</b></p>
      <ul>{reasons}</ul>
      <pre style="background:#f6f6f6;padding:12px;border-radius:8px">{item["payload"]}</pre>
      <form method="post">
        <button name="decision" value="approve" style="padding:10px 16px;margin-right:10px">Approve</button>
        <button name="decision" value="reject" style="padding:10px 16px">Reject</button>
      </form>
    </body></html>
    """


@app.post("/approve/{req_id}")
async def approve_submit(req_id: str, decision: str = Form(...)):
    item = PENDING.get(req_id)
    if not item:
        raise HTTPException(status_code=404, detail="not found")

    if decision == "reject":
        PENDING.pop(req_id, None)
        return {"status": "REJECTED", "req_id": req_id}

    payload = item["payload"]
    token = item["token"]
    if not _verify_token(payload, token):
        raise HTTPException(status_code=401, detail="invalid/expired token")

    call = item["call"]
    tool_name = item["tool_name"]
    params = (call or {}).get("params") or {}

    forward_payload = {
        "doc_ids": params.get("doc_ids", []),
        "permanent": params.get("permanent", False),
    }

    client = CLIENT
    if client is None:
        client = httpx.AsyncClient(timeout=20.0)

    resp = await client.post(f"{TOOL_BACKEND_URL}/tools/{tool_name}", json=forward_payload)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail={"upstream": resp.text})

    PENDING.pop(req_id, None)
    return {"status": "APPROVED_AND_EXECUTED", "req_id": req_id, "result": resp.json()}