"""Check your GitHub OAuth App settings. Run: python scripts/check_oauth.py"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
cid, secret = os.getenv("GITHUB_CLIENT_ID", ""), os.getenv("GITHUB_CLIENT_SECRET", "")
base = os.getenv("UI_BASE_URL", "http://localhost:8000").rstrip("/")

if not (cid and secret):
    sys.exit("""GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET are not set in .env.

Create the OAuth App (about 2 minutes):
  1. github.com -> Settings -> Developer settings -> OAuth Apps -> New OAuth App
  2. Application name: SpecGuard
     Homepage URL:     %s
     Authorization callback URL: %s/auth/callback
  3. Register application, then 'Generate a new client secret'
  4. Put GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET in .env, then run this script again.""" % (base, base))

ok = True
r = requests.get("https://github.com/login/oauth/authorize", params={"client_id": cid, "redirect_uri": f"{base}/auth/callback"},
                 allow_redirects=False, timeout=20)
print("client id  :", "OK" if r.status_code in (200, 302) else f"REJECTED (HTTP {r.status_code}) - check GITHUB_CLIENT_ID")
ok &= r.status_code in (200, 302)

t = requests.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                  data={"client_id": cid, "client_secret": secret, "code": "specguard-check"}, timeout=20).json()
good = t.get("error") == "bad_verification_code"  # valid credentials, bogus code
print("secret     :", "OK" if good else f"REJECTED ({t.get('error')}) - regenerate the client secret")
ok &= good
print("callback   :", f"{base}/auth/callback  (must match the app's 'Authorization callback URL' exactly)")
sys.exit(0 if ok else 1)
