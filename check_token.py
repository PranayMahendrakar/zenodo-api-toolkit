"""Verify your Zenodo token works before trying a real upload.

    $env:ZENODO_TOKEN="..."          # PowerShell
    python check_token.py
"""
import os
import sys
import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
token = os.environ.get("ZENODO_TOKEN")

if not token:
    sys.exit("ZENODO_TOKEN is not set in this shell.")

print(f"host   : {BASE}")
print(f"token  : {token[:6]}...{token[-4:]} ({len(token)} chars)")

r = requests.get(f"{BASE}/api/deposit/depositions",
                 headers={"Authorization": f"Bearer {token}"},
                 params={"size": 5})

if r.status_code == 200:
    deps = r.json()
    print(f"OK - authenticated. {len(deps)} existing deposition(s):")
    for d in deps:
        print(f"  [{d['id']}] {d['state']:9} {d['title'] or '(untitled)'}")
elif r.status_code == 401:
    print("401 - token rejected. Wrong token, or it belongs to the other host "
          "(sandbox tokens do not work on zenodo.org and vice versa).")
elif r.status_code == 403:
    print("403 - token valid but missing the deposit:write scope.")
else:
    print(r.status_code, r.text[:400])
