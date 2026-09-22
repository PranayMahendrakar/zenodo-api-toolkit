"""Read-only tour of the Zenodo API. The public parts need no token."""
import os
import json
import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
TOKEN = os.environ.get("ZENODO_TOKEN")
auth = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def section(t):
    print("\n" + "=" * 62 + f"\n  {t}\n" + "=" * 62)


section("1. PUBLIC SEARCH  GET /api/records  (no token needed)")
r = requests.get(f"{BASE}/api/records",
                 params={"q": "digital twin manufacturing",
                         "size": 3, "sort": "mostrecent"})
d = r.json()
print(f"total matching records: {d['hits']['total']:,}")
for h in d["hits"]["hits"]:
    print(f"  {h['doi']:28} {h['metadata']['title'][:60]}")
    print(f"    {h.get('stats', {}).get('downloads', 0)} downloads, "
          f"{h.get('stats', {}).get('views', 0)} views")

section("2. LICENSE VOCABULARY  GET /api/vocabularies/licenses")
r = requests.get(f"{BASE}/api/vocabularies/licenses", params={"q": "cc-by", "size": 5})
if r.ok:
    for h in r.json()["hits"]["hits"]:
        print(f"  {h['id']:20} {h.get('title', {}).get('en', '')[:55]}")
else:
    print(f"  {r.status_code} (try /api/licenses/ on the legacy path)")

section("3. ONE RECORD IN FULL  GET /api/records/:id")
r = requests.get(f"{BASE}/api/records", params={"q": "zenodo", "size": 1})
rec_id = r.json()["hits"]["hits"][0]["id"]
r = requests.get(f"{BASE}/api/records/{rec_id}")
rec = r.json()
print(f"  id       : {rec['id']}")
print(f"  doi      : {rec.get('doi')}")
print(f"  files    : {[f.get('key') for f in rec.get('files', [])][:5]}")
print(f"  stats    : {json.dumps(rec.get('stats', {}))[:200]}")
print(f"  links    : {list(rec.get('links', {}).keys())}")

if not TOKEN:
    print("\n(no ZENODO_TOKEN set - skipping the authenticated sections)")
    raise SystemExit

section("4. YOUR DEPOSITIONS  GET /api/deposit/depositions")
r = requests.get(f"{BASE}/api/deposit/depositions",
                 headers=auth, params={"size": 25})
deps = r.json()
drafts = [d for d in deps if d["state"] != "done"]
print(f"  {len(deps)} total, {len(drafts)} unpublished draft(s)")
for d in deps:
    print(f"  [{d['id']}] {d['state']:11} {(d['title'] or '(untitled)')[:55]}")
if drafts:
    print("\n  orphan drafts you may want to delete:")
    for d in drafts:
        print(f"    python delete_draft.py {d['id']}")

section("5. YOUR PUBLISHED RECORD STATS")
for d in deps:
    if d["state"] != "done":
        continue
    rr = requests.get(f"{BASE}/api/records/{d['id']}")
    if not rr.ok:
        continue
    s = rr.json().get("stats", {})
    print(f"  [{d['id']}] views={s.get('views',0):5} downloads={s.get('downloads',0):5}"
          f"  {(d['title'] or '')[:45]}")
