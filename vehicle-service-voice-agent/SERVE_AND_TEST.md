# Fix Microphone Permission Issue

## The Problem
Browsers block microphone access for pages loaded via `file://` protocol.

## Solution: Serve via HTTP

### Step 1: Start HTTP Server (in project folder)
```bash
cd /home/wac/vehicle-service-voice-agent/vehicle-service-voice-agent
python3 -m http.server 3000
```

### Step 2: Open in Browser
Go to: **http://localhost:3000/test_webrtc_debug.html**

(NOT the file:// path!)

### Step 3: Allow Microphone
When the browser asks, click **"Allow"** for microphone access.

---

## Alternative: Use ngrok for HTTPS
If localhost doesn't work:

```bash
# Install ngrok
# Then run:
ngrok http 3000
```

Use the HTTPS URL provided by ngrok.

---

## What's Running Where

| Service | Command | URL |
|---------|---------|-----|
| FastAPI | `uvicorn main:app --reload` | http://localhost:8000 |
| Agent Worker | `python simple_agent.py dev` | (no URL, connects to LiveKit) |
| Web Server | `python3 -m http.server 3000` | http://localhost:3000 |
| LiveKit | Cloud service | wss://ruka-voice-agent-rim8gdnh.livekit.cloud |

---

## Testing Steps

1. **Start all services:**
   ```bash
   # Terminal 1
   uvicorn main:app --reload --port 8000

   # Terminal 2
   python simple_agent.py dev

   # Terminal 3
   python3 -m http.server 3000
   ```

2. **Dispatch agent:**
   ```bash
   lk dispatch create --new-room --agent-name speedcare-test-agent
   ```

3. **Open browser:**
   http://localhost:3000/test_webrtc_debug.html

4. **Paste room name** from dispatch output

5. **Allow microphone** when browser asks

6. **Click Connect** and speak!
