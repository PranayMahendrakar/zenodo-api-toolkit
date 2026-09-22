"""
Exercise every write endpoint of the Zenodo deposit API WITHOUT publishing.

  create draft -> upload file -> list files -> set metadata -> read back
  -> show draft URL -> delete draft

Nothing becomes public and no DOI is minted. Safe to run on production.
Pass --keep to leave the draft alive so you can eyeball it in the browser.

    python test_roundtrip.py "C:\path\to\file.pdf"
    python test_roundtrip.py "C:\path\to\file.pdf" --keep
"""
import os
import sys
import json
import time
import hashlib
import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
TOKEN = os.environ["ZENODO_TOKEN"]
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}"})

path = sys.argv[1]
KEEP = "--keep" in sys.argv
if not os.path.isfile(path):
    sys.exit(f"file not found: {path}")

name = os.path.basename(path)
size = os.path.getsize(path)
local_md5 = hashlib.md5(open(path, "rb").read()).hexdigest()


def step(n, what):
    print(f"\n[{n}] {what}")


t0 = time.time()
print(f"host {BASE}   file {name}   {size/1e6:.2f} MB   md5 {local_md5}")

# ---------------------------------------------------------------- 1. create
step(1, "POST /api/deposit/depositions")
r = S.post(f"{BASE}/api/deposit/depositions", json={})
r.raise_for_status()
dep = r.json()
dep_id, bucket = dep["id"], dep["links"]["bucket"]
print(f"    id={dep_id}  state={dep['state']}")
print(f"    links exposed: {', '.join(sorted(dep['links']))}")

try:
    # ------------------------------------------------------------ 2. upload
    step(2, f"PUT {{bucket}}/{name}   (streamed)")
    t = time.time()
    with open(path, "rb") as fh:
        r = S.put(f"{bucket}/{name}", data=fh)
    r.raise_for_status()
    f = r.json()
    dt = time.time() - t
    print(f"    {f['size']:,} bytes in {dt:.1f}s  ({size/1e6/dt:.1f} MB/s)")
    print(f"    server checksum {f['checksum']}")
    print(f"    integrity: {'MATCH' if f['checksum'].endswith(local_md5) else 'MISMATCH'}")

    # ------------------------------------------------------- 3. list files
    step(3, f"GET /api/deposit/depositions/{dep_id}/files")
    r = S.get(f"{BASE}/api/deposit/depositions/{dep_id}/files")
    for x in r.json():
        print(f"    {x['filename']}  {x.get('filesize', 0):,} bytes")

    # --------------------------------------------------------- 4. metadata
    step(4, f"PUT /api/deposit/depositions/{dep_id}   (metadata)")
    meta = {"metadata": {
        "upload_type": "publication",
        "publication_type": "report",
        "title": f"[API TEST - DO NOT PUBLISH] {name}",
        "description": "<p>Temporary draft created by test_roundtrip.py to "
                       "verify the Zenodo API. Deleted automatically.</p>",
        "creators": [{"name": "Mahendrakar, Pranay"}],
        "access_right": "open",
        "license": "cc-by-4.0",
        "keywords": ["api-test"],
    }}
    r = S.put(f"{BASE}/api/deposit/depositions/{dep_id}", json=meta)
    r.raise_for_status()
    m = r.json()["metadata"]
    print(f"    title    : {m['title'][:60]}")
    print(f"    prereserved DOI: {m.get('prereserve_doi')}")

    # -------------------------------------------------------- 5. read back
    step(5, f"GET /api/deposit/depositions/{dep_id}")
    r = S.get(f"{BASE}/api/deposit/depositions/{dep_id}")
    d = r.json()
    print(f"    state={d['state']}  submitted={d['submitted']}  files={len(d['files'])}")
    print(f"    draft URL: {d['links']['html']}")

    if KEEP:
        print(f"\nDRAFT KEPT. Inspect it, then remove with:")
        print(f"    python delete_draft.py {dep_id}")
        sys.exit(0)

finally:
    if not KEEP:
        step(6, f"DELETE /api/deposit/depositions/{dep_id}   (cleanup)")
        r = S.delete(f"{BASE}/api/deposit/depositions/{dep_id}")
        print(f"    {r.status_code} {'deleted - nothing left behind' if r.status_code == 204 else r.text[:200]}")

print(f"\nall endpoints exercised in {time.time()-t0:.1f}s. Nothing was published.")
