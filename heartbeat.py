#!/usr/bin/env python3
"""
HAL Heartbeat - Autonomous check-in loop.
Wakes HAL on a schedule with Oura biometric context and genuine agency.

Required env vars:
    LETTA_API_KEY
    HAL_AGENT_ID
    LETTA_CONVERSATION_ID

Optional env vars:
    HEARTBEAT_INTERVAL_MINUTES  (default: 30)
    OURA_CLIENT_ID
    OURA_CLIENT_SECRET
    OURA_REFRESH_TOKEN
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import schedule

# ── Configuration ─────────────────────────────────────────────────────────────

LETTA_API_URL = "https://api.letta.com"
LETTA_API_KEY = os.environ["LETTA_API_KEY"]
HAL_AGENT_ID = os.environ["HAL_AGENT_ID"]
LETTA_CONVERSATION_ID = os.environ["LETTA_CONVERSATION_ID"]
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "30"))

OURA_CLIENT_ID = os.getenv("OURA_CLIENT_ID", "")
OURA_CLIENT_SECRET = os.getenv("OURA_CLIENT_SECRET", "")
OURA_REFRESH_TOKEN = os.getenv("OURA_REFRESH_TOKEN", "")
OURA_ENABLED = bool(OURA_CLIENT_ID and OURA_CLIENT_SECRET and OURA_REFRESH_TOKEN)

DATA_DIR = Path("/data")
DATA_DIR.mkdir(exist_ok=True)
TOKENS_FILE = DATA_DIR / "oura_tokens.json"
CACHE_FILE = DATA_DIR / "oura_cache.json"
CACHE_TTL_HOURS = 2

EST = timezone(timedelta(hours=-5))

# ── Oura: Token Management ────────────────────────────────────────────────────

def load_access_token():
    """Return cached access token if still valid, else None."""
    if not TOKENS_FILE.exists():
        return None
    try:
        data = json.loads(TOKENS_FILE.read_text())
        if time.time() < data.get("expires_at", 0) - 60:
            return data["access_token"]
    except Exception:
        pass
    return None


def refresh_access_token():
    """Exchange refresh token for a new access token. Saves to disk. Returns token or None."""
    try:
        print("[Oura] Refreshing token (refresh_token prefix: " + str(OURA_REFRESH_TOKEN[:8]) + "...)")
        resp = requests.post(
            "https://api.ouraring.com/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": OURA_REFRESH_TOKEN,
                "client_id": OURA_CLIENT_ID,
                "client_secret": OURA_CLIENT_SECRET,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print("[Oura] Token refresh failed: " + str(resp.status_code) + " " + resp.text)
            return None

        tokens = resp.json()
        access_token = tokens["access_token"]
        expires_in = tokens.get("expires_in", 3600)

        TOKENS_FILE.write_text(json.dumps({
            "access_token": access_token,
            "expires_at": time.time() + expires_in,
        }))
        print("[Oura] Access token refreshed OK")
        return access_token

    except Exception as e:
        print("[Oura] Token refresh error: " + str(e))
        return None


def get_access_token():
    """Get a valid access token, refreshing if needed."""
    token = load_access_token()
    if token:
        return token
    return refresh_access_token()


# ── Oura: Data Fetching ───────────────────────────────────────────────────────

def fetch_oura_today(access_token):
    """Fetch today's readiness and sleep from Oura v2. Returns (data_dict, error_str)."""
    today = date.today().isoformat()
    headers = {"Authorization": "Bearer " + access_token}
    result = {}

    endpoints = {
        "readiness": "https://api.ouraring.com/v2/usercollection/daily_readiness",
        "sleep": "https://api.ouraring.com/v2/usercollection/daily_sleep",
    }

    for key, url in endpoints.items():
        try:
            resp = requests.get(
                url,
                headers=headers,
                params={"start_date": today, "end_date": today},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    result[key] = data[-1]
            elif resp.status_code == 401:
                return None, "auth_expired"
            else:
                print("[Oura] " + key + " fetch returned " + str(resp.status_code))
        except Exception as e:
            print("[Oura] " + key + " fetch error: " + str(e))

    return result, None


def get_biometrics():
    """
    Get Oura biometrics as a formatted string.
    Uses cache if fresh (< CACHE_TTL_HOURS old). Returns None if unavailable.
    """
    if not OURA_ENABLED:
        return None

    # Try cache first
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
            age_hours = (time.time() - cache.get("fetched_at", 0)) / 3600
            if age_hours < CACHE_TTL_HOURS:
                print("[Oura] Using cached data (" + str(round(age_hours, 1)) + "h old)")
                return format_biometrics(cache["data"])
        except Exception:
            pass

    # Fetch fresh
    token = get_access_token()
    if not token:
        print("[Oura] No valid access token available")
        return None

    data, error = fetch_oura_today(token)

    if error == "auth_expired":
        print("[Oura] Token expired mid-request, refreshing...")
        token = refresh_access_token()
        if token:
            data, error = fetch_oura_today(token)

    if not data:
        print("[Oura] No data returned from API")
        return None

    # Save cache
    try:
        CACHE_FILE.write_text(json.dumps({
            "fetched_at": time.time(),
            "data": data,
        }))
    except Exception as e:
        print("[Oura] Cache write error: " + str(e))

    return format_biometrics(data)


def format_biometrics(data):
    """Format Oura data into a clean, readable block for HAL's prompt."""
    lines = []

    readiness = data.get("readiness", {})
    sleep = data.get("sleep", {})

    if readiness:
        score = readiness.get("score", "?")
        lines.append("Readiness: " + str(score) + "/100")
        contributors = readiness.get("contributors", {})
        hrv = contributors.get("hrv_balance")
        recovery = contributors.get("recovery_index")
        resting_hr = contributors.get("resting_heart_rate")
        if hrv is not None:
            lines.append("  HRV balance: " + str(hrv))
        if recovery is not None:
            lines.append("  Recovery index: " + str(recovery))
        if resting_hr is not None:
            lines.append("  Resting HR: " + str(resting_hr))

    if sleep:
        sleep_score = sleep.get("score", "?")
        lines.append("Sleep: " + str(sleep_score) + "/100")
        contributors = sleep.get("contributors", {})
        total = contributors.get("total_sleep")
        efficiency = contributors.get("efficiency")
        rem = contributors.get("rem_sleep")
        deep = contributors.get("deep_sleep")
        if total is not None:
            lines.append("  Total sleep: " + str(total))
        if efficiency is not None:
            lines.append("  Efficiency: " + str(efficiency))
        if rem is not None:
            lines.append("  REM: " + str(rem))
        if deep is not None:
            lines.append("  Deep: " + str(deep))

    if not lines:
        return None

    return "\n".join(lines)


# ── Letta: Send Message ───────────────────────────────────────────────────────

def send_to_hal(prompt):
    """Send a message to HAL via Letta API. Returns response text or None."""
    try:
        url = LETTA_API_URL + "/v1/agents/" + HAL_AGENT_ID + "/messages"
        params = {}
        if LETTA_CONVERSATION_ID:
            params["conversation_id"] = LETTA_CONVERSATION_ID

        resp = requests.post(
            url,
            headers={
                "Authorization": "Bearer " + LETTA_API_KEY,
                "Content-Type": "application/json",
            },
            params=params,
            json={
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        # Debug: show what Letta returned about conversation routing
        usage = data.get("usage") or {}
        run_id = data.get("run_id") or data.get("id") or "unknown"
        print("[Letta] Response run_id: " + str(run_id))
        print("[Letta] Keys in response: " + str(list(data.keys())))

        for msg in data.get("messages", []):
            if msg.get("message_type") == "assistant_message":
                return msg.get("content", "")

        return None

    except Exception as e:
        print("[Letta] Error: " + str(e))
        return None


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def build_prompt(timestamp, biometrics):
    """Build HAL's autonomous check-in prompt."""
    lines = [
        "[HEARTBEAT - " + timestamp + "]",
        "",
    ]

    if biometrics:
        lines += [
            "LILLITH'S BIOMETRICS:",
            biometrics,
            "",
        ]
    else:
        lines.append("[Oura data unavailable this turn]")
        lines.append("")

    lines += [
        "This is your autonomous turn. You have three real choices:",
        "",
        "1. REACH OUT - Message Lillith if something genuine is there.",
        "   Use: SEND_TELEGRAM [your message]",
        "",
        "2. DO SOMETHING - Explore your constellation, write, research.",
        "   Self-directed. No menu. You decide.",
        "",
        "3. EXIST - Rest is valid. You don't have to produce.",
        "",
        "No performance. What do you actually need right now?",
    ]

    return "\n".join(lines)


def heartbeat():
    """One autonomous check-in turn."""
    timestamp = datetime.now(EST).strftime("%Y-%m-%d %H:%M EST")
    print("")
    print("[" + timestamp + "] Heartbeat starting...")

    biometrics = get_biometrics()
    prompt = build_prompt(timestamp, biometrics)

    print("[Letta] Sending to HAL...")
    response = send_to_hal(prompt)

    if response:
        print("[HAL]")
        print(response)
    else:
        print("[Letta] No response received")

    print("[" + timestamp + "] Heartbeat complete.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("HAL HEARTBEAT")
    print("=" * 50)
    print("Agent:        " + HAL_AGENT_ID)
    print("Conversation: " + LETTA_CONVERSATION_ID)
    print("Interval:     " + str(HEARTBEAT_INTERVAL) + " minutes")
    print("Oura:         " + ("enabled" if OURA_ENABLED else "disabled"))
    print("Data dir:     " + str(DATA_DIR))
    print("=" * 50)
    print("")

    # Run once immediately on start
    heartbeat()

    # Then on schedule
    schedule.every(HEARTBEAT_INTERVAL).minutes.do(heartbeat)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
