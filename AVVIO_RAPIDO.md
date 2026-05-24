# Avvio rapido — Bridge n8n → Cursor CLI

Guida passo-passo per Windows. Tieni **due terminali aperti** mentre lavori con n8n Cloud.

Cartella di lavoro:

```
C:\Users\ACER\Desktop\main-bs-web\server-local
```

---

## Prima volta (solo una volta)

### 1. Setup Python e dipendenze

```powershell
cd C:\Users\ACER\Desktop\main-bs-web\server-local
.\setup.bat
```

Se Python manca: `.\install-python.bat`, chiudi e riapri PowerShell, poi rilancia `.\setup.bat`.

### 2. File `.env`

```powershell
copy .env.example .env
```

Modifica `.env` e imposta almeno:

| Variabile | Cosa mettere |
|-----------|--------------|
| `BRIDGE_API_KEY` | Chiave segreta lunga (header `X-API-Key` per n8n) |
| `N8N_CALLBACK_URL` | URL webhook n8n che riceve il risultato del task |
| `WORKSPACE_BS_WEBDEV` | Percorso cartella progetto (es. `..` = main-bs-web) |

### 3. Cursor CLI (agent)

**Nativo Windows:**

```powershell
irm 'https://cursor.com/install?win32=true' | iex
agent login
agent --version
```

**Oppure via WSL** (in `.env`: `AGENT_USE_WSL=true`).

### 4. ngrok (solo se n8n è in cloud)

1. Account su [ngrok.com](https://ngrok.com)
2. Token da [dashboard.ngrok.com](https://dashboard.ngrok.com/get-started/your-authtoken)
3. Configura una volta:

```powershell
.\ngrok-setup.bat IL_TUO_TOKEN_NGROK
```

Se ngrok è vecchio:

```powershell
& "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe" update
```

---

## Garanzie anti-blocco (automation)

Il bridge è progettato per **non richiedere la tua presenza** al terminale:

| Meccanismo | Cosa evita |
|------------|------------|
| Risposta **202 immediata** a n8n | Timeout webhook n8n |
| **Thread pool** dedicato (`AGENT_WORKER_THREADS=4`) | API/dashboard bloccate durante task lunghi |
| `stdin` chiuso + `CREATE_NO_WINDOW` | Prompt PowerShell nel terminale bridge |
| **Timeout** agent (`AGENT_TIMEOUT_SEC=0` = disabilitato) + ping `progress` ogni `AGENT_PROGRESS_CALLBACK_SEC` | Task lunghi senza kill a 10 min |
| **HALT** → `taskkill /T` | Agent figli che continuano dopo cancel |
| Polling cancel ogni 1s durante esecuzione | Cancel reattivo |
| **Retry** agent (max 5) + callback retry (max 5) | Fallimenti temporanei |
| Prompt Cursor: `--force --trust --yolo --approve-mcps` | Approvazioni CLI interattive |
| Direttive headless nel prompt | `curl`/git interattivi |

**Prerequisiti Cursor (una volta):** `agent login` o `CURSOR_API_KEY` in `.env`.

**Limite noto:** se la sessione Cursor scade, il task fallisce con callback `error` (non resta bloccato in attesa).

### Non devi approvare nulla nel Terminale 1

- Lascia aperto solo `.\start.bat` — **non digitare comandi lì**
- Test HTTP in un **terzo terminale** con `curl.exe`
- Usa la **dashboard** per monitorare e HALT

---

### Terminale 1 — Bridge (sempre acceso)

```powershell
cd C:\Users\ACER\Desktop\main-bs-web\server-local
.\start.bat
```

**Non chiudere questa finestra.** Deve restare in esecuzione.

All'avvio vedi qualcosa tipo:

```
Uvicorn running on http://0.0.0.0:8787
Dashboard available at http://127.0.0.1:8787/dashboard
```

| Cosa | URL |
|------|-----|
| Health check | http://localhost:8787/health |
| Dashboard locale | http://127.0.0.1:8787/dashboard |
| API execute | http://localhost:8787/api/v1/execute-agent |

---

### Terminale 2 — ngrok (solo per n8n Cloud)

Apri un **secondo** PowerShell:

```powershell
cd C:\Users\ACER\Desktop\main-bs-web\server-local
.\start-ngrok.bat
```

Copia l'URL HTTPS che compare, esempio:

```
Forwarding   https://abc123.ngrok-free.app -> http://localhost:8787
```

**Non chiudere neanche questa finestra** finché n8n deve raggiungerti da Internet.

> Se n8n gira sulla stessa rete Wi‑Fi del PC, ngrok non serve: usa `http://192.168.x.x:8787`.

---

## Test locale (Terminale 3, opzionale)

Sostituisci `LA_TUA_CHIAVE` con il valore di `BRIDGE_API_KEY` dal file `.env`.

### Health

```powershell
curl.exe http://localhost:8787/health
```

Atteso: `{"status":"ok"}`

### Invio task (202 Accepted)

```powershell
curl.exe -X POST http://localhost:8787/api/v1/execute-agent `
  -H "Content-Type: application/json" `
  -H "X-API-Key: LA_TUA_CHIAVE" `
  -d '{"dedicated_prompt":"test","task_id":"t1","project_id":1}'
```

Atteso: `{"status":"accepted","task_id":"t1"}`

**PowerShell:** usa apici singoli `'...'` nel body JSON. Non usare `\"` con curl.exe.

Alternativa consigliata:

```powershell
$key = "LA_TUA_CHIAVE_DA_ENV"
$body = @{ dedicated_prompt="test"; task_id="t1"; project_id=1 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8787/api/v1/execute-agent" -Method POST `
  -Headers @{ "X-API-Key"=$key; "Content-Type"="application/json" } -Body $body
```

---

## Configurazione n8n

### Nodo 1 — Invio task al bridge

| Campo | Valore |
|-------|--------|
| Method | `POST` |
| URL (cloud) | `https://TUO-ID.ngrok-free.app/api/v1/execute-agent` |
| URL (LAN) | `http://192.168.x.x:8787/api/v1/execute-agent` |
| Header `Content-Type` | `application/json` |
| Header `X-API-Key` | valore di `BRIDGE_API_KEY` da `.env` |

Body JSON:

```json
{
  "dedicated_prompt": "Il task per Cursor",
  "task_id": "task-42",
  "project_id": 101,
  "project_area": "bs-webdev",
  "context": { "source": "n8n" }
}
```

Risposta attesa: **202** con `status: accepted`. n8n non deve aspettare la fine dell'agent.

### Nodo 2 — Webhook callback (risultato)

L'URL del webhook n8n deve coincidere con `N8N_CALLBACK_URL` in `.env`.

Il bridge invia in POST (più volte durante il run, poi una finale):

```json
{
  "task_id": "task-42",
  "project_id": 101,
  "status": "progress",
  "phase": "heartbeat",
  "elapsed_sec": 240,
  "silence_sec": 130,
  "summary": "Task still running (240s elapsed)..."
}
```

```json
{
  "task_id": "task-42",
  "project_id": 101,
  "status": "success",
  "summary": "output agent troncato..."
}
```

In n8n: su `status === progress` aggiorna CRM / notifica e **non** chiudere il workflow; su `success` o `error` concludi.

---

## Ordine di spegnimento

1. `Ctrl+C` nel Terminale 2 (ngrok)
2. `Ctrl+C` nel Terminale 1 (bridge)

---

## Problemi frequenti

| Errore | Causa | Soluzione |
|--------|-------|-----------|
| `422 validation_error` | JSON malformato in curl | Usa apici singoli o `Invoke-RestMethod` |
| `401` | API key sbagliata | Copia `BRIDGE_API_KEY` da `.env` (una sola riga, no duplicati) |
| Connessione rifiutata | Bridge spento | Terminale 1: `.\start.bat` |
| ngrok non trovato | PATH | Usa `.\start-ngrok.bat` |
| ngrok version too old | Agent obsoleto | Comando `ngrok update` (vedi sopra) |
| `N8N_CALLBACK_URL` mancante | `.env` incompleto | Aggiungi URL webhook n8n |
| Terminale bloccato con domande | L'agent usava PowerShell interattivo | Riavvia bridge (fix: stdin chiuso, niente prompt) |

---

## Riepilogo visivo

```
┌─────────────────────┐     ┌─────────────────────┐
│  TERMINALE 1        │     │  TERMINALE 2        │
│  .\start.bat        │     │  .\start-ngrok.bat  │
│  Bridge :8787       │◄────│  Tunnel HTTPS       │
└──────────┬──────────┘     └──────────▲──────────┘
           │                             │
           │                             │ n8n Cloud
           ▼                             │
    Cursor agent (CLI)              Webhook POST
           │                             │
           └──── callback ──────────────┘
                 N8N_CALLBACK_URL
```

---

## Comandi utili

```powershell
# Reinstallare dipendenze dopo git pull
.\venv\Scripts\pip.exe install -r requirements.txt

# Log attività
Get-Content .\bridge_activity.log -Tail 50

# Dashboard nel browser
start http://127.0.0.1:8787/dashboard
```
