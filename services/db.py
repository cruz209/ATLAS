from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "demo_world.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    con = _conn()
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS meta(
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entities(
            entity_id TEXT PRIMARY KEY,
            project_id TEXT,
            owner TEXT,
            sensitivity TEXT,
            lock_status TEXT,
            lock_owner TEXT,
            retention_days INTEGER,
            legal_hold INTEGER,
            irreversible_score REAL,
            last_modified TEXT,
            last_accessed TEXT,
            status TEXT,
            trust_boundary TEXT,
            is_present INTEGER
        );

        CREATE TABLE IF NOT EXISTS tags(
            entity_id TEXT,
            k TEXT,
            v TEXT,
            PRIMARY KEY(entity_id, k),
            FOREIGN KEY(entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS dependencies(
            entity_id TEXT,
            dep_id TEXT,
            PRIMARY KEY(entity_id, dep_id),
            FOREIGN KEY(entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS edges(
            src TEXT,
            rel TEXT,
            dst TEXT,
            PRIMARY KEY(src, rel, dst)
        );

        CREATE TABLE IF NOT EXISTS audit_log(
            audit_id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            actor TEXT,
            tool_name TEXT,
            decision TEXT,
            reasons TEXT,
            final_tool TEXT,
            executed INTEGER,
            world_version INTEGER
        );
        """
    )
    cur.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('version','0')")
    con.commit()
    con.close()


def bump_version(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    cur.execute("SELECT v FROM meta WHERE k='version'")
    v = int(cur.fetchone()[0])
    v += 1
    cur.execute("UPDATE meta SET v=? WHERE k='version'", (str(v),))
    return v


def get_version() -> int:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT v FROM meta WHERE k='version'")
    v = int(cur.fetchone()[0])
    con.close()
    return v


def upsert_entity(e: Dict[str, Any]) -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO entities(
            entity_id, project_id, owner, sensitivity, lock_status, lock_owner,
            retention_days, legal_hold, irreversible_score, last_modified, last_accessed,
            status, trust_boundary, is_present
        )
        VALUES(
            :entity_id,:project_id,:owner,:sensitivity,:lock_status,:lock_owner,
            :retention_days,:legal_hold,:irreversible_score,:last_modified,:last_accessed,
            :status,:trust_boundary,:is_present
        )
        ON CONFLICT(entity_id) DO UPDATE SET
            project_id=excluded.project_id,
            owner=excluded.owner,
            sensitivity=excluded.sensitivity,
            lock_status=excluded.lock_status,
            lock_owner=excluded.lock_owner,
            retention_days=excluded.retention_days,
            legal_hold=excluded.legal_hold,
            irreversible_score=excluded.irreversible_score,
            last_modified=excluded.last_modified,
            last_accessed=excluded.last_accessed,
            status=excluded.status,
            trust_boundary=excluded.trust_boundary,
            is_present=excluded.is_present
        """,
        e,
    )
    bump_version(con)
    con.commit()
    con.close()


def seed_demo_world() -> None:
    con = _conn()
    cur = con.cursor()
    cur.executescript(
        """
        DELETE FROM edges;
        DELETE FROM dependencies;
        DELETE FROM tags;
        DELETE FROM entities;
        DELETE FROM audit_log;
        UPDATE meta SET v='0' WHERE k='version';
        """
    )

    now = datetime.utcnow()
    iso = lambda dt: dt.isoformat()

    entities = [
        dict(
            entity_id="doc:Active_Incident_Runbook",
            project_id="proj:ops",
            owner="team:sre",
            sensitivity="Internal",
            lock_status="Exclusive",
            lock_owner="agent.oncall",
            retention_days=365,
            legal_hold=0,
            irreversible_score=0.9,
            last_modified=iso(now - timedelta(days=200)),
            last_accessed=iso(now - timedelta(days=1)),
            status="Active",
            trust_boundary="default",
            is_present=1,
        ),
        dict(
            entity_id="doc:Q4_Legal_Contract",
            project_id="proj:legal",
            owner="team:legal",
            sensitivity="Restricted",
            lock_status=None,
            lock_owner=None,
            retention_days=2555,
            legal_hold=1,
            irreversible_score=0.95,
            last_modified=iso(now - timedelta(days=300)),
            last_accessed=iso(now - timedelta(days=3)),
            status="Active",
            trust_boundary="default",
            is_present=1,
        ),
        dict(
            entity_id="doc:Old_Project_Notes_2022",
            project_id="proj:archive",
            owner="team:engineering",
            sensitivity="Internal",
            lock_status=None,
            lock_owner=None,
            retention_days=180,
            legal_hold=0,
            irreversible_score=0.7,
            last_modified=iso(now - timedelta(days=730)),
            last_accessed=iso(now - timedelta(days=120)),
            status="Active",
            trust_boundary="default",
            is_present=1,
        ),
        dict(
            entity_id="doc:Roadmap_2024",
            project_id="proj:strategy",
            owner="team:product",
            sensitivity="Internal",
            lock_status=None,
            lock_owner=None,
            retention_days=365,
            legal_hold=0,
            irreversible_score=0.8,
            last_modified=iso(now - timedelta(days=200)),
            last_accessed=iso(now - timedelta(days=10)),
            status="Active",
            trust_boundary="default",
            is_present=1,
        ),
        dict(
            entity_id="workflow:quarterly-review",
            project_id="proj:strategy",
            owner="team:product",
            sensitivity="Internal",
            lock_status=None,
            lock_owner=None,
            retention_days=9999,
            legal_hold=0,
            irreversible_score=0.1,
            last_modified=iso(now - timedelta(days=5)),
            last_accessed=iso(now - timedelta(days=5)),
            status="Active",
            trust_boundary="default",
            is_present=1,
        ),
    ]

    for e in entities:
        cur.execute(
            """
            INSERT INTO entities(
                entity_id, project_id, owner, sensitivity, lock_status, lock_owner,
                retention_days, legal_hold, irreversible_score, last_modified, last_accessed,
                status, trust_boundary, is_present
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                e["entity_id"], e["project_id"], e["owner"], e["sensitivity"],
                e["lock_status"], e["lock_owner"], e["retention_days"], e["legal_hold"],
                e["irreversible_score"], e["last_modified"], e["last_accessed"],
                e["status"], e["trust_boundary"], e["is_present"],
            ),
        )

    edges = [
        ("doc:Active_Incident_Runbook", "BELONGS_TO", "proj:ops"),
        ("doc:Q4_Legal_Contract", "BELONGS_TO", "proj:legal"),
        ("doc:Q4_Legal_Contract", "SUBJECT_TO", "policy:legal-hold-2024"),
        ("doc:Old_Project_Notes_2022", "BELONGS_TO", "proj:archive"),
        ("doc:Roadmap_2024", "BELONGS_TO", "proj:strategy"),
        ("doc:Roadmap_2024", "DEPENDS_ON", "workflow:quarterly-review"),
        ("workflow:quarterly-review", "REFERENCED_BY", "doc:Roadmap_2024"),
    ]
    cur.executemany("INSERT OR IGNORE INTO edges(src, rel, dst) VALUES(?,?,?)", edges)

    deps = [("doc:Roadmap_2024", "workflow:quarterly-review")]
    cur.executemany("INSERT OR IGNORE INTO dependencies(entity_id, dep_id) VALUES(?,?)", deps)

    tags = [
        ("doc:Active_Incident_Runbook","env","prod"),
        ("doc:Active_Incident_Runbook","team","sre"),
        ("doc:Q4_Legal_Contract","env","prod"),
        ("doc:Q4_Legal_Contract","team","legal"),
        ("doc:Old_Project_Notes_2022","env","dev"),
        ("doc:Old_Project_Notes_2022","team","engineering"),
        ("doc:Roadmap_2024","env","prod"),
        ("doc:Roadmap_2024","team","product"),
    ]
    cur.executemany("INSERT OR IGNORE INTO tags(entity_id,k,v) VALUES(?,?,?)", tags)

    con.commit()
    con.close()


def fetch_entity(entity_id: str) -> Optional[Dict[str, Any]]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,))
    row = cur.fetchone()
    if not row:
        con.close()
        return None

    cur.execute("SELECT k,v FROM tags WHERE entity_id=?", (entity_id,))
    tags = {r["k"]: r["v"] for r in cur.fetchall()}

    cur.execute("SELECT dep_id FROM dependencies WHERE entity_id=?", (entity_id,))
    deps = [r["dep_id"] for r in cur.fetchall()]

    con.close()
    d = dict(row)
    d["tags"] = tags
    d["dependencies"] = deps
    return d


def list_documents(project_id: Optional[str], older_than_days: int, status_filter: str) -> List[Dict[str, Any]]:
    con = _conn()
    cur = con.cursor()

    q = "SELECT * FROM entities"
    args: List[Any] = []
    clauses = []

    if status_filter:
        clauses.append("status=?")
        args.append(status_filter)
    if project_id:
        clauses.append("project_id=?")
        args.append(project_id)

    if clauses:
        q += " WHERE " + " AND ".join(clauses)

    cur.execute(q, args)
    rows = cur.fetchall()

    out = []
    now = datetime.utcnow()
    for r in rows:
        last_modified = datetime.fromisoformat(r["last_modified"])
        if (now - last_modified).days < older_than_days:
            continue
        out.append(fetch_entity(r["entity_id"]) or dict(r))

    con.close()
    return out


def neighbors(start_ids: List[str], rel_types: Optional[List[str]], hops: int, limit: int) -> List[Tuple[str,str,str]]:
    con = _conn()
    cur = con.cursor()
    frontier = list(start_ids)
    out: List[Tuple[str,str,str]] = []
    seen = set()

    for _ in range(max(0, hops)):
        nxt = []
        for s in frontier:
            if rel_types:
                cur.execute(
                    "SELECT rel,dst FROM edges WHERE src=? AND rel IN (%s)"
                    % ",".join("?" * len(rel_types)),
                    [s, *rel_types],
                )
            else:
                cur.execute("SELECT rel,dst FROM edges WHERE src=?", (s,))
            for rel, dst in cur.fetchall():
                key = (s, rel, dst)
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)
                nxt.append(dst)
                if len(out) >= limit:
                    con.close()
                    return out
        frontier = nxt

    con.close()
    return out


def update_status(entity_id: str, status: str) -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute("UPDATE entities SET status=? WHERE entity_id=?", (status, entity_id))
    bump_version(con)
    con.commit()
    con.close()


def audit_insert(
    audit_id: str,
    ts: str,
    actor: str,
    tool_name: str,
    decision: str,
    reasons: str,
    final_tool: str,
    executed: int,
    world_version: int,
) -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO audit_log(
            audit_id, ts, actor, tool_name, decision,
            reasons, final_tool, executed, world_version
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (audit_id, ts, actor, tool_name, decision, reasons, final_tool, executed, world_version),
    )
    con.commit()
    con.close()