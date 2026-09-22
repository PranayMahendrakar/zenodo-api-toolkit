"""Delete an UNPUBLISHED Zenodo draft deposition.

Zenodo refuses to delete already-published records, so this can only ever
remove drafts -- but double-check the id before running it.

    python delete_draft.py 22025399
"""
import os
import sys
import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
TOKEN = os.environ["ZENODO_TOKEN"]
dep_id = sys.argv[1]

h = {"Authorization": f"Bearer {TOKEN}"}
r = requests.get(f"{BASE}/api/deposit/depositions/{dep_id}", headers=h)
r.raise_for_status()
d = r.json()
print(f"[{d['id']}] state={d['state']} title={d['title'] or '(untitled)'} "
      f"files={len(d.get('files', []))}")

if d["state"] == "done":
    sys.exit("this deposition is published - it cannot be deleted.")

r = requests.delete(f"{BASE}/api/deposit/depositions/{dep_id}", headers=h)
print("deleted" if r.status_code == 204 else f"{r.status_code} {r.text[:300]}")
