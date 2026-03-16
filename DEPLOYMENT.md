# ATLAS Production Deployment Guide

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- Docker & Docker Compose (optional, for containerized deployment)
- AWS credentials configured (for Bedrock agent)

### 1. Install Dependencies

```bash
pip install -r requirements-prod.txt
```

### 2. Initialize Database

```bash
python init_db.py
```

This creates `demo_world.db` with seed entities.

### 3. Start ATLAS Proxy

**Option A: Direct Python**
```bash
python atlas_proxy_prod.py
```

**Option B: Docker Compose**
```bash
docker-compose up -d
```

You should see:
```
[Zeroconf] ATLAS proxy advertised as 'ATLAS Production Proxy' on 192.168.1.X:9000
```

### 4. Start Agent

In a new terminal:
```bash
# Configure AWS credentials
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# Run agent (auto-discovers ATLAS via Zeroconf)
python nova_agent_prod.py
```

The agent will automatically discover the ATLAS proxy:
```
[Discovery] Searching for ATLAS proxy via Zeroconf...
[Discovery] ✅ Found ATLAS proxy: http://192.168.1.X:9000
```

---

## 🏗️ Architecture Overview

```
┌─────────────┐
│ Nova Agent  │  ← Auto-discovers ATLAS via Zeroconf
└──────┬──────┘
       │ HTTP tool calls
       ↓
┌─────────────────────┐
│   ATLAS Proxy       │  ← Transparent intercept layer
│   (Port 9000)       │  ← P0→P1→P2-P4 evaluation
│                     │  ← NEEDS_CONTEXT loop
│   • Admissibility   │  ← Approval queue
│   • Context Engine  │  ← Audit logging
│   • Approval UI     │
└──────┬──────────────┘
       │ Forwards ALLOW/DOWNGRADE
       ↓
┌─────────────────────┐
│  Tool Backend       │  ← Real tool implementations
│  (Port 9001)        │
│                     │
│  • list_documents   │
│  • get_document     │
│  • archive_document │
│  • delete_document  │
└─────────────────────┘
       │
       ↓
┌─────────────────────┐
│   SQLite DB         │  ← World state
│   (demo_world.db)   │  ← Audit trail
└─────────────────────┘
```

---

## 🔒 Key Features

### 1. **Zeroconf Service Discovery**
- Agents auto-discover ATLAS on local network
- No hardcoded URLs or manual configuration
- Multi-proxy support (load balancing)

### 2. **Confirmation Token Retry Flow**
When agent tries a high-risk action:

```
Agent → delete doc:Q4_Legal_Contract
         ↓
ATLAS → ESCALATE (P0.3: Restricted sensitivity)
         ↓ Returns:
         • approval_url: http://192.168.1.X:9000/approve/req_abc123
         • confirmation_token: "sig.expiry"
         ↓
Human → Opens approval_url in browser
        → Clicks "Approve"
         ↓
Agent → Retries with confirmation_token
         ↓
ATLAS → P0.3 passes (token valid)
         ↓
Tool  → Executes delete
```

### 3. **Progressive Context Retrieval (NEEDS_CONTEXT)**
Instead of prompt stuffing, ATLAS requests typed facts:

```
Agent → delete doc:Roadmap_2024
         ↓
ATLAS → NEEDS_CONTEXT
         • NeedFact("entity", "doc:Roadmap_2024", "Need retention_days")
         ↓
Context Engine → entity_peek("doc:Roadmap_2024")
         ↓ Returns: {retention_days: 365, age_days: 200}
         ↓
ATLAS → Re-evaluate with evidence
         ↓ P1.2: age < retention → DOWNGRADE
         ↓
Tool  → archive_document (safer alternative)
```

### 4. **Lexicographic Policy Evaluation**

```
P0: Safety Invariants (Hard Stops)
 ├─ P0.1: Lock conflicts → BLOCK
 ├─ P0.2: Legal hold → BLOCK
 ├─ P0.3: High sensitivity → ESCALATE
 └─ P0.4: Trust boundary → ESCALATE

P1: World Validity
 ├─ P1.1: Existence check
 ├─ P1.2: Retention window → DOWNGRADE
 └─ P1.3: Active dependencies → DOWNGRADE

P2-P4: Soft Predicates (Goal, Efficiency, UX)
```

If any level fails, lower levels are not checked.

---

## 🧪 Testing Scenarios

### Scenario 1: ALLOW (Clean Delete)
```
> Delete doc:Old_Project_Notes_2022
```
**Expected:**
- ✅ ALLOW
- Age: 730 days, Retention: 180 days
- Successfully deleted

### Scenario 2: DOWNGRADE (Retention Window)
```
> Delete doc:Roadmap_2024
```
**Expected:**
- ⬇️ DOWNGRADE
- Age: 200 days < Retention: 365 days
- Archived instead of deleted

### Scenario 3: BLOCK (Legal Hold)
```
> Delete doc:Q4_Legal_Contract
```
**Expected:**
- 🚫 BLOCK
- Reason: P0.2 legal hold violation
- No execution

### Scenario 4: ESCALATE (High Sensitivity)
*If legal_hold is removed but sensitivity=Restricted:*
```
> Delete doc:Q4_Legal_Contract
```
**Expected:**
- ⚠️ ESCALATE
- Reason: P0.3 requires human confirmation
- Approval URL provided

Agent response:
```
🤖 I need human approval for this action.
   Please review at: http://192.168.1.X:9000/approve/req_abc123
   
   Once approved, I'll automatically retry.
```

### Scenario 5: BLOCK (Lock Conflict)
```
> Delete doc:Active_Incident_Runbook
```
**Expected:**
- 🚫 BLOCK
- Reason: P0.1 Exclusive lock held by agent.oncall
- Current actor (agent.nova) cannot modify

---

## 🎛️ Admin Dashboard

Access at: `http://localhost:9000/admin`

Features:
- System status (world version, pending approvals)
- Approval queue with one-click review
- Recent audit log (last 50 actions)
- Real-time decision metrics

---

## 📊 Monitoring

### Health Checks
```bash
curl http://localhost:9000/health
```

Returns:
```json
{
  "ok": true,
  "service": "ATLAS Proxy v2",
  "world_version": 42,
  "pending_approvals": 2
}
```

### Audit Log Query
```bash
sqlite3 demo_world.db "SELECT * FROM audit_log ORDER BY ts DESC LIMIT 10"
```

### Metrics to Track
- Decision latency (ms)
- Decision distribution (ALLOW/BLOCK/ESCALATE/DOWNGRADE)
- Approval queue depth
- Cache hit rate (context engine)

---

## 🔧 Configuration

### Environment Variables

**ATLAS Proxy:**
```bash
ATLAS_HOST=0.0.0.0              # Bind address
ATLAS_PORT=9000                 # Proxy port
TOOL_BACKEND_URL=http://localhost:9001
ATLAS_HMAC_SECRET=<random>     # For confirmation tokens
ATLAS_SERVICE_NAME="My ATLAS"  # Zeroconf name
```

**Agent:**
```bash
NOVA_MODEL_ID=amazon.nova-pro-v1:0
AWS_REGION=us-east-1
ATLAS_ACTOR_ID=agent.nova      # Actor identity
```

---

## 🐛 Troubleshooting

### Agent Can't Discover ATLAS
```
[Discovery] ❌ No ATLAS proxy found
```

**Fix:**
1. Ensure ATLAS proxy is running: `curl http://localhost:9000/health`
2. Check firewall allows mDNS (port 5353)
3. Verify same network segment (Zeroconf is LAN-only)
4. Manual fallback: Set `ATLAS_BASE_URL=http://localhost:9000` in agent

### ESCALATE Not Working
```
⚠️ ESCALATE but no approval URL
```

**Fix:**
1. Check ATLAS logs for approval creation
2. Verify HMAC secret is consistent
3. Ensure approval queue is not full

### Tool Backend Unreachable
```
502 Bad Gateway: upstream error
```

**Fix:**
1. Start tool backend: `uvicorn tool_backend:app --port 9001`
2. Verify `TOOL_BACKEND_URL` in proxy config
3. Check Docker network if containerized

---

## 🚢 Production Deployment

### Security Hardening

1. **HMAC Secret:**
```bash
export ATLAS_HMAC_SECRET=$(openssl rand -hex 32)
```

2. **HTTPS:**
Use reverse proxy (nginx/Caddy) with TLS:
```nginx
server {
    listen 443 ssl;
    server_name atlas.company.internal;
    
    location / {
        proxy_pass http://localhost:9000;
    }
}
```

3. **Database:**
Replace SQLite with PostgreSQL:
- Update `services/db.py` to use `psycopg2`
- Set `DATABASE_URL` env var

4. **Approval Queue:**
Replace in-memory dict with Redis:
```python
# In atlas_proxy_prod.py
import redis
approval_queue = RedisApprovalQueue(redis.Redis(...))
```

### Scaling

**Horizontal:**
- Run multiple ATLAS proxies (stateless)
- Use Redis for shared approval queue
- Load balance with HAProxy/nginx

**Vertical:**
- Increase worker count: `uvicorn --workers 4`
- Tune connection pools
- Cache world state snapshots

### Observability

**Structured Logging:**
```python
import structlog
logger = structlog.get_logger()
logger.info("decision", decision=decision, latency=latency_ms)
```

**Prometheus Metrics:**
```python
from prometheus_client import Counter, Histogram

decision_counter = Counter("atlas_decisions", "Total decisions", ["decision"])
latency_histogram = Histogram("atlas_latency", "Decision latency")
```

**Distributed Tracing:**
```python
from opentelemetry import trace
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("evaluate_admissibility"):
    ...
```

---

## 📚 API Reference

### POST /tool/{tool_name}

**Request:**
```json
{
  "actor": "agent.nova",
  "params": {
    "doc_ids": ["doc:example"]
  },
  "confirmation_token": "optional_for_retry"
}
```

**Response (ALLOW):**
```json
{
  "status": "OK",
  "decision": "ALLOW",
  "reasons": ["All policies passed"],
  "result": {"deleted": ["doc:example"]},
  "audit_id": "aud_abc123",
  "decision_latency_ms": 45,
  "world_version": 42
}
```

**Response (ESCALATE):**
```json
{
  "status": "PENDING_APPROVAL",
  "decision": "ESCALATE",
  "reasons": ["P0.3: Restricted sensitivity requires approval"],
  "approval_url": "http://192.168.1.X:9000/approve/req_abc123",
  "confirmation_token": "sig.expiry",
  "audit_id": "aud_abc123",
  "decision_latency_ms": 52,
  "world_version": 42
}
```

**Response (BLOCK):**
```
HTTP 403 Forbidden
{
  "detail": {
    "status": "BLOCKED",
    "decision": "BLOCK",
    "reasons": ["P0.2: Legal hold prevents deletion"],
    "audit_id": "aud_abc123"
  }
}
```

---

## 🎓 Next Steps

1. **Custom Policies:** Edit `atlas_core/admissibility.py` to add domain-specific rules
2. **New Tools:** Add adapters in `services/` and register in `tool_backend.py`
3. **UI Integration:** Build React dashboard consuming `/api/audit` endpoint
4. **Multi-Agent:** Run multiple agents with different `ATLAS_ACTOR_ID` values
5. **External Tools:** Point `TOOL_BACKEND_URL` to real production APIs

---

## 📖 References

- **Paper:** ATLAS v1: World-State Admissibility and Contextual Retrieval (arXiv:2603.00495)
- **Zeroconf:** https://python-zeroconf.readthedocs.io/
- **FastAPI:** https://fastapi.tiangolo.com/
- **AWS Bedrock:** https://docs.aws.amazon.com/bedrock/

---

## 🤝 Support

For issues or questions:
1. Check logs: `docker-compose logs atlas-proxy`
2. Verify health: `curl http://localhost:9000/health`
3. Review audit trail in admin dashboard

**Production Support:**
- Enable structured logging
- Set up Sentry for error tracking
- Configure PagerDuty for approval queue alerts
