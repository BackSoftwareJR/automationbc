# automationbc — n8n → Cursor CLI Bridge

Local FastAPI middleware that receives webhooks from **n8n**, runs **Cursor CLI** (`agent`) asynchronously, and POSTs results back to n8n.

## Quick start (Windows)

```powershell
.\setup.bat
copy .env.example .env   # set BRIDGE_API_KEY and N8N_CALLBACK_URL
.\start.bat
.\start-ngrok.bat        # optional: expose to n8n Cloud
```

## Quick start (macOS / Linux)

```bash
chmod +x setup.sh
./setup.sh
cp .env.example .env
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8787
```

## API

| Endpoint | Auth | Response | Description |
|----------|------|----------|-------------|
| `GET /health` | No | 200 | Health check |
| `POST /api/v1/execute-agent` | `X-API-Key` | **202** | Queue agent run (async) |

### Inbound payload (JSON)

```json
{
  "dedicated_prompt": "Your task for the Cursor agent",
  "task_id": "task-42",
  "project_id": 101,
  "project_area": "bs-webdev",
  "context": { "source": "n8n" }
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `dedicated_prompt` | Yes | Prompt sent to `agent` |
| `task_id` | Yes | string or int |
| `project_id` | Yes | string or int |
| `project_area` | No | Default `bs-webdev` (workspace mapping) |
| `context` | No | Optional metadata dict |

### Immediate response (202 Accepted)

```json
{
  "status": "accepted",
  "task_id": "task-42"
}
```

n8n should not wait for the agent to finish — only for this 202.

### Callback POST (`N8N_CALLBACK_URL`)

After the agent completes (success, error, or timeout), the bridge POSTs:

```json
{
  "task_id": "task-42",
  "project_id": 101,
  "status": "success",
  "summary": "truncated stdout/stderr..."
}
```

`status` is `"success"` or `"error"`.

## Configuration

Copy `.env.example` to `.env`. Never commit `.env`.

| Variable | Description |
|----------|-------------|
| `BRIDGE_API_KEY` | Required. `X-API-Key` header |
| `N8N_CALLBACK_URL` | Required. n8n webhook for results |
| `CALLBACK_TIMEOUT_SEC` | HTTP timeout for callback (default 30) |
| `CALLBACK_SUMMARY_MAX_CHARS` | Max summary length (default 4000) |
| `WORKSPACE_BS_WEBDEV` | Path for `project_area` mapping |
| `AGENT_USE_WSL` | `true` on Windows to run `agent` inside WSL |
| `CURSOR_API_KEY` | Optional Cursor CLI auth |

## Secure exposure (n8n Cloud)

Use **ngrok** or Cloudflare Tunnel — do not port-forward without a firewall.

```
POST https://YOUR-TUNNEL.ngrok-free.app/api/v1/execute-agent
Header: X-API-Key: <BRIDGE_API_KEY>
```

## n8n workflow pattern

1. **HTTP Request** → bridge `/execute-agent` → expect **202**
2. Separate **Webhook** node (or Wait) at `N8N_CALLBACK_URL` → receives final `status` + `summary`

## License

Private project — BackSoftwareJR.
