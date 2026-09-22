"""
Upload a research paper to Zenodo directly from Python.

Flow (per https://developers.zenodo.org/#quickstart-upload):
  1. POST /api/deposit/depositions            -> create empty deposition, get bucket URL
  2. PUT  <bucket_url>/<filename>             -> stream the PDF up (new files API)
  3. PUT  /api/deposit/depositions/<id>       -> attach metadata
  4. POST /api/deposit/depositions/<id>/actions/publish  -> mint the DOI

Token scopes needed: deposit:write (+ deposit:actions to publish).
Get one at https://sandbox.zenodo.org/account/settings/applications/tokens/new/

Usage:
    set ZENODO_TOKEN=...            (PowerShell: $env:ZENODO_TOKEN="...")
    python zenodo_upload.py paper.pdf
"""

import os
import sys
import requests

# Start on sandbox. Switch to "https://zenodo.org" only when you are sure --
# a published record on production cannot be deleted and the DOI is permanent.
BASE_URL = os.environ.get("ZENODO_BASE", "https://zenodo.org")
TOKEN = os.environ["ZENODO_TOKEN"]

METADATA = {
    "metadata": {
        "upload_type": "publication",
        "publication_type": "article",      # article | preprint | thesis | conferencepaper | ...
        "title": "My Research Paper Title",
        "description": "<p>Abstract of the paper goes here (HTML allowed).</p>",
        "creators": [
            {"name": "Doe, John", "affiliation": "My University",
             "orcid": "0000-0002-1825-0097"},
        ],
        "access_right": "open",
        "license": "cc-by-4.0",
        "keywords": ["machine learning", "example"],
        # "publication_date": "2026-08-20",   # defaults to today
        # "communities": [{"identifier": "zenodo"}],
        # "related_identifiers": [
        #     {"relation": "isSupplementTo", "identifier": "10.1234/foo"}
        # ],
    }
}


def upload(path, publish=False):
    # Validate BEFORE creating anything server-side, otherwise a bad path
    # leaves an orphaned empty draft behind on Zenodo.
    if not os.path.isfile(path):
        sys.exit(f"file not found: {path}")
    size = os.path.getsize(path)
    if size == 0:
        sys.exit(f"file is empty: {path}")
    print(f"about to upload {path} ({size/1e6:.2f} MB) to {BASE_URL}")

    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {TOKEN}"})

    # 1. create the deposition
    r = s.post(f"{BASE_URL}/api/deposit/depositions", json={})
    r.raise_for_status()
    dep = r.json()
    dep_id = dep["id"]
    bucket = dep["links"]["bucket"]
    print(f"deposition {dep_id} created")

    # 2. stream the file into the bucket (handles large files, up to 50 GB)
    filename = os.path.basename(path)
    with open(path, "rb") as fh:
        r = s.put(f"{bucket}/{filename}", data=fh)
    r.raise_for_status()
    print(f"uploaded {filename} ({r.json()['size']} bytes, md5 {r.json()['checksum']})")

    # 3. attach metadata
    r = s.put(f"{BASE_URL}/api/deposit/depositions/{dep_id}", json=METADATA)
    r.raise_for_status()
    print("metadata attached")
    print("prereserved DOI:", r.json()["metadata"].get("prereserve_doi"))

    # 4. publish -- irreversible, mints the DOI
    if publish:
        r = s.post(f"{BASE_URL}/api/deposit/depositions/{dep_id}/actions/publish")
        r.raise_for_status()
        rec = r.json()
        print("published:", rec["doi"], rec["links"]["record_html"])
    else:
        print("draft left unpublished:",
              f"{BASE_URL}/deposit/{dep_id}")

    return dep_id


if __name__ == "__main__":
    upload(sys.argv[1], publish="--publish" in sys.argv)
