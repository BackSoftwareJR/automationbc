# automationbc — n8n → Cursor CLI Bridge

Local FastAPI middleware that receives webhooks from **n8n** and runs **Cursor CLI** (`agent`) on your machine.

## Quick start (Windows)

```powershell
.\setup.bat
copy .env.example .env   # set BRIDGE_API_KEY
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

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | No | Health check |
| `POST /api/v1/execute-agent` | `X-API-Key` | Run Cursor agent from n8n payload |

### Payload (JSON)

```json
{
  "project_area": "bs-webdev",
  "task_description": "Your task for the agent",
  "context": { "source": "n8n" }
}
```

## Configuration

Copy `.env.example` to `.env`. Never commit `.env`.

| Variable | Description |
|----------|-------------|
| `BRIDGE_API_KEY` | Required. Sent as `X-API-Key` header |
| `WORKSPACE_BS_WEBDEV` | Path for `project_area` mapping |
| `AGENT_USE_WSL` | `true` on Windows to run `agent` inside WSL |
| `CURSOR_API_KEY` | Optional Cursor CLI auth |

## Secure exposure (n8n Cloud)

Use **ngrok** or Cloudflare Tunnel — do not port-forward without a firewall.

```
POST https://YOUR-TUNNEL.ngrok-free.app/api/v1/execute-agent
Header: X-API-Key: <BRIDGE_API_KEY>
```

## License

Private project — BackSoftwareJR.
