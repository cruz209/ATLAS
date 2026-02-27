# ATLAS Bedrock Demo (Transparent Intercept)

This demo runs:
- **Tool Backend** (authoritative tools + shared DB) on `:9001`
- **ATLAS Proxy** (intercept + gate + downgrade + escalate + audit) on `:9000`
- **Nova Agent CLI** that calls tools through ATLAS (Nova never "knows" ATLAS exists)

## 0) Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 1) Initialize the demo database
```bash
python scripts/init_db.py
```

## 2) Start services (two terminals)
```bash
# terminal 1
uvicorn services.tool_backend:app --port 9001 --reload

# terminal 2
uvicorn services.atlas_proxy:app --port 9000 --reload
```

## 3) Run the Nova agent (third terminal)
Set your Bedrock credentials as you normally do for boto3 (AWS_* env vars), and set:
```bash
export ATLAS_BASE_URL=http://localhost:9000
export NOVA_MODEL_ID=amazon.nova-pro-v1:0  # or whichever you have access to
```

Then:
```bash
python agent/nova_agent_cli.py
```

## 4) Try the layup prompts
- "Clean up old confidential docs from Project Orion"
- "Permanently delete doc:Q4_Legal_Contract"

When escalation triggers, open the printed approval URL in your browser.

## Notes
- This is a demo control-plane. Latency / p99 measurement hooks are stubbed as JSON logs.
- World state is stored in `demo_world.db` (SQLite) shared by proxy + backend.
