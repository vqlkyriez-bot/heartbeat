#!/usr/bin/env python3
"""
One-time Oura OAuth2 token setup.
Run this locally ONCE to get your refresh token, then add it to Railway env vars.

Usage:
    python3 get_token.py
"""

import requests
from urllib.parse import urlparse, parse_qs, urlencode

CLIENT_ID = "ed4e26b8-c352-4657-88a9-e1dc3a412c5d"
CLIENT_SECRET = "TsLbI7UHlR3MfMk8L8Bh5vequ01-o445iQ7HWblzxyg"
REDIRECT_URI = "http://localhost:8000/callback"
SCOPES = "daily heartrate spo2 daily_stress"


def main():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
    }
    auth_url = "https://cloud.ouraring.com/oauth/authorize?" + urlencode(params)

    print("")
    print("Step 1: Open this URL in your browser:")
    print("")
    print(auth_url)
    print("")
    print("Step 2: Authorize the app.")
    print("Step 3: You'll be redirected to a page that won't load (localhost).")
    print("        Copy the full URL from your browser's address bar and paste it below.")
    print("")

    pasted = input("Paste the full redirect URL here: ").strip()

    parsed = urlparse(pasted)
    params = parse_qs(parsed.query)

    if "code" not in params:
        print("ERROR: No code found in that URL. Make sure you copied the full address bar URL.")
        return

    auth_code = params["code"][0]
    print("")
    print("Got code. Exchanging for tokens...")

    resp = requests.post(
        "https://api.ouraring.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15,
    )

    if resp.status_code != 200:
        print("ERROR: Token exchange failed.")
        print("Status: " + str(resp.status_code))
        print("Response: " + resp.text)
        return

    tokens = resp.json()

    print("")
    print("=" * 50)
    print("SUCCESS. Add this to Railway environment variables:")
    print("=" * 50)
    print("")
    print("OURA_REFRESH_TOKEN=" + tokens["refresh_token"])
    print("")
    print("Done. You never need to run this again unless the refresh token is revoked.")


if __name__ == "__main__":
    main()
