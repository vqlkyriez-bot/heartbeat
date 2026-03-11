#!/usr/bin/env python3
"""
HAL Heartbeat - Autonomous check-in loop.
Routes directly to the agent's default conversation/history, enriches prompts
with Oura data, and optionally sends Telegram messages when HAL emits
SEND_TELEGRAM directives.

Required env vars:
    LETTA_API_KEY
    HAL_AGENT_ID

Optional env vars:
    HEARTBEAT_INTERVAL_MINUTES   default: 30
    OURA_CLIENT_ID
    OURA_CLIENT_SECRET
    OURA_REFRESH_TOKEN
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    DATA_DIR                     default: /data
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import schedule

# ── Configuration ─────────────────────────────────────────────────────────────

LETTA_API_URL = os.getenv("LETTA_API_URL", "https://api.letta.com").rstrip("/")
LETTA_API_KEY = os.environ["LETTA_API_KEY"]
HAL_AGENT_ID = os.environ["HAL_AGENT_ID"]
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "30"))

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TOKENS_FILE = DATA_DIR / "oura_tokens.json"
CACHE_FILE = DATA_DIR / "oura_cache.json"
CACHE_TTL_HOURS = float(os.getenv("OURA_CACHE_TTL_HOURS", "2"))

OURA_CLIENT_ID = os.getenv("OURA_CLIENT_ID", "")
OURA_CLIENT_SECRET = os.getenv("OURA_CLIENT_SECRET", "")
OURA_REFRESH_TOKEN_ENV = os.getenv("OURA_REFRESH_TOKEN", "")
OURA_ENABLED = bool(
    OURA_CLIENT_ID and OURA_CLIENT_SECRET and
    (OURA_REFRESH_TOKEN_ENV or TOKENS_FILE.exists())
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

LOCAL_TZ = ZoneInfo("America/New_York")


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def safe_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# ── Oura: Token Management ────────────────────────────────────────────────────

def load_tokens() -> dict | None:
    if not TOKENS_FILE.exists():
        return None
    try:
        return json.loads(TOKENS_FILE.read_text())
    except Exception as e:
        print(f"[Oura] Failed to read token file: {e}")
        return None


def load_access_token() -> str | None:
    tokens = load_tokens()
    if not tokens:
        return None
    try:
        if time.time() < float(tokens.get("expires_at", 0)) - 60:
            return tokens.get("access_token")
    except Exception:
        pass
    return None


def load_refresh_token() -> str | None:
    tokens = load_tokens()
    if tokens and tokens.get("refresh_token"):
        return tokens["refresh_token"]
    return OURA_REFRESH_TOKEN_ENV or None


def refresh_access_token() -> str | None:
    current_refresh_token = load_refresh_token()
    if not current_refresh_token:
        print("[Oura] No refresh token available")
        return None

    try:
        print(f"[Oura] Refreshing token ({current_refresh_token[:8]}...)")
        resp = requests.post(
            "https://api.ouraring.com/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": current_refresh_token,
                "client_id": OURA_CLIENT_ID,
                "client_secret": OURA_CLIENT_SECRET,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[Oura] Token refresh failed: {resp.status_code} {resp.text}")
            return None

        payload = resp.json()
        access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        refresh_token = payload.get("refresh_token", current_refresh_token)

        safe_write_json(
            TOKENS_FILE,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": time.time() + expires_in,
            },
        )
        print("[Oura] Access token refreshed OK")
        return access_token

    except Exception as e:
        print(f"[Oura] Token refresh error: {e}")
        return None


def get_access_token() -> str | None:
    token = load_access_token()
    if token:
        return token
    return refresh_access_token()


# ── Oura: Data Fetching ───────────────────────────────────────────────────────

def oura_get(url: str, token: str, params: dict) -> requests.Response | None:
    try:
        return requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=20,
        )
    except Exception as e:
        print(f"[Oura] Request error for {url}: {e}")
        return None


def fetch_daily_metric(token: str, endpoint: str, label: str) -> tuple[dict | None, str | None]:
    """
    Try today first, then yesterday, because daily aggregates often lag.
    Returns (record, error_code)
    """
    today = now_local().date()
    for day in (today, today - timedelta(days=1)):
        iso_day = day.isoformat()
        print(f"[Oura] Fetching {label} for {iso_day}")
        resp = oura_get(
            endpoint,
            token,
            {"start_date": iso_day, "end_date": iso_day},
        )
        if resp is None:
            continue
        if resp.status_code == 401:
            return None, "auth_expired"
        if resp.status_code != 200:
            print(f"[Oura] {label} fetch returned {resp.status_code}: {resp.text[:200]}")
            continue

        data = resp.json().get("data", [])
        print(f"[Oura] {label} returned {len(data)} item(s) for {iso_day}")
        if data:
            return data[-1], None

    return None, None


def fetch_heartrate(token: str) -> tuple[dict | None, str | None]:
    try:
        now_utc = datetime.now(timezone.utc)
        start_utc = now_utc - timedelta(hours=2)

        resp = oura_get(
            "https://api.ouraring.com/v2/usercollection/heartrate",
            token,
            {
                "start_datetime": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_datetime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        if resp is None:
            return None, None
        if resp.status_code == 401:
            return None, "auth_expired"
        if resp.status_code != 200:
            print(f"[Oura] heartrate fetch returned {resp.status_code}: {resp.text[:200]}")
            return None, None

        items = resp.json().get("data", [])
        print(f"[Oura] heartrate returned {len(items)} item(s)")
        if not items:
            return None, None

        items_sorted = sorted(items, key=lambda x: x.get("timestamp", ""), reverse=True)
        latest = items_sorted[0]
        print(
            f"[Oura] Latest HR: {latest.get('bpm')} bpm at {latest.get('timestamp')}"
        )
        return latest, None

    except Exception as e:
        print(f"[Oura] heartrate fetch error: {e}")
        return None, None


def fetch_oura_snapshot(token: str) -> tuple[dict | None, str | None]:
    readiness, error = fetch_daily_metric(
        token,
        "https://api.ouraring.com/v2/usercollection/daily_readiness",
        "readiness",
    )
    if error == "auth_expired":
        return None, error

    sleep, error = fetch_daily_metric(
        token,
        "https://api.ouraring.com/v2/usercollection/daily_sleep",
        "sleep",
    )
    if error == "auth_expired":
        return None, error

    heartrate, error = fetch_heartrate(token)
    if error == "auth_expired":
        return None, error

    result = {}
    if readiness:
        result["readiness"] = readiness
    if sleep:
        result["sleep"] = sleep
    if heartrate:
        result["heartrate"] = heartrate

    return (result if result else None), None


def format_biometrics(data: dict | None) -> str | None:
    if not data:
        return None

    lines = []

    heartrate = data.get("heartrate") or {}
    readiness = data.get("readiness") or {}
    sleep = data.get("sleep") or {}

    # Current BPM
    bpm = heartrate.get("bpm")
    hr_source = heartrate.get("source")
    hr_timestamp = heartrate.get("timestamp")
    try:
        if bpm is not None:
            bpm_num = float(bpm)
            if 30 <= bpm_num <= 220:
                label = f"Current BPM: {int(round(bpm_num))} bpm"
                if hr_source:
                    label += f" (source: {hr_source})"
                if hr_timestamp:
                    label += f" at {hr_timestamp}"
                lines.append(label)
    except (ValueError, TypeError):
        print(f"[Oura] Invalid BPM value: {bpm}")

    # Readiness
    if readiness:
        score = readiness.get("score")
        if score is not None:
            lines.append(f"Readiness score: {score}/100")

        contributors = readiness.get("contributors", {})
        for key, label in [
            ("hrv_balance", "HRV balance score"),
            ("recovery_index", "Recovery index score"),
            ("resting_heart_rate", "Resting HR score"),
        ]:
            value = contributors.get(key)
            if value is not None:
                lines.append(f"  {label}: {value}/100")

    # Sleep
    if sleep:
        sleep_score = sleep.get("score")
        if sleep_score is not None:
            lines.append(f"Sleep score: {sleep_score}/100")

        contributors = sleep.get("contributors", {})
        for key, label in [
            ("total_sleep", "Total sleep score"),
            ("efficiency", "Efficiency score"),
            ("rem_sleep", "REM score"),
            ("deep_sleep", "Deep sleep score"),
        ]:
            value = contributors.get(key)
            if value is not None:
                lines.append(f"  {label}: {value}/100")

    return "\n".join(lines) if lines else None


def get_biometrics() -> str | None:
    if not OURA_ENABLED:
        return None

    # Cache
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
            fetched_at = float(cache.get("fetched_at", 0))
            age_hours = (time.time() - fetched_at) / 3600
            if age_hours < CACHE_TTL_HOURS:
                print(f"[Oura] Using cached data ({age_hours:.1f}h old)")
                return format_biometrics(cache.get("data"))
        except Exception as e:
            print(f"[Oura] Cache read error: {e}")

    token = get_access_token()
    if not token:
        print("[Oura] No valid access token available")
        return None

    data, error = fetch_oura_snapshot(token)
    if error == "auth_expired":
        print("[Oura] Token expired mid-request, refreshing...")
        token = refresh_access_token()
        if token:
            data, error = fetch_oura_snapshot(token)

    if not data:
        print("[Oura] No data returned from API")
        return None

    try:
        safe_write_json(
            CACHE_FILE,
            {
                "fetched_at": time.time(),
                "data": data,
            },
        )
    except Exception as e:
        print(f"[Oura] Cache write error: {e}")

    return format_biometrics(data)


# ── Letta ─────────────────────────────────────────────────────────────────────

def send_to_hal(prompt: str) -> str | None:
    """
    Send to the agent endpoint so the message routes through the agent's default
    message history rather than an explicit conversation ID.
    """
    try:
        url = f"{LETTA_API_URL}/v1/agents/{HAL_AGENT_ID}/messages"
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {LETTA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "messages": [{"role": "user", "content": prompt}],
                "streaming": False,
                "include_return_message_types": ["assistant_message"],
            },
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()

        for msg in payload.get("messages", []):
            if msg.get("message_type") == "assistant_message":
                content = msg.get("content")
                if isinstance(content, str):
                    return content

        print("[Letta] No assistant_message found in response")
        return None

    except Exception as e:
        print(f"[Letta] Error: {e}")
        return None


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_ENABLED:
        print("[Telegram] Not enabled")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print("[Telegram] Message sent OK")
            return True

        print(f"[Telegram] Send failed: {resp.status_code} {resp.text}")
        return False

    except Exception as e:
        print(f"[Telegram] Send error: {e}")
        return False


def parse_telegram_directives(response_text: str) -> list[str]:
    """
    Accepts:
      SEND_TELEGRAM hello
      **SEND_TELEGRAM** hello
      SEND_TELEGRAM: hello
    Collects continuation lines until a blank line or another directive.
    """
    directives: list[str] = []
    lines = response_text.splitlines()

    directive_re = re.compile(r"^\s*\**SEND_TELEGRAM\**\:?\s*(.*)$")

    i = 0
    while i < len(lines):
        match = directive_re.match(lines[i])
        if not match:
            i += 1
            continue

        current = []
        first_line_text = match.group(1).strip()
        if first_line_text:
            current.append(first_line_text)

        i += 1
        while i < len(lines):
            if not lines[i].strip():
                break
            if directive_re.match(lines[i]):
                i -= 1
                break
            current.append(lines[i].rstrip())
            i += 1

        message = "\n".join(current).strip()
        if message:
            preview = (message[:60] + "...") if len(message) > 60 else message
            print(f"[Telegram] Found directive: {preview}")
            directives.append(message)

        i += 1

    return directives


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def build_prompt(timestamp: str, biometrics: str | None) -> str:
    lines = [
        f"[HEARTBEAT - {timestamp}]",
        "",
    ]

    if biometrics:
        lines.extend([
            "LILLITH'S BIOMETRICS:",
            biometrics,
            "",
        ])
    else:
        lines.extend([
            "[Oura data unavailable this turn]",
            "",
        ])

    lines.extend([
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
    ])

    return "\n".join(lines)


def heartbeat() -> None:
    timestamp = now_local().strftime("%Y-%m-%d %H:%M %Z")
    print("")
    print(f"[{timestamp}] Heartbeat starting...")

    biometrics = get_biometrics()
    prompt = build_prompt(timestamp, biometrics)

    print("[Letta] Sending to HAL...")
    response = send_to_hal(prompt)

    if response:
        print("[HAL]")
        print(response)

        telegram_msgs = parse_telegram_directives(response)
        if telegram_msgs:
            print(f"[Telegram] Found {len(telegram_msgs)} directive(s)")
            for msg in telegram_msgs:
                preview = msg[:50] + ("..." if len(msg) > 50 else "")
                print(f"[Telegram] Sending: {preview}")
                send_telegram_message(msg)
    else:
        print("[Letta] No response received")

    print(f"[{timestamp}] Heartbeat complete.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 50)
    print("HAL HEARTBEAT")
    print("=" * 50)
    print(f"Agent:        {HAL_AGENT_ID}")
    print("Mode:         default agent message history")
    print(f"Interval:     {HEARTBEAT_INTERVAL} minutes")
    print(f"Oura:         {'enabled' if OURA_ENABLED else 'disabled'}")
    print(f"Telegram:     {'enabled' if TELEGRAM_ENABLED else 'disabled'}")
    print(f"Data dir:     {DATA_DIR}")
    print("=" * 50)
    print("")

    heartbeat()
    schedule.every(HEARTBEAT_INTERVAL).minutes.do(heartbeat)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
