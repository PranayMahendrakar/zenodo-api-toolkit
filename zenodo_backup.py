r"""
Back up everything you have on Zenodo to local disk. Strictly read-only.

For every published deposition this writes

    <outdir>/<record_id>_<slug-of-title>/
        metadata.json     full deposition JSON  (legacy deposit API)
        record.json       public /api/records/<id> JSON, which carries .stats
        <files...>        every attached file, streamed and md5-verified

plus <outdir>/manifest.json listing every record, file, size, checksum and
verification result, with a run timestamp.

Safety properties, by construction:
  * the HTTP session refuses to issue anything but GET/HEAD, so this tool
    cannot publish, edit or delete anything on Zenodo;
  * downloaded data files are never overwritten or deleted. A download lands
    on a path that did not exist a moment earlier; if a file already exists
    with a different checksum the fresh copy is written beside it as
    <name>.new and the difference is reported.
    (The tool's own bookkeeping -- metadata.json, record.json, manifest.json
    -- is rewritten on every run; that is the point of running it again.)

Reruns are resumable: a file already on disk whose md5 matches Zenodo is
skipped without re-downloading.

Usage (PowerShell):
    $env:ZENODO_TOKEN="..."
    python zenodo_backup.py
    python zenodo_backup.py --outdir D:\backups\zenodo
    python zenodo_backup.py --metadata-only
    python zenodo_backup.py --record 3911877     # single record; public ones
                                                 # work with no token at all
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
TOKEN = os.environ.get("ZENODO_TOKEN")

CHUNK = 1024 * 256          # 256 KiB streaming chunks
PART_SUFFIX = ".zenodo-part"

# Zenodo allows 100 results per page only to authenticated callers; guests are
# capped at 25 and get an HTTP 400 if they ask for more.
PAGE_SIZE = 100 if TOKEN else 25

# Documented rate limits: 100 req/min authenticated, 60/min as a guest. Stay
# just inside them rather than relying on 429 backoff to bail us out.
POLITE_SLEEP = 0.7 if TOKEN else 1.1

# Carriage-return repainting only makes sense on a real console; when output is
# piped to a file it just leaves a trail of half-drawn lines.
IS_TTY = bool(getattr(sys.stdout, "isatty", lambda: False)())
CR = "\r" if IS_TTY else ""


# --------------------------------------------------------------- read-only
class ReadOnlySession(requests.Session):
    """A requests Session that physically cannot mutate anything on Zenodo.

    Both entry points are guarded: request() catches ordinary calls, and
    send() catches anything handed a pre-built request, plus the redirect
    chain (a 307/308 preserves the method, so it is worth re-checking).
    """

    SAFE = ("GET", "HEAD")

    def request(self, method, url, *args, **kwargs):
        if str(method).upper() not in self.SAFE:
            raise RuntimeError(
                "zenodo_backup is read-only; blocked %s %s" % (method, url))
        return super().request(method, url, *args, **kwargs)

    def send(self, request, **kwargs):
        method = str(getattr(request, "method", "")).upper()
        if method not in self.SAFE:
            raise RuntimeError(
                "zenodo_backup is read-only; blocked %s %s"
                % (method, getattr(request, "url", "?")))
        return super().send(request, **kwargs)


# ------------------------------------------------------------------ helpers
def ascii_safe(text):
    """Windows consoles are cp1252 -- never let a title crash the run."""
    return str(text).encode("ascii", "replace").decode("ascii")


def human(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.0f B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def slugify(text, limit=60):
    s = ascii_safe(text or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:limit].rstrip("-") or "untitled"


_BAD_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = ({"con", "prn", "aux", "nul", "clock$"}
             | {"com%d" % i for i in range(1, 10)}
             | {"lpt%d" % i for i in range(1, 10)})


def safe_filename(name):
    """Map a Zenodo file key onto something Windows will accept."""
    name = _BAD_FS.sub("_", ascii_safe(name or "unnamed")).strip(" .")
    if not name:
        name = "unnamed"
    if name.split(".")[0].lower() in _RESERVED:
        name = "_" + name
    if len(name) > 180:                 # keep the extension when truncating
        stem, dot, ext = name.rpartition(".")
        name = (stem[:179 - len(ext)] + "." + ext) if dot and len(ext) <= 12 \
            else name[:180]
    return name


def md5_hex(checksum):
    """Zenodo reports 'md5:<hex>' on records and bare <hex> on depositions.

    Anything explicitly tagged with another algorithm returns None rather than
    a digest we would then compare against an md5 and wrongly call a mismatch.
    """
    if not checksum:
        return None
    c = str(checksum).strip()
    if ":" in c:
        algo, _, digest = c.partition(":")
        return digest.lower() if algo.lower() == "md5" else None
    return c.lower()


def md5_of_file(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, payload):
    """Write payload as JSON. Returns (size_bytes, error); one of them is None.

    ensure_ascii keeps the file readable on a cp1252 box, and a disk or
    permission failure has to come back as a message, not a traceback.
    """
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=True, sort_keys=True)
        return os.path.getsize(path), None
    except (OSError, TypeError, ValueError) as exc:
        return 0, "cannot write %s: %s" % (os.path.basename(path),
                                           ascii_safe(exc)[:160])


def free_path(path, tag=".new"):
    """First unused <path><tag>, <path><tag>.1, ... Never an existing file."""
    cand = path + tag
    i = 1
    while os.path.exists(cand):
        cand = "%s%s.%d" % (path, tag, i)
        i += 1
    return cand


def api_get(session, url, params=None, stream=False, tries=4):
    """GET with backoff. Returns (response, error); exactly one is None."""
    for attempt in range(1, tries + 1):
        try:
            r = session.get(url, params=params, stream=stream, timeout=(15, 300))
        except requests.RequestException as exc:
            if attempt == tries:
                return None, "network error: " + ascii_safe(exc)[:200]
            time.sleep(2 * attempt)
            continue
        if r.status_code == 429:
            wait = 5 * attempt
            try:
                wait = max(wait, int(r.headers.get("Retry-After", 0)))
            except (TypeError, ValueError):
                pass
            print("    rate limited (429) - waiting %ds" % wait)
            r.close()
            time.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < tries:
            r.close()
            time.sleep(2 * attempt)
            continue
        return r, None
    return None, "gave up after %d retries" % tries


def body_snippet(r, limit=300):
    """A short, single-line, ASCII rendering of an error body.

    Zenodo answers some 404s with a full HTML page; dumping 300 characters of
    doctype and meta tags tells the reader nothing, so say so instead.
    """
    try:
        text = r.text or ""
    except Exception:                       # pragma: no cover - decode oddity
        return "(unreadable body)"
    stripped = text.lstrip().lower()
    if stripped.startswith("<!doctype") or stripped.startswith("<html"):
        try:
            msg = (r.json() or {}).get("message")
        except ValueError:
            msg = None
        return ascii_safe(msg) if msg else "(HTML error page)"
    return " ".join(ascii_safe(text).split())[:limit]


def http_problem(r, what):
    return "%s: HTTP %d %s" % (what, r.status_code, body_snippet(r))


# ------------------------------------------------------------- API wrappers
TOKEN_REJECTED = (
    "Zenodo did not accept ZENODO_TOKEN. It refused the request as if it came\n"
    "  from an anonymous caller, which means the token is missing, expired,\n"
    "  revoked, mistyped, lacks the deposit:write scope, or belongs to the\n"
    "  other host (sandbox tokens do not work on zenodo.org, or vice versa).\n"
    "  Current host: %s")


def _size_limit_error(r):
    """True if this 400 is Zenodo's 'page size > 25 needs auth' complaint.

    Verified behaviour: an absent or invalid token is treated as anonymous,
    and the size check fires *before* the permission check, so a bad token
    surfaces as HTTP 400 about paging rather than 401/403. Reporting that
    verbatim sends the reader chasing a paging bug they do not have.
    """
    try:
        payload = r.json() or {}
    except ValueError:
        return False
    for e in payload.get("errors") or []:
        if isinstance(e, dict) and e.get("field") == "size":
            return True
    return False


def _mentions_all_versions(r):
    try:
        payload = r.json() or {}
    except ValueError:
        return False
    for e in payload.get("errors") or []:
        if isinstance(e, dict) and e.get("field") == "all_versions":
            return True
    return False


def list_depositions(session):
    """Every deposition on the account, paginated. Returns (list, error)."""
    out, page, all_versions = [], 1, True
    while True:
        params = {"size": PAGE_SIZE, "page": page}
        if all_versions:
            params["all_versions"] = 1
        r, err = api_get(session, BASE + "/api/deposit/depositions", params=params)
        if err:
            return None, err
        if r.status_code == 400:
            if _size_limit_error(r):
                r.close()
                return None, TOKEN_REJECTED % BASE
            if all_versions and _mentions_all_versions(r):
                all_versions = False    # some deploys reject the extra arg
                r.close()
                continue
            return None, http_problem(r, "listing depositions")
        if r.status_code == 401:
            return None, TOKEN_REJECTED % BASE
        if r.status_code == 403:
            return None, TOKEN_REJECTED % BASE
        if r.status_code != 200:
            return None, http_problem(r, "listing depositions")
        try:
            batch = r.json()
        except ValueError:
            return None, "listing depositions: response was not JSON"
        if not isinstance(batch, list):
            return None, ("listing depositions: expected a list, got %s"
                          % type(batch).__name__)
        out.extend(batch)
        print("  page %d: %d deposition(s)" % (page, len(batch)))
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        if page > 500:
            print("  stopping at page 500 (sanity guard)")
            break
        time.sleep(POLITE_SLEEP)
    return out, None


def get_deposition(session, dep_id):
    r, err = api_get(session, "%s/api/deposit/depositions/%s"
                     % (BASE, quote(str(dep_id), safe="")))
    if err:
        return None, err
    if r.status_code != 200:
        return None, http_problem(r, "deposition %s" % dep_id)
    try:
        return r.json(), None
    except ValueError:
        return None, "deposition %s: response was not JSON" % dep_id


def get_record(session, rec_id):
    r, err = api_get(session, "%s/api/records/%s"
                     % (BASE, quote(str(rec_id), safe="")))
    if err:
        return None, err
    if r.status_code != 200:
        return None, http_problem(r, "record %s" % rec_id)
    try:
        return r.json(), None
    except ValueError:
        return None, "record %s: response was not JSON" % rec_id


def file_list(container):
    """The file entries out of whichever shape Zenodo used.

    Today /api/records/<id> answers with a plain list (verified). InvenioRDM
    also serialises files as {"enabled": true, "entries": {...}} in places, so
    accept that too rather than iterating a dict and calling .get on a string.
    """
    files = (container or {}).get("files")
    if isinstance(files, list):
        return [f for f in files if isinstance(f, dict)]
    if isinstance(files, dict):
        entries = files.get("entries")
        if isinstance(entries, dict):
            out = []
            for key, meta in entries.items():
                if isinstance(meta, dict):
                    meta = dict(meta)
                    meta.setdefault("key", key)
                    out.append(meta)
            return out
        if isinstance(entries, list):
            return [f for f in entries if isinstance(f, dict)]
    return []


def normalise_files(record, deposition, rec_id):
    """One file shape out of two API dialects.

    public record : {key, size, checksum:'md5:<hex>', links:{self:<content>}}
    deposition    : {filename, filesize, checksum:'<hex>', links:{download}}
    """
    entries, seen = [], set()
    for f in file_list(record):
        key = f.get("key") or f.get("filename")
        if not key or key in seen:
            continue
        links = f.get("links") or {}
        # On a public record links.self already IS the /content URL (verified).
        # links.content is the InvenioRDM spelling; prefer it when both exist,
        # because there self points at file *metadata*, not the bytes.
        url = links.get("content") or links.get("self") or (
            "%s/api/records/%s/files/%s/content"
            % (BASE, quote(str(rec_id), safe=""), quote(key, safe="")))
        entries.append({"key": key, "size": f.get("size"),
                        "md5": md5_hex(f.get("checksum")), "url": url})
        seen.add(key)
    for f in file_list(deposition):
        key = f.get("filename") or f.get("key")
        if not key or key in seen:
            continue
        links = f.get("links") or {}
        url = links.get("download") or links.get("self") or (
            "%s/api/records/%s/files/%s/content"
            % (BASE, quote(str(rec_id), safe=""), quote(key, safe="")))
        entries.append({"key": key, "size": f.get("filesize") or f.get("size"),
                        "md5": md5_hex(f.get("checksum")), "url": url})
        seen.add(key)
    return entries


def deposition_sort_key(d):
    """Numeric where possible so 9 sorts before 10, not after it."""
    raw = d.get("record_id") or d.get("id")
    try:
        return (0, int(raw), "")
    except (TypeError, ValueError):
        return (1, 0, ascii_safe(raw))


# ------------------------------------------------------------- downloading
def show_progress(label, done, total):
    if not IS_TTY:
        return
    if total:
        pct = min(100.0, done * 100.0 / total)
        line = "      %-34.34s %5.1f%%  %s / %s" % (label, pct, human(done),
                                                    human(total))
    else:
        line = "      %-34.34s     ?%%  %s" % (label, human(done))
    sys.stdout.write("\r" + line + "        ")
    sys.stdout.flush()


def stream_to(session, url, tmp_path, label, expected_size):
    """Stream url into tmp_path. Returns (md5_hex, bytes_written, error)."""
    r, err = api_get(session, url, stream=True)
    if err:
        return None, 0, err
    if r.status_code != 200:
        problem = http_problem(r, "download")
        r.close()
        return None, 0, problem
    total = expected_size
    try:
        total = int(r.headers.get("Content-Length") or 0) or expected_size
    except (TypeError, ValueError):
        pass
    h = hashlib.md5()
    done, last = 0, 0.0
    try:
        with open(tmp_path, "wb") as fh:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > 0.2:
                    show_progress(label, done, total)
                    last = now
    except (requests.RequestException, OSError) as exc:
        return None, done, "transfer failed: " + ascii_safe(exc)[:200]
    finally:
        r.close()
    show_progress(label, done, total or done)
    return h.hexdigest(), done, None


def backup_file(session, entry, dest_dir, rel_dir):
    """Fetch one file. Returns a manifest row. Never overwrites or deletes."""
    key = entry["key"]
    local_name = safe_filename(key)
    final = os.path.join(dest_dir, local_name)
    label = ascii_safe(local_name)
    row = {
        "name": key,
        "local_name": local_name,
        "path": "%s/%s" % (rel_dir, local_name),
        "size_remote": entry.get("size"),
        "size_local": None,
        "checksum_remote": entry.get("md5"),
        "checksum_local": None,
        "bytes_downloaded": 0,
        "status": None,
        "note": "",
    }
    expected = entry.get("md5")

    # Already on disk? Verify rather than re-fetch -- this is what makes
    # reruns resumable, and it is the only branch that reads local files.
    if os.path.exists(final):
        try:
            have = md5_of_file(final)
        except OSError as exc:
            row["status"] = "ERROR"
            row["note"] = "cannot read existing file: " + ascii_safe(exc)[:120]
            print("      %-34.34s ERROR    %s" % (label, row["note"]))
            return row
        row["checksum_local"] = have
        row["size_local"] = os.path.getsize(final)
        if expected and have == expected:
            row["status"] = "SKIPPED"
            row["note"] = "already present, md5 matches"
            print("      %-34.34s SKIPPED  md5 ok, %s"
                  % (label, human(row["size_local"])))
            return row
        if not expected:
            row["status"] = "SKIPPED"
            row["note"] = "already present; Zenodo published no checksum to compare"
            print("      %-34.34s SKIPPED  present, no remote checksum" % label)
            return row
        # Differs. Leave the local copy strictly alone and write beside it.
        target = free_path(final)
        row["note"] = ("local copy differs from Zenodo; fresh copy written as %s"
                       % os.path.basename(target))
        print("      %-34.34s DIFFERS  local md5 %s != remote %s"
              % (label, have, expected))
    else:
        target = final

    # free_path, not final + PART_SUFFIX: a leftover (or user-owned) file at
    # that exact name would otherwise be overwritten by the transfer and then
    # renamed away or deleted. The scratch path must not already exist.
    tmp = free_path(final, PART_SUFFIX)
    got, nbytes, err = stream_to(session, entry["url"], tmp, label,
                                 entry.get("size"))
    if err:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)      # our own partial, created seconds ago
            except OSError:
                pass
        row["status"] = "ERROR"
        row["note"] = err
        print(CR + "      %-34.34s ERROR    %s        " % (label, err))
        return row

    if os.path.exists(target):      # defensive: never clobber
        target = free_path(target)
    os.rename(tmp, target)

    row["local_name"] = os.path.basename(target)
    row["path"] = "%s/%s" % (rel_dir, row["local_name"])
    row["size_local"] = nbytes
    row["bytes_downloaded"] = nbytes
    row["checksum_local"] = got

    if expected and got != expected:
        row["status"] = "MISMATCH"
        row["note"] = ((row["note"] + " | " if row["note"] else "")
                       + "expected md5 %s, got %s" % (expected, got))
        print(CR + "      %-34.34s MISMATCH %s  expected %s got %s        "
              % (ascii_safe(row["local_name"]), human(nbytes), expected, got))
    elif not expected:
        row["status"] = "OK"
        row["note"] = ((row["note"] + " | " if row["note"] else "")
                       + "no remote checksum published; md5 recorded locally")
        print(CR + "      %-34.34s OK       %s  md5 %s (unverified)        "
              % (ascii_safe(row["local_name"]), human(nbytes), got))
    else:
        row["status"] = "OK"
        print(CR + "      %-34.34s OK       %s  md5 verified                "
              % (ascii_safe(row["local_name"]), human(nbytes)))
    return row


# --------------------------------------------------------------- one record
def backup_record(session, rec_id, deposition, outdir, metadata_only):
    record, rec_err = get_record(session, rec_id)

    title = (deposition or {}).get("title") or ""
    if not title and record:
        title = ((record.get("metadata") or {}).get("title")
                 or record.get("title") or "")

    # safe_filename on the id as well: --record is user input, and an id like
    # "../../elsewhere" would otherwise make os.path.join write outside outdir.
    dirname = "%s_%s" % (safe_filename(str(rec_id)), slugify(title))

    entry = {
        "record_id": rec_id,
        "doi": (deposition or {}).get("doi") or (record or {}).get("doi"),
        "title": ascii_safe(title),
        "state": (deposition or {}).get("state") or (record or {}).get("state"),
        "dir": dirname,
        "metadata_json": None,
        "record_json": None,
        "stats": (record or {}).get("stats") or {},
        "files": [],
        "notes": [],
        "errors": [],
    }

    # Nothing to save at all -- do not leave an empty directory behind.
    if deposition is None and record is None:
        print("\n[%s] UNAVAILABLE" % rec_id)
        print("    " + (rec_err or "no deposition and no public record"))
        entry["dir"] = None
        entry["title"] = "(unavailable)"
        entry["errors"].append(rec_err or "no deposition and no public record")
        return entry

    print("\n[%s] %s" % (ascii_safe(rec_id), ascii_safe(title)[:64] or "(untitled)"))
    print("    dir: " + dirname)

    dest = os.path.join(outdir, dirname)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as exc:
        msg = "cannot create %s: %s" % (dirname, ascii_safe(exc)[:160])
        entry["dir"] = None
        entry["errors"].append(msg)
        print("    " + msg)
        return entry

    if deposition is not None:
        path = os.path.join(dest, "metadata.json")
        size, werr = write_json(path, deposition)
        if werr:
            entry["errors"].append(werr)
            print("    metadata.json  FAILED - " + werr)
        else:
            entry["metadata_json"] = dirname + "/metadata.json"
            print("    metadata.json  (%d bytes)" % size)
    else:
        # Without a token this is simply the expected state, not a failure.
        msg = ("no deposition JSON: this record is not yours, or the token "
               "cannot see it" if TOKEN else
               "no deposition JSON: running unauthenticated, public data only")
        entry["notes" if not TOKEN else "errors"].append(msg)
        print("    metadata.json  SKIPPED - no deposition access")

    if rec_err:
        entry["errors"].append(rec_err)
        print("    record.json    SKIPPED - " + rec_err)
    else:
        path = os.path.join(dest, "record.json")
        size, werr = write_json(path, record)
        if werr:
            entry["errors"].append(werr)
            print("    record.json    FAILED - " + werr)
        else:
            entry["record_json"] = dirname + "/record.json"
            # .stats lives on the public record, never on a deposition.
            s = entry["stats"]
            print("    record.json    (%d bytes)  views=%s downloads=%s"
                  % (size, s.get("views", 0), s.get("downloads", 0)))

    files = normalise_files(record, deposition, rec_id)
    if not files:
        print("    files: none listed")
        return entry

    if metadata_only:
        for f in files:
            entry["files"].append({
                "name": f["key"], "local_name": None, "path": None,
                "size_remote": f.get("size"), "size_local": None,
                "checksum_remote": f.get("md5"), "checksum_local": None,
                "bytes_downloaded": 0,
                "status": "NOT_FETCHED", "note": "--metadata-only",
            })
        print("    files: %d listed, not fetched (--metadata-only)" % len(files))
        return entry

    print("    files: %d" % len(files))
    for f in files:
        entry["files"].append(backup_file(session, f, dest, dirname))
        time.sleep(POLITE_SLEEP)
    return entry


# -------------------------------------------------------------------- report
def table(rows, headers, aligns=None):
    cols = len(headers)
    aligns = aligns or ["<"] * cols
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(row[i])))
    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [rule,
           "| " + " | ".join("%-*s" % (widths[i], headers[i])
                             for i in range(cols)) + " |",
           rule]
    for row in rows:
        out.append("| " + " | ".join(
            "%*s" % ((-widths[i]) if aligns[i] == "<" else widths[i], row[i])
            for i in range(cols)) + " |")
    out.append(rule)
    return "\n".join(out)


def print_summary(manifest, elapsed):
    records = manifest["records"]
    files = [f for r in records for f in r["files"]]
    n_ok = sum(1 for f in files if f["status"] == "OK")
    n_skip = sum(1 for f in files if f["status"] == "SKIPPED")
    n_not = sum(1 for f in files if f["status"] == "NOT_FETCHED")
    mismatched = [f for f in files if f["status"] == "MISMATCH"]
    errored = [f for f in files if f["status"] == "ERROR"]
    nbytes = sum(f.get("bytes_downloaded") or 0 for f in files)

    print("\n" + "=" * 72)
    print("  BACKUP SUMMARY")
    print("=" * 72)

    if records:
        rows = []
        for r in records:
            good = sum(1 for f in r["files"] if f["status"] in ("OK", "SKIPPED"))
            local = sum(f.get("size_local") or 0 for f in r["files"])
            rows.append([r["record_id"], r["doi"] or "-", good, len(r["files"]),
                         human(local), (r["title"][:44] or "(untitled)")])
        print(table(rows, ["record", "doi", "ok", "files", "on disk", "title"],
                    ["<", "<", ">", ">", ">", "<"]))
        print("")

    stats = [
        ["records backed up", len(records)],
        ["files downloaded", n_ok],
        ["files skipped (already correct)", n_skip],
        ["files listed but not fetched", n_not],
        ["checksum MISMATCH", len(mismatched)],
        ["errors", len(errored) + sum(len(r["errors"]) for r in records)],
        ["bytes downloaded this run", "%s (%s)" % (format(nbytes, ","), human(nbytes))],
        ["elapsed", "%.1fs" % elapsed],
    ]
    print(table(stats, ["metric", "value"], ["<", ">"]))

    if mismatched:
        print("\nMISMATCHES - inspect these, the copy on disk may be corrupt:")
        for f in mismatched:
            print("  %s  %s" % (f["path"], f["note"]))
    if errored:
        print("\nFILE ERRORS:")
        for f in errored:
            print("  %s  %s" % (ascii_safe(f["name"]), f["note"]))
    record_errors = [(r["record_id"], e) for r in records for e in r["errors"]]
    if record_errors:
        print("\nRECORD ERRORS:")
        for rid, e in record_errors:
            print("  [%s] %s" % (rid, e))
    record_notes = [(r["record_id"], n) for r in records
                    for n in r.get("notes", [])]
    if record_notes:
        print("\nNOTES:")
        for rid, n in record_notes:
            print("  [%s] %s" % (rid, n))

    print("\nmanifest: " + manifest["manifest_path"])


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        prog="zenodo_backup.py",
        description="Back up your Zenodo records to local disk. Read-only: "
                    "this tool only ever issues GET requests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="environment:\n"
               "  ZENODO_TOKEN   your personal access token (deposit:write scope)\n"
               "  ZENODO_BASE    https://zenodo.org (default) or "
               "https://sandbox.zenodo.org\n\n"
               "Nothing is ever published, edited or deleted on Zenodo: the\n"
               "HTTP session refuses to issue anything but GET. Downloaded\n"
               "files are never overwritten either -- a copy that differs from\n"
               "Zenodo is written beside the original as <name>.new.")
    ap.add_argument("--outdir", default="zenodo_backup", metavar="PATH",
                    help="destination directory (default: ./zenodo_backup)")
    ap.add_argument("--record", metavar="ID",
                    help="back up a single record id instead of everything")
    ap.add_argument("--metadata-only", action="store_true",
                    help="write metadata.json/record.json, download no files")
    ap.add_argument("--include-drafts", action="store_true",
                    help="also save metadata.json for unpublished drafts")
    args = ap.parse_args()

    started = time.time()
    stamp = datetime.now(timezone.utc)

    session = ReadOnlySession()
    session.headers.update({"User-Agent": "zenodo_backup.py (read-only)"})
    if TOKEN:
        session.headers.update({"Authorization": "Bearer " + TOKEN})

    if not TOKEN and not args.record:
        sys.exit(
            "ZENODO_TOKEN is not set in this shell, so your deposition list\n"
            "cannot be read. Either set it --\n"
            '    $env:ZENODO_TOKEN="..."          (PowerShell)\n'
            "-- or back up a single public record with:\n"
            "    python zenodo_backup.py --record <id>")

    if args.record is not None and not str(args.record).strip():
        sys.exit("--record needs a record id, for example --record 3911877")

    outdir = os.path.abspath(args.outdir)
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as exc:
        sys.exit("cannot create the output directory %s\n  %s"
                 % (outdir, ascii_safe(exc)[:200]))

    print("=" * 72)
    print("  ZENODO BACKUP - read-only, nothing on Zenodo is modified")
    print("=" * 72)
    print("host   : " + BASE)
    print("token  : " + ("%s...%s (%d chars)" % (TOKEN[:6], TOKEN[-4:], len(TOKEN))
                         if TOKEN else "not set - public records only"))
    print("outdir : " + outdir)
    print("mode   : " + ("metadata only" if args.metadata_only
                         else "metadata + files"))

    records, drafts = [], []

    if args.record:
        rec_id = str(args.record).strip()
        deposition = None
        if TOKEN:
            deposition, err = get_deposition(session, rec_id)
            if err:
                print("\nnote: could not read deposition %s -- %s" % (rec_id, err))
                print("      falling back to the public record.")
        else:
            print("\nno token set - backing up the public record only.")
        records.append(backup_record(session, rec_id, deposition, outdir,
                                     args.metadata_only))
    else:
        print("\nenumerating depositions ...")
        deps, err = list_depositions(session)
        if err:
            sys.exit("could not list depositions -- " + err)
        deps = [d for d in deps if isinstance(d, dict)]
        published = sorted([d for d in deps if d.get("state") == "done"],
                           key=deposition_sort_key)
        drafts = sorted([d for d in deps if d.get("state") != "done"],
                        key=deposition_sort_key)
        print("  %d deposition(s): %d published, %d draft(s)"
              % (len(deps), len(published), len(drafts)))

        for d in published:
            # record_id, not id: for a published deposition they match on
            # Zenodo today, but /api/records/<id> is keyed on the record id,
            # so ask for the field that actually means that.
            rec_id = d.get("record_id") or d.get("id")
            records.append(backup_record(session, str(rec_id), d, outdir,
                                         args.metadata_only))
            time.sleep(POLITE_SLEEP)

        if drafts:
            print("\ndrafts (unpublished - no DOI, no public record):")
            for d in drafts:
                print("  [%s] %-11s %s"
                      % (d.get("id"), d.get("state"),
                         ascii_safe(d.get("title") or "(untitled)")[:52]))
            if args.include_drafts:
                for d in drafts:
                    dirname = "draft-%s_%s" % (safe_filename(str(d.get("id"))),
                                               slugify(d.get("title")))
                    dest = os.path.join(outdir, dirname)
                    try:
                        os.makedirs(dest, exist_ok=True)
                    except OSError as exc:
                        print("  could not create %s: %s"
                              % (dirname, ascii_safe(exc)[:120]))
                        continue
                    _, werr = write_json(os.path.join(dest, "metadata.json"), d)
                    print("  %s %s/metadata.json"
                          % ("FAILED  " + werr + " for" if werr else "saved",
                             dirname))
            else:
                print("  (pass --include-drafts to save their metadata too)")

    files = [f for r in records for f in r["files"]]
    manifest = {
        "tool": "zenodo_backup.py",
        "generated_at": stamp.isoformat(),
        "base_url": BASE,
        "outdir": outdir,
        "authenticated": bool(TOKEN),
        "mode": "metadata-only" if args.metadata_only else "metadata+files",
        "record_filter": args.record,
        "manifest_path": os.path.join(outdir, "manifest.json"),
        "summary": {
            "records": len(records),
            "files_total": len(files),
            "files_downloaded": sum(1 for f in files if f["status"] == "OK"),
            "files_skipped": sum(1 for f in files if f["status"] == "SKIPPED"),
            "files_not_fetched": sum(1 for f in files if f["status"] == "NOT_FETCHED"),
            "files_mismatched": sum(1 for f in files if f["status"] == "MISMATCH"),
            "files_errored": sum(1 for f in files if f["status"] == "ERROR"),
            "record_errors": sum(len(r["errors"]) for r in records),
            "bytes_downloaded": sum(f.get("bytes_downloaded") or 0 for f in files),
            "elapsed_seconds": round(time.time() - started, 2),
        },
        "records": records,
        "drafts": [{"id": d.get("id"), "state": d.get("state"),
                    "title": ascii_safe(d.get("title") or "")} for d in drafts],
    }
    _, manifest_err = write_json(manifest["manifest_path"], manifest)

    print_summary(manifest, time.time() - started)
    if manifest_err:
        print("WARNING: " + manifest_err)

    bad = (manifest["summary"]["files_mismatched"]
           + manifest["summary"]["files_errored"]
           + manifest["summary"]["record_errors"]
           + (1 if manifest_err else 0))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\ninterrupted - nothing on Zenodo was touched.")
    except OSError as exc:
        # Disk full, path too long, permission denied: report it, do not
        # decorate the console with a traceback.
        sys.exit("local filesystem error: %s" % ascii_safe(exc)[:300])
