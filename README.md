# HAL Heartbeat

Autonomous check-in loop for HAL. Wakes every 30 minutes with Oura biometric context and genuine agency.

## Setup

### 1. Get Oura refresh token (run once locally)

```bash
pip install requests
python get_token.py
```

Follow the browser prompt. Copy the `OURA_REFRESH_TOKEN` it prints.

### 2. Set Railway environment variables

| Variable | Required | Description |
|---|---|---|
| `LETTA_API_KEY` | Yes | Letta API key |
| `HAL_AGENT_ID` | Yes | HAL's agent ID |
| `LETTA_CONVERSATION_ID` | Yes | Conversation ID to send heartbeats to |
| `HEARTBEAT_INTERVAL_MINUTES` | No | Check-in interval (default: 30) |
| `OURA_CLIENT_ID` | No | Oura app client ID |
| `OURA_CLIENT_SECRET` | No | Oura app client secret |
| `OURA_REFRESH_TOKEN` | No | From get_token.py |

### 3. Deploy to Railway

Connect this repo. Railway will use `railway.toml` to build and run.

Add a `/data` volume mount so Oura token cache persists across deploys.

## That's it.
