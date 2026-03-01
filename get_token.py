#!/usr/bin/env python3
"""
One-time Oura OAuth2 token setup.
Run this locally ONCE to get your refresh token, then add it to Railway env vars.

Usage:
    python get_token.py
"""

import requests
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

CLIENT_ID = "ed4e26b8-c352-4657-88a9-e1dc3a412c5d"
CLIENT_SECRET = "TsLbI7UHlR3MfMk8L8Bh5vequ01-o445iQ7HWblzxyg"
REDIRECT_URI = "http://localhost:8000/callback"
SCOPES = "daily heartrate spo2 daily_stress"

auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Got it! You can close this window and go back to the terminal.")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received. Something went wrong.")

    def log_message(self, format, *args):
        pass  # Suppress server logs


def main():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
    }
    auth_url = "https://cloud.ouraring.com/oauth/authorize?" + urlencode(params)

    print("Opening Oura authorization page in your browser...")
    print("If it doesn't open automatically, visit:")
    print(auth_url)
    print("")
    webbrowser.open(auth_url)

    print("Waiting for callback on http://localhost:8000/callback ...")
    server = HTTPServer(("localhost", 8000), CallbackHandler)
    server.handle_request()

    if not auth_code:
        print("ERROR: No authorization code received.")
        return

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
