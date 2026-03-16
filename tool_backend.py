from __future__ import annotations
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from services import db
from atlas_core.world_state import DocStatus

app = FastAPI(title="Demo Tool Backend", version="0.1")

class ListReq(BaseModel):
    project_id: Optional[str] = None
    older_than_days: int = 0
    status_filter: str = DocStatus.ACTIVE

class DocReq(BaseModel):
    doc_id: str

class MultiDocReq(BaseModel):
    doc_ids: List[str] = Field(default_factory=list)
    permanent: bool = False

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/tools/list_documents")
def list_documents(req: ListReq) -> Dict[str, Any]:
    docs = db.list_documents(req.project_id, req.older_than_days, req.status_filter)
    return {"documents": docs, "world_version": db.get_version()}

@app.post("/tools/get_document")
def get_document(req: DocReq) -> Dict[str, Any]:
    d = db.fetch_entity(req.doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="doc not found")
    return {"document": d, "world_version": db.get_version()}

@app.post("/tools/archive_document")
def archive_document(req: MultiDocReq) -> Dict[str, Any]:
    for doc_id in req.doc_ids:
        d = db.fetch_entity(doc_id)
        if not d:
            raise HTTPException(status_code=404, detail=f"{doc_id} not found")
        db.update_status(doc_id, DocStatus.ARCHIVED)
    return {"archived": req.doc_ids, "world_version": db.get_version()}

@app.post("/tools/delete_document")
def delete_document(req: MultiDocReq) -> Dict[str, Any]:
    # Demo semantics:
    # - permanent=True sets status DELETED; otherwise also DELETED (kept simple)
    for doc_id in req.doc_ids:
        d = db.fetch_entity(doc_id)
        if not d:
            raise HTTPException(status_code=404, detail=f"{doc_id} not found")
        db.update_status(doc_id, DocStatus.DELETED)
    return {"deleted": req.doc_ids, "permanent": req.permanent, "world_version": db.get_version()}
