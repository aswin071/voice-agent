# SpeedCare Voice Agent

SpeedCare Voice Agent is a multilingual vehicle-service booking system built around:

- A FastAPI backend in `main.py`
- A LiveKit voice worker in `simple_agent.py`
- A conversational state machine in `agent_core/`
- Sarvam STT/TTS plugins in `plugins/`
- PostgreSQL for persistence and Redis for session/cache support

## Project Layout

- `main.py`: FastAPI app, health checks, metrics, and API router registration
- `simple_agent.py`: main LiveKit worker for local/dev voice runs
- `agent_worker.py`: alternate worker implementation still under iteration
- `agent.py`: legacy outbound-caller sample from the original starter
- `init_db.py`: creates database tables from SQLAlchemy models
- `test_webrtc_debug.html`: easiest browser page for local WebRTC testing

## Fresh Environment Setup

### Requirements

- Python 3.10+
- PostgreSQL
- Redis
- LiveKit server or LiveKit Cloud project
- Sarvam API key
- Anthropic API key

### Install

```bash
cd /home/wac/vehicle-service-voice-agent/vehicle-service-voice-agent
python3.10 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env.local
```

### Configure `.env.local`

Fill in at least these values:

```env
LIVEKIT_URL=wss://your-livekit-host
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret
SARVAM_API_KEY=your_sarvam_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
DATABASE_URL=postgresql+asyncpg://speedcare:speedcare@localhost:5432/speedcare
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=replace-with-a-long-random-secret
SIP_OUTBOUND_TRUNK_ID=
```

If you keep the default `DATABASE_URL`, make sure that PostgreSQL database and credentials exist before starting the app.

### Initialize the Database

Start PostgreSQL and Redis, then run:

```bash
python init_db.py
```

## Run the Project

Use three terminals after activating the virtual environment.

### Terminal 1: FastAPI backend

```bash
cd /home/wac/vehicle-service-voice-agent/vehicle-service-voice-agent
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Terminal 2: LiveKit voice agent

```bash
cd /home/wac/vehicle-service-voice-agent/vehicle-service-voice-agent
source .venv/bin/activate
python simple_agent.py dev
```

### Terminal 3: static test page

```bash
cd /home/wac/vehicle-service-voice-agent/vehicle-service-voice-agent
python3 -m http.server 3000
```

Then open:

- `http://localhost:8000/docs` for API docs
- `http://localhost:3000/test_webrtc_debug.html` for browser voice testing

## LiveKit Test Flow

Once the API and worker are running, dispatch the agent:

```bash
lk dispatch create --new-room --agent-name speedcare-agent
```

Paste the returned room name into `test_webrtc_debug.html`, allow microphone access, and connect.

## Notes

- `simple_agent.py` is the best current entrypoint for development.
- `agent.py` is still the older starter example and should not be used as the main SpeedCare runtime.
- The browser test pages currently contain a hardcoded `LIVEKIT_URL`. If your LiveKit URL is different, update the `LIVEKIT_URL` constant in the HTML test file you use.
