"""
Publish a NEW VERSION of an already-published Zenodo record.

A new version keeps the DOI version chain intact: the concept DOI (the "all
versions" DOI) keeps resolving to the newest version, every version keeps its
own permanent DOI, and the record page grows a version selector. This is the
correct way to release a revised paper -- do NOT upload a fresh deposition,
that produces an unrelated record with no link back to the original.

Flow used by --new (https://developers.zenodo.org/#new-version):
  1. POST /api/deposit/depositions/:id/actions/newversion  -> links.latest_draft
  2. GET  <latest_draft>                                   -> new draft id + bucket
  3. GET/DELETE /api/deposit/depositions/:newid/files/:fid -> drop inherited files
  4. PUT  <bucket>/<filename>                              -> upload the revision
  5. PUT  /api/deposit/depositions/:newid                  -> carried-over metadata
  6. stop. The draft stays unpublished unless --publish is given as well.

Nothing is ever published without --publish AND typing the record title at the
prompt. Publishing is irreversible.

Token scopes: deposit:write (+ deposit:actions to publish). Listing the chain of
a public record id needs no token at all.

Usage:
    $env:ZENODO_TOKEN="..."                                  # PowerShell
    python zenodo_version.py                                 # your version trees
    python zenodo_version.py --list 8092663                  # one public record
    python zenodo_version.py --list 8092663 --all            # every version
    python zenodo_version.py --new 8092663 revised.pdf --version 2.0
    python zenodo_version.py --new 8092663 revised.pdf --version 2.0 --dry-run
    python zenodo_version.py --new 8092663 revised.pdf --version 2.0 --publish
"""

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import time
from datetime import date
from urllib.parse import quote

import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org").rstrip("/")
TOKEN = os.environ.get("ZENODO_TOKEN")

WIDTH = 78
TIMEOUT = 60
UPLOAD_TIMEOUT = (30, 900)          # (connect, read) -- uploads can be slow
POLITE_SLEEP = 0.35                 # between loop requests; guests get 60/min
HEAD_ROWS, TAIL_ROWS = 3, 6         # version rows shown around an elision
DEPOSIT_PAGE_SIZE = 100             # /api/deposit/depositions page size
MAX_PAGES = 50                      # 5000 depositions; stops a runaway loop

# One row of the version tree. Kept under 80 columns so it does not wrap.
ROW = "   %s %-4s %-26s %-10s  %-12s %-9s%s"

# Zenodo assigns these itself; carrying them into a new version is either
# rejected or silently wrong, so they are stripped from inherited metadata.
SERVER_OWNED = ("doi", "prereserve_doi", "relations", "version_id")

MISSING = object()

# Set as soon as a new-version draft exists. From then on every abort - a
# network drop mid-upload included - has to tell the user what was left behind
# and how to throw it away.
DRAFT_IN_FLIGHT = None


# --------------------------------------------------------------- formatting

def ascii_safe(value, limit=None, flatten=True):
    """Zenodo metadata is full of non-Latin1 text and the Windows console is
    cp1252, which raises on it. Everything printed from the server goes here."""
    text = "" if value is None else str(value)
    text = text.encode("ascii", "replace").decode("ascii")
    if flatten:
        text = " ".join(text.split())
    if limit and len(text) > limit:
        text = text[:limit - 3] + "..."
    return text


def box(lines):
    print("+" + "-" * (WIDTH - 2) + "+")
    for line in lines:
        print("| " + ascii_safe(line)[:WIDTH - 4].ljust(WIDTH - 4) + " |")
    print("+" + "-" * (WIDTH - 2) + "+")


def fail(message, code=1):
    print(message)
    if code and DRAFT_IN_FLIGHT is not None and "delete_draft.py" not in message:
        for line in discard_hints(DRAFT_IN_FLIGHT):
            print(line)
    sys.exit(code)


def human_size(nbytes):
    try:
        value = float(nbytes)
    except (TypeError, ValueError):
        return "? bytes"
    if value < 1024:
        return "%d bytes" % int(value)
    if value < 1024 * 1024:
        return "%.1f KB" % (value / 1024.0)
    return "%.2f MB" % (value / (1024.0 * 1024.0))


def md5_of(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------- http

def new_session(token=None):
    session = requests.Session()
    if token:
        session.headers.update({"Authorization": "Bearer %s" % token})
    return session


def call(session, method, url, **kwargs):
    """One HTTP call, retrying on 429. Returns a Response; exits with a message
    on a network failure instead of dumping a traceback.

    A dropped connection is retried only for GET and PUT, which are safe to
    repeat. A POST or DELETE that failed mid-flight may already have been
    applied server-side, so those are reported instead of replayed."""
    safe_to_repeat = method.upper() in ("GET", "PUT")
    delay = 5
    resp = None
    for attempt in range(4):
        try:
            resp = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            # Zenodo drops idle keep-alive sockets; one retry clears it.
            if not safe_to_repeat or attempt == 3:
                note = ("  giving up after 4 attempts." if safe_to_repeat else
                        "  a %s may already have been applied server-side - check\n"
                        "  the record in the web UI before retrying." % method.upper())
                fail("network error on %s %s\n  %s\n%s"
                     % (method, url, ascii_safe(exc), note))
            print("    connection dropped (%s) - retrying" % type(exc).__name__)
            time.sleep(2)
            continue
        if resp.status_code != 429:
            return resp
        header = resp.headers.get("Retry-After", "")
        wait = int(header) if header.isdigit() else delay
        print("    HTTP 429 rate limited - waiting %ds (attempt %d/4)"
              % (min(wait, 120), attempt + 1))
        time.sleep(min(wait, 120))
        delay *= 2
    return resp


def status_hints(code):
    if code == 401:
        return ["401 - token rejected. It may be expired, or it may belong to the",
                "      other host (sandbox tokens do not work on zenodo.org)."]
    if code == 403:
        return ["403 - permission denied. Either the record belongs to a different",
                "      Zenodo account, or the token lacks the needed scope",
                "      (deposit:write to edit, deposit:actions to publish)."]
    if code == 404:
        return ["404 - not found. Zenodo also answers 404 for records that exist",
                "      but belong to somebody else, and for concept ids on",
                "      endpoints that only accept a version id."]
    if code == 400:
        return ["400 - Zenodo rejected the request; the message above names the",
                "      offending field."]
    return []


def http_error(resp, what, extra=()):
    print("")
    print("ERROR: %s -- HTTP %s" % (what, resp.status_code))
    body = ascii_safe(resp.text, 400)
    if body:
        print("  server said: %s" % body)
    lines = list(status_hints(resp.status_code)) + list(extra)
    if DRAFT_IN_FLIGHT is not None and not any("delete_draft.py" in l for l in lines):
        lines += discard_hints(DRAFT_IN_FLIGHT)
    for line in lines:
        print("  %s" % line)


def must(resp, what, extra=()):
    if resp.ok:
        return resp
    http_error(resp, what, extra)
    sys.exit(1)


def deposit_hints(rec_id):
    return ["is %s really one of your own published records?" % rec_id,
            "'python check_token.py' lists every deposition this token can see."]


def discard_hints(draft_id):
    """Every abort after the draft exists has to say how to throw it away.
    delete_draft.py defaults to the sandbox, so name the host as well."""
    lines = ["draft %s exists and is NOT published; discard it with" % draft_id,
             "    python delete_draft.py %s" % draft_id]
    if "sandbox" not in BASE:
        lines.append('    (delete_draft.py defaults to the sandbox - run'
                     ' $env:ZENODO_BASE="%s" first)' % BASE)
    return lines


# ---------------------------------------------------------- version reading

def hits_total(payload):
    total = (payload.get("hits") or {}).get("total", 0)
    if isinstance(total, dict):                     # older Invenio shape
        total = total.get("value", 0)
    return int(total or 0)


def version_relation(hit):
    relations = ((hit.get("metadata") or {}).get("relations") or {}).get("version")
    if isinstance(relations, list) and relations:
        first = relations[0]
        return first if isinstance(first, dict) else {}
    return {}


def version_index(hit):
    """0-based position in the chain, straight from metadata.relations.version."""
    return version_relation(hit).get("index")


def fetch_chain(session, rec_id, page_size, fetch_all):
    """Return (versions_oldest_first, total, failed_response_or_None).

    The endpoint answers newest-first, capped at 25 per page for guests and 100
    for authenticated callers. Without --all we pull page 1 (newest) and the
    last page (oldest) only -- two requests are enough to draw a tree with a
    gap in the middle."""
    url = "%s/api/records/%s/versions" % (BASE, rec_id)
    resp = call(session, "GET", url,
                params={"size": page_size, "page": 1, "sort": "version"})
    if not resp.ok:
        return [], 0, resp
    payload = resp.json()
    total = hits_total(payload)
    hits = list((payload.get("hits") or {}).get("hits") or [])

    pages = int(math.ceil(total / float(page_size))) if total else 1
    wanted = list(range(2, pages + 1)) if fetch_all else ([pages] if pages > 1 else [])
    for page in wanted:
        time.sleep(POLITE_SLEEP)
        more = call(session, "GET", url,
                    params={"size": page_size, "page": page, "sort": "version"})
        if not more.ok:                              # deep-paging cap, 429, ...
            print("   (could not fetch page %d: HTTP %s)" % (page, more.status_code))
            break
        hits.extend((more.json().get("hits") or {}).get("hits") or [])

    unique = {}
    for hit in hits:
        unique[hit.get("id")] = hit
    values = list(unique.values())
    if any(version_index(h) is not None for h in values):
        values.sort(key=lambda h: (version_index(h) is None, version_index(h) or 0))
    else:
        values.reverse()                             # newest-first -> oldest-first
    return values, total, None


def trim_rows(hits, fetch_all):
    if fetch_all or len(hits) <= HEAD_ROWS + TAIL_ROWS:
        return hits
    return hits[:HEAD_ROWS] + hits[-TAIL_ROWS:]


def print_chain(record, hits, total, fetch_all):
    """ASCII tree: the concept DOI is the parent, one row per version."""
    title = record.get("title") or (record.get("metadata") or {}).get("title")
    print("")
    print("[%s] %s" % (record.get("id"), ascii_safe(title, 62) or "(untitled)"))
    print("   concept DOI : %s"
          % (record.get("conceptdoi") or "(none - record predates concept DOIs)"))
    print("   concept id  : %s   (parent of the whole chain)"
          % (record.get("conceptrecid") or "?"))
    print("   versions    : %d" % total)
    if not hits:
        print("   (no version chain returned)")
        return
    print(ROW % ("|  ", "#", "DOI", "published", "version", "record id", ""))

    rows = trim_rows(hits, fetch_all)
    hidden = len(hits) - len(rows)
    leading = version_index(rows[0]) or 0
    if leading:                                  # a page could not be fetched
        print("   |   ... %d earlier version(s) not shown ..." % leading)
    elided = False
    previous = None
    for position, hit in enumerate(rows):
        index = version_index(hit)
        gap = 0
        if previous is not None and index is not None and index > previous + 1:
            gap = index - previous - 1
        elif hidden and not elided and position == HEAD_ROWS:
            gap = hidden                         # no indexes: count by position
        if gap:
            print("   |   ... %d version(s) not shown - re-run with --all ..." % gap)
            elided = True
        connector = "'--" if position == len(rows) - 1 else "+--"
        label = "v%d" % (index + 1) if index is not None else "v?"
        meta = hit.get("metadata") or {}
        version_string = meta.get("version")
        print((ROW % (connector, label,
                      ascii_safe(hit.get("doi"), 26) or "(no DOI)",
                      ascii_safe(meta.get("publication_date"), 10) or "?",
                      ascii_safe(version_string, 12) if version_string else "-",
                      hit.get("id"),
                      " <- LATEST" if version_relation(hit).get("is_last") else "")
               ).rstrip())
        previous = index


# -------------------------------------------------------------- list command

def published_depositions(session):
    """Every deposition the token owns, plus one entry per published chain."""
    collected, known = [], set()
    page = 1
    while page <= MAX_PAGES:
        resp = call(session, "GET", "%s/api/deposit/depositions" % BASE,
                    params={"size": DEPOSIT_PAGE_SIZE, "page": page})
        must(resp, "GET /api/deposit/depositions",
             ["without a valid token Zenodo answers 403 here."])
        batch = resp.json()
        if not isinstance(batch, list):
            fail("unexpected response from /api/deposit/depositions: %s"
                 % ascii_safe(json.dumps(batch), 200))
        # Stopping at "len(batch) < size" would silently drop everything after
        # page 1 if the server capped the page size, so page until nothing new
        # comes back. The id set also stops a server that ignores ?page.
        fresh = [d for d in batch if d.get("id") not in known]
        for dep in fresh:
            known.add(dep.get("id"))
        collected.extend(fresh)
        if not fresh:
            break
        page += 1
        time.sleep(POLITE_SLEEP)
    if page > MAX_PAGES:
        print("(stopped after %d pages - showing the first %d deposition(s))"
              % (MAX_PAGES, len(collected)))

    seen, unique = set(), []
    for dep in collected:
        if dep.get("state") != "done":
            continue
        key = dep.get("conceptrecid") or dep.get("id")   # one tree per chain
        if key in seen:
            continue
        seen.add(key)
        unique.append(dep)
    return collected, unique


def cmd_list(args):
    session = new_session(TOKEN)
    page_size = 100 if TOKEN else 25            # guests are capped at 25 per page
    ids = args.list or []

    box(["ZENODO VERSION CHAINS",
         "host  %s" % BASE,
         "token %s" % ("present" if TOKEN else "absent (public records only)")])

    if ids:
        records = []
        for rec_id in ids:
            resp = call(session, "GET", "%s/api/records/%s" % (BASE, rec_id))
            if not resp.ok:
                http_error(resp, "GET /api/records/%s" % rec_id, ["skipping this id."])
                continue
            records.append(resp.json())
            time.sleep(POLITE_SLEEP)
        if not records:
            fail("\nno readable records among the ids given.")
    else:
        if not TOKEN:
            fail("\nZENODO_TOKEN is not set in this shell, so your own depositions\n"
                 "cannot be listed. Either set it:\n"
                 '    $env:ZENODO_TOKEN="..."\n'
                 "or name public record ids explicitly, which needs no token:\n"
                 "    python zenodo_version.py --list 8092663")
        everything, records = published_depositions(session)
        drafts = len([d for d in everything if d.get("state") != "done"])
        print("\n%d deposition(s), %d published chain(s), %d unpublished draft(s)."
              % (len(everything), len(records), drafts))
        if not records:
            fail("nothing published yet - there are no version chains to show.")

    for record in records:
        # For a published deposition the record id equals record_id; for a
        # concept id, GET /api/records already resolved to the newest version.
        rec_id = record.get("record_id") or record.get("id")
        hits, total, bad = fetch_chain(session, rec_id, page_size, args.all)
        if bad is not None:
            http_error(bad, "GET /api/records/%s/versions" % rec_id)
            continue
        print_chain(record, hits, total, args.all)
        time.sleep(POLITE_SLEEP)

    print("")
    print("A chain with 1 version has never been re-released. To publish a revision:")
    print("    python zenodo_version.py --new <record_id> <new_file.pdf> --version 2.0")


# ------------------------------------------------------ new version command

def build_metadata(old_meta, title=None, version=None):
    """Carry the previous version's metadata forward, minus anything Zenodo
    assigns itself, with today's publication date."""
    meta = copy.deepcopy(old_meta or {})
    for key in SERVER_OWNED:
        meta.pop(key, None)
    meta["publication_date"] = date.today().isoformat()
    if title:
        meta["title"] = title
    if version:
        meta["version"] = version
    return meta


def format_value(value):
    if value is MISSING:
        return "(absent)"
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return ascii_safe(text, 92)


def print_diff(old_meta, new_meta, indent="   "):
    changed = 0
    for key in sorted(set(old_meta or {}) | set(new_meta or {})):
        before = (old_meta or {}).get(key, MISSING)
        after = (new_meta or {}).get(key, MISSING)
        if before == after:
            continue
        changed += 1
        print("%s- %-17s %s" % (indent, key, format_value(before)))
        print("%s+ %-17s %s" % (indent, key, format_value(after)))
    if not changed:
        print("%s(metadata identical - only the files and the new DOI differ)" % indent)
    return changed


def confirm_yes(question, assume_yes):
    if assume_yes:
        print("%s? --yes given, continuing." % question)
        return True
    try:
        answer = input("%s? [y/N] " % question)
    except (EOFError, KeyboardInterrupt):
        print("")
        return False
    return answer.strip().lower() in ("y", "yes")


def confirm_publish(title, draft_id):
    """Typing the title back is the guard on the irreversible step. A title this
    console cannot render would be shown as '????', which is far too easy to
    type, so those records ask for the draft id instead."""
    text = str(title or "").strip()
    shown = ascii_safe(text, flatten=False)
    if text and shown == text:
        print("")
        print("To confirm, type the title of the new version exactly:")
        print("    %s" % shown)
        accepted = text
    else:
        print("")
        if text:
            print("The title contains characters this console cannot display, so")
        else:
            print("This draft has no readable title, so")
        print("type the draft id to confirm instead:")
        print("    %s" % draft_id)
        accepted = str(draft_id)
    try:
        typed = input("> ")
    except (EOFError, KeyboardInterrupt):
        print("")
        return False
    # bool() first: an empty accepted string must never be satisfied by an
    # empty answer, or the guard on the irreversible step becomes Enter.
    return bool(typed.strip()) and typed.strip() == accepted


def upload_file(session, bucket, path, filename):
    url = "%s/%s" % (bucket.rstrip("/"), quote(filename))
    delay = 5
    resp = None
    for attempt in range(3):
        try:
            with open(path, "rb") as handle:          # reopened on every attempt
                resp = session.put(url, data=handle, timeout=UPLOAD_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 2:
                fail("network error uploading %s\n  %s" % (filename, ascii_safe(exc)))
            print("    connection dropped (%s) - re-uploading from the start"
                  % type(exc).__name__)
            time.sleep(delay)
            continue
        if resp.status_code != 429:
            return resp
        print("    HTTP 429 rate limited - waiting %ds" % delay)
        time.sleep(delay)
        delay *= 2
    return resp


def cmd_new(args):
    if not TOKEN:
        fail("ZENODO_TOKEN is not set in this shell.\n"
             "--new needs a token with the deposit:write scope "
             "(and deposit:actions if you pass --publish).")

    src_id, path = args.new

    # Validate locally BEFORE touching Zenodo, so a typo cannot leave an
    # orphaned new-version draft hanging off a published record.
    if os.path.isdir(path):
        fail("that is a directory, not a file: %s\n"
             "A new version carries exactly one file; name it explicitly." % path)
    if not os.path.isfile(path):
        fail("file not found: %s" % path)
    file_size = os.path.getsize(path)
    if file_size == 0:
        fail("file is empty: %s" % path)
    filename = os.path.basename(path)
    local_md5 = md5_of(path)

    session = new_session(TOKEN)

    # ---------------------------------------------------- 1. read the source
    resp = call(session, "GET", "%s/api/deposit/depositions/%s" % (BASE, src_id))
    must(resp, "GET /api/deposit/depositions/%s" % src_id, deposit_hints(src_id))
    source = resp.json()
    state = source.get("state")
    if state != "done":
        fail("deposition %s is in state '%s', not 'done'.\n"
             "Only an already-published record can get a new version; an\n"
             "unpublished draft should just be edited in place." % (src_id, state))

    old_meta = source.get("metadata") or {}
    new_meta = build_metadata(old_meta, args.title, args.version)
    inherited = source.get("files") or []
    is_last = version_relation(source).get("is_last")

    # -------------------------------------------------------------- 2. plan
    box(["NEW VERSION PLAN - nothing has been sent to Zenodo yet",
         "host %s" % BASE])
    print("   source record   : %s  %s"
          % (source.get("id"), ascii_safe(source.get("title"), 45)))
    print("   source DOI      : %s   (published %s)"
          % (source.get("doi"), ascii_safe(old_meta.get("publication_date"), 10)))
    print("   concept DOI     : %s   (unchanged, keeps pointing at the newest)"
          % (source.get("conceptdoi") or "(none)"))
    print("   new DOI         : minted by Zenodo at publish time")
    print("   file to upload  : %s  (%s, md5 %s)"
          % (ascii_safe(filename, 40), human_size(file_size), local_md5))
    if inherited:
        verb = "KEPT (--keep-files)" if args.keep_files else "DELETED from the draft"
        for item in inherited:
            print("   inherited file  : %-38s -> %s"
                  % (ascii_safe(item.get("filename"), 38), verb))
    else:
        print("   inherited file  : none")
    print("")
    print("   metadata changes vs the current version:")
    print_diff(old_meta, new_meta)
    print("")
    print("   steps: newversion -> %supload -> metadata -> %s"
          % ("" if args.keep_files else "delete inherited files -> ",
             "PUBLISH (irreversible)" if args.publish else "stop, leave draft"))

    if is_last is False:
        print("")
        box(["WARNING - %s IS NOT THE NEWEST VERSION of its chain" % src_id,
             "A new version is cut from the newest one. Zenodo normally",
             "refuses this; check the chain before you continue:",
             "    python zenodo_version.py --list %s" % src_id])

    if args.dry_run:
        print("")
        print("--dry-run: the source record above was read and NOTHING was written.")
        print("Drop --dry-run to create the draft.")
        return

    print("")
    # --yes must not wave through a source that is not the newest version.
    if not confirm_yes("Create the new version draft",
                       args.yes and is_last is not False):
        fail("aborted - nothing was created.", 0)

    # ------------------------------------------------ 3. cut the new version
    print("")
    print("[1] POST /api/deposit/depositions/%s/actions/newversion" % src_id)
    resp = call(session, "POST",
                "%s/api/deposit/depositions/%s/actions/newversion" % (BASE, src_id))
    must(resp, "actions/newversion",
         ["if Zenodo says a new version is already drafted, finish or delete",
          "that draft first (python delete_draft.py <draft_id>)."])
    latest_draft = (resp.json().get("links") or {}).get("latest_draft")
    if not latest_draft:
        fail("newversion succeeded but returned no links.latest_draft - stopping\n"
             "before touching anything else. Check the record in the web UI.")

    print("[2] GET %s" % ascii_safe(latest_draft))
    resp = call(session, "GET", latest_draft)
    must(resp, "GET the new draft",
         ["a new-version draft may already exist on that record - check",
          "%s in the web UI before re-running." % BASE])
    draft = resp.json()
    new_id = draft.get("id")
    if not new_id:
        fail("newversion returned a draft with no id - stopping before anything\n"
             "else is touched. Check the record in the web UI.")

    # Everything below this point DELETES files and REPLACES metadata, so it
    # must be aimed at a fresh unpublished draft and never at the published
    # record we were asked to version.
    if str(new_id) == str(src_id):
        fail("newversion pointed back at the published record %s instead of a\n"
             "new draft. Refusing to delete files from or overwrite a published\n"
             "record. Nothing further was sent." % src_id)
    if draft.get("state") == "done" or draft.get("submitted") is True:
        fail("deposition %s came back as state '%s' (submitted=%s) - that is a\n"
             "published record, not a draft. Refusing to modify it. Nothing\n"
             "further was sent." % (new_id, draft.get("state"),
                                    draft.get("submitted")))

    global DRAFT_IN_FLIGHT
    DRAFT_IN_FLIGHT = new_id            # every abort from here on names it

    bucket = (draft.get("links") or {}).get("bucket")
    if not bucket:
        # Some responses carry only the html link; re-read the canonical
        # deposit endpoint once before giving up.
        again = call(session, "GET",
                     "%s/api/deposit/depositions/%s" % (BASE, new_id))
        if again.ok:
            draft = again.json()
            bucket = (draft.get("links") or {}).get("bucket")
    if not bucket:
        fail("draft %s has no links.bucket to upload into - stopping before\n"
             "anything else is changed.\n%s"
             % (new_id, "\n".join(discard_hints(new_id))))
    print("    new draft id=%s  state=%s" % (new_id, draft.get("state")))

    # --------------------------------------- 4. drop the inherited old files
    print("[3] GET /api/deposit/depositions/%s/files" % new_id)
    resp = call(session, "GET", "%s/api/deposit/depositions/%s/files" % (BASE, new_id))
    must(resp, "list the new draft's files")
    existing = resp.json()
    existing = existing if isinstance(existing, list) else []
    if not existing:
        print("    (the draft inherited no files)")
    for item in existing:
        label = "%s (%s)" % (ascii_safe(item.get("filename"), 40),
                             human_size(item.get("filesize")))
        if args.keep_files:
            print("    keeping  %s" % label)
            continue
        drop = call(session, "DELETE", "%s/api/deposit/depositions/%s/files/%s"
                    % (BASE, new_id, item.get("id")))
        if drop.status_code not in (200, 204):
            http_error(drop, "delete inherited file %s" % label,
                       discard_hints(new_id))
            sys.exit(1)
        print("    deleted  %s" % label)
        time.sleep(POLITE_SLEEP)

    # -------------------------------------------------- 5. upload the new file
    print("[4] PUT {bucket}/%s" % ascii_safe(filename, 40))
    started = time.time()
    resp = upload_file(session, bucket, path, filename)
    must(resp, "upload %s" % ascii_safe(filename, 40), discard_hints(new_id))
    uploaded = resp.json()
    elapsed = max(time.time() - started, 0.001)
    checksum = str(uploaded.get("checksum") or "")
    print("    %s bytes in %.1fs  server checksum %s"
          % (uploaded.get("size"), elapsed,
             ascii_safe(checksum, 40) or "(none returned)"))
    if not checksum:
        fail("    integrity: UNVERIFIED\n"
             "the server returned no checksum, so this upload could not be\n"
             "verified. Do not publish this draft blindly.\n%s"
             % "\n".join(discard_hints(new_id)))
    matches = checksum.split(":")[-1].strip().lower() == local_md5
    print("    integrity: %s" % ("MATCH" if matches else "MISMATCH"))
    if not matches:
        fail("upload checksum does not match the local file - do not publish\n"
             "this draft.\n%s" % "\n".join(discard_hints(new_id)))

    # ----------------------------------------------------------- 6. metadata
    print("[5] PUT /api/deposit/depositions/%s   (metadata)" % new_id)
    resp = call(session, "PUT", "%s/api/deposit/depositions/%s" % (BASE, new_id),
                json={"metadata": new_meta})
    must(resp, "set metadata on draft %s" % new_id, discard_hints(new_id))
    draft = resp.json()
    saved_meta = draft.get("metadata") or {}
    print("    title   : %s" % ascii_safe(saved_meta.get("title"), 60))
    print("    version : %s" % ascii_safe(saved_meta.get("version") or "(unset)", 40))
    print("    date    : %s" % ascii_safe(saved_meta.get("publication_date"), 12))

    draft_url = ((draft.get("links") or {}).get("html")
                 or "%s/deposit/%s" % (BASE, new_id))

    print("")
    box(["DRAFT CREATED - NOT PUBLISHED, no DOI minted yet",
         "draft id  %s" % new_id,
         "review it %s" % draft_url])

    if not args.publish:
        print("Next: review that page and publish from there, or throw it away:")
        for line in discard_hints(new_id):
            print(line)
        print("Do not re-run --new to publish - that would cut yet another version.")
        return

    publish_draft(session, source, old_meta, new_id, draft, draft_url)


def draft_filenames(session, new_id, fallback):
    """What is REALLY on the draft. The metadata PUT does not always echo the
    file list back, and "(none)" on the screen right before an irreversible
    publish is the one thing this review must never get wrong."""
    resp = call(session, "GET",
                "%s/api/deposit/depositions/%s/files" % (BASE, new_id))
    if resp.ok:
        payload = resp.json()
        if isinstance(payload, list):
            return [ascii_safe(f.get("filename") or f.get("key"), 60)
                    for f in payload], True
    return [ascii_safe(f.get("filename") or f.get("key"), 60)
            for f in (fallback or [])], False


def publish_draft(session, source, old_meta, new_id, draft, draft_url):
    saved_meta = draft.get("metadata") or {}
    title = saved_meta.get("title") or draft.get("title") or ""

    print("")
    box(["REVIEW BEFORE PUBLISHING",
         "draft %s  ->  new version of record %s" % (new_id, source.get("id"))])
    print("   metadata diff, previous version -> this new version:")
    print_diff(old_meta, saved_meta)
    print("")
    print("   files on the previous version:")
    for item in (source.get("files") or []):
        print("     %s" % ascii_safe(item.get("filename"), 60))
    if not (source.get("files") or []):
        print("     (none)")
    names, confirmed = draft_filenames(session, new_id, draft.get("files"))
    print("   files on this new version:")
    for name in names:
        print("     %s" % name)
    if not names:
        print("     (NONE - Zenodo refuses to publish a record with no files)")
    if not confirmed:
        print("     (warning: the draft's file list could not be re-read; the")
        print("      list above comes from the earlier response)")

    print("")
    box(["IRREVERSIBLE - READ THIS",
         "Publishing mints a permanent DOI for this new version.",
         "A published record can NEVER be deleted and its FILES can never be",
         "changed, replaced or removed afterwards. Only the metadata stays",
         "editable. The concept DOI %s" % (source.get("conceptdoi") or "(none)"),
         "will start resolving to this new version instead of the current one."])
    print("   review the draft first: %s" % draft_url)

    if not confirm_publish(title, new_id):
        fail("\nconfirmation did not match - NOTHING was published.\n"
             "The draft is still there: %s\n%s"
             % (draft_url, "\n".join(discard_hints(new_id))))

    print("")
    print("[6] POST /api/deposit/depositions/%s/actions/publish" % new_id)
    resp = call(session, "POST",
                "%s/api/deposit/depositions/%s/actions/publish" % (BASE, new_id))
    must(resp, "publish draft %s" % new_id,
         ["nothing was published; the draft is still at %s" % draft_url,
          "a 403 here usually means the token lacks the deposit:actions scope."])
    record = resp.json()
    links = record.get("links") or {}
    print("")
    box(["PUBLISHED",
         "version DOI %s" % record.get("doi"),
         "concept DOI %s" % (record.get("conceptdoi") or "(none)"),
         "record      %s" % (links.get("record_html") or links.get("html") or "?")])
    print("Check the chain with:")
    print("    python zenodo_version.py --list %s" % record.get("id"))


# --------------------------------------------------------------------- cli

EXAMPLES = """
examples:
  python zenodo_version.py
      version tree of every record you have published (needs ZENODO_TOKEN)

  python zenodo_version.py --list 8092663 --all
      full version tree of any public record; no token required

  python zenodo_version.py --new 8092663 revised.pdf --version 2.0 --dry-run
      show exactly what a new version would change, send nothing

  python zenodo_version.py --new 8092663 revised.pdf --version 2.0
      create the new-version draft and stop: unpublished, no DOI minted

  python zenodo_version.py --new 8092663 revised.pdf --version 2.0 --publish
      same, then show the diff and ask you to type the title before minting
      the permanent DOI

environment:
  ZENODO_TOKEN   API token (deposit:write, plus deposit:actions to publish)
  ZENODO_BASE    https://zenodo.org (default) or https://sandbox.zenodo.org
"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="zenodo_version.py",
        description="Inspect Zenodo version chains and release a new version of "
                    "an already-published record. Read-only unless --new is "
                    "given, and never publishes without --publish plus a typed "
                    "confirmation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES)
    parser.add_argument("--list", nargs="*", metavar="RECORD_ID",
                        help="show version chains. With no ids, every record you "
                             "have published (needs a token). With ids, those "
                             "public records (no token needed). This is the "
                             "default when no arguments are given.")
    parser.add_argument("--new", nargs=2, metavar=("RECORD_ID", "FILE"),
                        help="create a new version of RECORD_ID carrying FILE. "
                             "Stops at an unpublished draft.")
    parser.add_argument("--keep-files", action="store_true",
                        help="keep the files inherited from the previous version. "
                             "Off by default, otherwise the new record ends up "
                             "holding both the old and the new PDF.")
    parser.add_argument("--title", metavar="TEXT",
                        help="override the title on the new version.")
    parser.add_argument("--version", metavar="TEXT",
                        help="set metadata.version on the new version, e.g. 2.0.")
    parser.add_argument("--publish", action="store_true",
                        help="after building the draft, show the diff and publish "
                             "once you type the record title. IRREVERSIBLE: mints "
                             "a permanent DOI and freezes the files.")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --new, print the plan and the diff, send nothing.")
    parser.add_argument("--yes", action="store_true",
                        help="skip the [y/N] prompt for creating the draft. Does "
                             "NOT skip the typed confirmation for --publish.")
    parser.add_argument("--all", action="store_true",
                        help="with --list, fetch and print every version instead "
                             "of the newest and oldest few.")
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    if args.new and args.list is not None:
        fail("--list and --new do opposite things; pass only one of them.")
    if args.new:
        return cmd_new(args)

    stray = [name for name, given in (("--publish", args.publish),
                                      ("--dry-run", args.dry_run),
                                      ("--keep-files", args.keep_files),
                                      ("--title", args.title),
                                      ("--version", args.version)) if given]
    if stray:
        fail("these flags only mean something together with --new: %s\n"
             "Nothing was sent to Zenodo. See --help." % ", ".join(stray))
    return cmd_list(args)


if __name__ == "__main__":
    main(sys.argv[1:])
