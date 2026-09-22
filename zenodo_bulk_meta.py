"""
Audit -- and optionally bulk-fix -- the metadata on your published Zenodo records.

Files on a published record are frozen forever, but METADATA is not: Zenodo lets
you reopen a published record with the edit -> update -> publish action cycle.
This script does that safely, one record at a time, and never touches files.

Everything is read-only until you pass --apply AND type APPLY at the prompt.

    $env:ZENODO_TOKEN="..."                             # PowerShell
    python zenodo_bulk_meta.py                          # audit every record
    python zenodo_bulk_meta.py --record 22026733        # audit one + dump JSON
    python zenodo_bulk_meta.py --add-keywords "digital twin,ai"      # dry run
    python zenodo_bulk_meta.py --add-keywords "digital twin,ai" --apply
    python zenodo_bulk_meta.py --set-orcid 0000-0002-1825-0097 --creator "Doe" --apply

Exactly one mutation flag per run, so you always know what class of change you
are making. PUT replaces the entire metadata object, so every write starts from
the record's own metadata dict, mutates a copy, and sends the whole thing back.
"""

import argparse
import copy
import difflib
import html
import json
import os
import re
import sys
import time

import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
TOKEN = os.environ.get("ZENODO_TOKEN")

PAGE_SIZE = 100          # depositions per listing page
MAX_PAGES = 50           # hard stop, 5000 depositions
SLEEP = 0.7              # between records; the 100 req/min limit is the floor
DESC_MIN = 100           # chars of plain text below which a description is thin

# Server-computed keys that must never be echoed back in a PUT. Note that
# metadata.doi is NOT in here: when you edit an already-published record Zenodo
# expects the record's own DOI to come back unchanged. (zenodo_version.py strips
# doi as well, but that is a different cycle -- a new version gets a new DOI.)
READONLY_KEYS = ("prereserve_doi", "relations", "version_id")

# key, column header, gap label for the summary, field label for the detail view
CHECKS = [
    ("orcid",    "ORCID", "creators missing orcid",       "creator orcid"),
    ("affil",    "AFFIL", "creators missing affiliation", "creator affiliation"),
    ("keywords", "KEYW",  "no keywords",                  "keywords"),
    ("license",  "LICN",  "no license",                   "license"),
    ("relids",   "RELID", "no related_identifiers",       "related_identifiers"),
    ("comms",    "COMM",  "no communities",               "communities"),
    ("desc",     "DESC",  "description under %d chars" % DESC_MIN,
                                                          "description length"),
    ("version",  "VERS",  "no version field",             "version"),
]

# Windows consoles are cp1252; never let a unicode title kill the run.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# --------------------------------------------------------------- formatting

def to_ascii(s):
    """Flatten to printable ASCII so tables stay aligned on a cp1252 console."""
    if s is None:
        return ""
    s = str(s).replace("\r", " ").replace("\n", " ")
    return s.encode("ascii", "replace").decode("ascii")


def trunc(s, n):
    s = to_ascii(s)
    return s if len(s) <= n else s[:n - 3] + "..."


def fmt_table(headers, rows, aligns=None):
    """ASCII table: +---+ borders only, no unicode."""
    aligns = aligns or ["l"] * len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def rule():
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            if aligns[i] == "r":
                out.append(cell.rjust(widths[i]))
            elif aligns[i] == "c":
                out.append(cell.center(widths[i]))
            else:
                out.append(cell.ljust(widths[i]))
        return "| " + " | ".join(out) + " |"

    lines = [rule(), line(headers), rule()]
    lines += [line(r) for r in rows]
    lines.append(rule())
    return "\n".join(lines)


def mask_token(token):
    """Never print a token. A short one gets hidden completely rather than
    having its two ends printed, which for a 10-char token is the whole thing."""
    if not token:
        return "(none)"
    if len(token) < 20:
        return "*" * 10
    return token[:6] + "..." + token[-4:]


def banner(title):
    print("\n" + "=" * 78)
    print("  " + to_ascii(title))
    print("=" * 78)


def strip_html(s):
    """Rough tag strip so description length measures readable text, not markup."""
    if not s:
        return ""
    s = str(s)                      # sparse records sometimes hand back non-strings
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------- HTTP plumbing

class Api:
    """Thin requests wrapper: retries 429, prints status + body, never raises."""

    def __init__(self, token):
        self.s = requests.Session()
        if token:
            self.s.headers.update({"Authorization": "Bearer " + token})

    def req(self, method, url, quiet=False, **kw):
        """Returns a Response, or None when the server never answered at all.

        A None is meaningful to the caller: for a write it means we do not know
        whether the server acted, so it must not be treated as 'it failed'.
        Retries are deliberately limited to idempotent GETs -- replaying a PUT
        or a publish action after a timeout is exactly how you double-publish.
        """
        if not url.startswith("http"):
            url = BASE + url
        kw.setdefault("timeout", 60)
        idempotent = method.upper() == "GET"
        net_tries = 4 if idempotent else 1     # never replay a write after a timeout
        net_used = 0
        throttled = 0
        while True:
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as exc:
                net_used += 1
                if net_used < net_tries:
                    time.sleep(2 * net_used)
                    continue
                print("    NETWORK ERROR %s %s" % (method, url))
                print("      %s: %s" % (type(exc).__name__, to_ascii(exc)[:200]))
                return None
            if r.status_code == 429 and throttled < 3:
                # A 429 means the server refused to act, so waiting it out is
                # safe for a write as well as for a read.
                throttled += 1
                try:
                    wait = float(r.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                wait = min(max(wait, 5.0 * throttled), 120.0)
                print("    429 rate limited, sleeping %.0fs" % wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500 and idempotent and net_used < net_tries - 1:
                net_used += 1
                time.sleep(2 * net_used)
                continue
            if not r.ok and not quiet:
                self.explain(method, url, r)
            return r

    @staticmethod
    def explain(method, url, r):
        print("    HTTP %s %s %s" % (r.status_code, method, url))
        body = to_ascii(r.text)[:400]
        if body:
            print("      " + body)
        if r.status_code == 401:
            print("      missing or invalid credentials -- check ZENODO_TOKEN is "
                  "set in this shell")
        elif r.status_code == 403:
            print("      token rejected: it may be invalid, may belong to the other "
                  "host (zenodo.org vs sandbox.zenodo.org), or may be missing the "
                  "deposit:write / deposit:actions scope")
        elif r.status_code == 404:
            print("      not found -- the id may belong to another account, or to "
                  "the other host (current ZENODO_BASE is %s)" % BASE)


def list_depositions(api):
    """Every deposition on the account, paginated."""
    deps, page = [], 1
    while page <= MAX_PAGES:
        r = api.req("GET", "/api/deposit/depositions",
                    params={"size": PAGE_SIZE, "page": page})
        if r is None or not r.ok:
            return None
        try:
            batch = r.json()
        except ValueError:
            print("    response was not JSON")
            return None
        if not isinstance(batch, list):
            print("    unexpected listing payload: " + to_ascii(batch)[:200])
            return None
        deps.extend(batch)
        if len(batch) < PAGE_SIZE:
            return deps
        page += 1
        time.sleep(0.3)
    print("  WARNING: stopped at the %d-page cap (%d depositions). Records beyond"
          % (MAX_PAGES, MAX_PAGES * PAGE_SIZE))
    print("           that were NOT looked at. Raise MAX_PAGES if you have more.")
    return deps


def get_deposition(api, dep_id):
    r = api.req("GET", "/api/deposit/depositions/%s" % dep_id)
    if r is None or not r.ok:
        return None
    try:
        return r.json()
    except ValueError:
        print("    response was not JSON")
        return None


# ----------------------------------------------------------------- auditing

def license_id(meta):
    """Deposit API gives 'cc-by-4.0'; the record API gives {'id': 'cc-by-4.0'}."""
    lic = meta.get("license")
    if isinstance(lic, dict):
        return (lic.get("id") or "").strip()
    if isinstance(lic, str):
        return lic.strip()
    return ""


def as_list(value):
    """Zenodo list fields are lists, but sparse/legacy records are not reliable.
    Never let list('some string') explode a string into characters."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def creator_dicts(meta):
    """Only dict entries are usable; anything else is ignored rather than raising."""
    return [c for c in as_list(meta.get("creators")) if isinstance(c, dict)]


def audit(meta):
    """Return ({check_key: (is_gap, cell_text)}, score)."""
    res = {}
    creators = creator_dicts(meta)

    for key, field in (("orcid", "orcid"), ("affil", "affiliation")):
        if not creators:
            res[key] = (True, "none")
        else:
            missing = [c for c in creators if not str(c.get(field) or "").strip()]
            res[key] = (bool(missing),
                        "ok" if not missing else "%d/%d" % (len(missing), len(creators)))

    res["keywords"] = (not as_list(meta.get("keywords")), "")
    res["license"] = (not license_id(meta), "")
    res["relids"] = (not as_list(meta.get("related_identifiers")), "")
    res["comms"] = (not as_list(meta.get("communities")), "")

    n = len(strip_html(meta.get("description")))
    res["desc"] = (n < DESC_MIN, "ok" if n >= DESC_MIN else str(n))

    res["version"] = (not str(meta.get("version") or "").strip(), "")

    for key in list(res):
        gap, text = res[key]
        res[key] = (gap, text or ("GAP" if gap else "ok"))
    score = sum(0 if res[k][0] else 1 for k in [c[0] for c in CHECKS])
    return res, score


def is_published(dep):
    return bool(dep.get("submitted")) or dep.get("state") == "done"


def audit_rows(api, deps):
    """GET each published deposition and build one table row per record."""
    rows, results = [], []
    total = len(deps)
    for i, d in enumerate(deps, 1):
        dep_id = d.get("id")
        print("  [%d/%d] GET /api/deposit/depositions/%s" % (i, total, dep_id))
        full = get_deposition(api, dep_id)
        if full is None:
            rows.append([str(dep_id), trunc(d.get("title"), 34)]
                        + ["?" for _ in CHECKS] + ["-"])
            continue
        meta = full.get("metadata") or {}
        res, score = audit(meta)
        results.append((full, meta, res, score))
        rows.append([str(dep_id), trunc(meta.get("title") or full.get("title"), 34)]
                    + [res[c[0]][1] for c in CHECKS]
                    + ["%d/%d" % (score, len(CHECKS))])
        if i < total:
            time.sleep(SLEEP)
    return rows, results


def print_audit(rows, results):
    headers = ["ID", "TITLE"] + [c[1] for c in CHECKS] + ["SCORE"]
    aligns = ["r", "l"] + ["c"] * len(CHECKS) + ["c"]
    print()
    print(fmt_table(headers, rows, aligns))
    print("  ok = present   GAP = missing   n/m = n of m creators missing it")
    print("  DESC shows the plain-text length when it is under %d chars" % DESC_MIN)

    if not results:
        return
    banner("GAP SUMMARY  (%d published record(s))" % len(results))
    srows = []
    for key, _, label, _field in CHECKS:
        n = sum(1 for _, _, res, _ in results if res[key][0])
        srows.append([label, str(n), "%5.1f%%" % (100.0 * n / len(results))])
    print(fmt_table(["GAP", "RECORDS", "SHARE"], srows, ["l", "r", "r"]))

    worst = sorted(results, key=lambda t: t[3])[:5]
    print("\n  lowest scoring records:")
    for full, meta, _, score in worst:
        print("    %d/%d  [%s] %s" % (score, len(CHECKS), full.get("id"),
                                      trunc(meta.get("title") or full.get("title"), 55)))
    avg = sum(t[3] for t in results) / float(len(results))
    print("\n  average score: %.2f/%d" % (avg, len(CHECKS)))


# ----------------------------------------------------------------- mutating

VALID_PUB_TYPES = {"article", "report", "workingpaper", "preprint",
                   "technicalnote", "conferencepaper", "thesis", "book",
                   "section", "patent", "deliverable", "milestone",
                   "proposal", "softwaredocumentation", "other"}

ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def orcid_checksum_ok(orcid):
    """ISO 7064 MOD 11-2, the check-digit scheme ORCID uses."""
    digits = orcid.replace("-", "")
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    result = (12 - (total % 11)) % 11
    return ("X" if result == 10 else str(result)) == digits[-1]


def clean_metadata(meta):
    """Copy of the record's metadata with server-computed keys removed."""
    m = copy.deepcopy(meta or {})
    for k in READONLY_KEYS:
        m.pop(k, None)
    return m


def plan(meta, args):
    """Return (new_metadata, notes). Never mutates the input."""
    new = copy.deepcopy(meta)
    notes = []

    if args.set_orcid or args.set_affiliation:
        needle = args.creator.lower()
        creators = creator_dicts(new)
        matched = 0
        for c in creators:
            name = str(c.get("name") or "")
            if needle not in name.lower():
                continue
            matched += 1
            if args.set_orcid:
                have = str(c.get("orcid") or "").strip()
                if have.upper() == args.set_orcid:
                    notes.append("creator '%s' already has that orcid" % to_ascii(name))
                elif have:
                    notes.append("REFUSED: creator '%s' already has a different orcid "
                                 "%s -- left untouched" % (to_ascii(name), to_ascii(have)))
                else:
                    c["orcid"] = args.set_orcid
            if args.set_affiliation:
                have = str(c.get("affiliation") or "").strip()
                if have == args.set_affiliation:
                    notes.append("creator '%s' already has that affiliation"
                                 % to_ascii(name))
                else:
                    if have:
                        notes.append("creator '%s' affiliation '%s' -> '%s'"
                                     % (to_ascii(name), to_ascii(have),
                                        to_ascii(args.set_affiliation)))
                    c["affiliation"] = args.set_affiliation
        if not matched:
            notes.append("no creator name contains '%s'" % to_ascii(args.creator))

    if args.add_keywords:
        existing = as_list(new.get("keywords"))
        lower = set(str(k).strip().lower() for k in existing)
        added = []
        for kw in args.add_keywords:
            if kw.lower() not in lower:
                existing.append(kw)
                lower.add(kw.lower())
                added.append(kw)
        if added:
            new["keywords"] = existing
        else:
            notes.append("all requested keywords are already present")

    if args.set_license:
        current = license_id(new)
        if current == args.set_license:
            notes.append("license is already " + current)
        else:
            if current:
                notes.append("license %s -> %s" % (current, args.set_license))
            new["license"] = args.set_license

    # Venue normalisation. publication_type and journal_title are metadata, so
    # they can be corrected on an already-published record without a new DOI.
    if args.set_venue:
        pub_type, journal = args.set_venue
        cur_type = new.get("publication_type") or "(unset)"
        if cur_type == pub_type:
            notes.append("publication_type is already " + pub_type)
        else:
            notes.append("publication_type %s -> %s" % (cur_type, pub_type))
            new["publication_type"] = pub_type
        if journal:
            cur_j = new.get("journal_title") or "(none)"
            if cur_j == journal:
                notes.append("journal_title is already " + journal)
            else:
                notes.append("journal_title %s -> %s" % (cur_j, journal))
                new["journal_title"] = journal

    dropped = sanitise_dates(new)
    if dropped:
        notes.append("dropped %d empty 'dates' entry that Zenodo emits but "
                     "will not accept back" % dropped)

    return new, notes


def sanitise_dates(meta):
    """Drop date entries that carry a type but no actual date.

    Zenodo returns entries like {"type": "accepted"} with no date value, then
    rejects that same structure on PUT with "metadata.dates: Invalid date
    provided". Any record carrying one cannot be metadata-edited at all until
    the empty entry is dropped. Returns how many were removed.
    """
    dates = meta.get("dates")
    if not isinstance(dates, list):
        return 0
    keep = [d for d in dates
            if isinstance(d, dict)
            and (d.get("date") or d.get("start") or d.get("end"))]
    dropped = len(dates) - len(keep)
    if dropped:
        if keep:
            meta["dates"] = keep
        else:
            meta.pop("dates", None)
    return dropped


def diff_text(dep_id, before, after):
    a = json.dumps(before, indent=2, sort_keys=True, ensure_ascii=True).splitlines()
    b = json.dumps(after, indent=2, sort_keys=True, ensure_ascii=True).splitlines()
    return list(difflib.unified_diff(a, b,
                                     fromfile="record %s BEFORE" % dep_id,
                                     tofile="record %s AFTER" % dep_id,
                                     lineterm="", n=3))


def license_title(entry):
    """title is {'en': '...'} in this vocabulary, but do not bet the run on it."""
    title = (entry or {}).get("title")
    if isinstance(title, dict):
        return to_ascii(title.get("en") or next(iter(title.values()), ""))
    return to_ascii(title or "")


def license_suggestions(api, lic):
    """Closest ids from the documented ?q= search, ranked by real similarity.

    The server's own ranking is useless for a typo -- q=bogus-license-xyz comes
    back with 351 hits led by 'abstyles' -- so rank the candidates locally.
    """
    r = api.req("GET", "/api/vocabularies/licenses", quiet=True,
                params={"q": lic, "size": 50})
    if r is None or not r.ok:
        return []
    try:
        ids = [h.get("id") for h in r.json()["hits"]["hits"] if h.get("id")]
    except (ValueError, KeyError, TypeError):
        return []
    return difflib.get_close_matches(lic.lower(), [str(i) for i in ids], n=5, cutoff=0.5)


def validate_license(api, lic):
    """Exact vocabulary lookup; no token needed. Unreachable = warn, not fail.

    Uses the item form of the documented licenses vocabulary, because the ?q=
    search is fuzzy: it returns hundreds of unrelated hits for a nonsense id and
    can push a genuinely valid id off the first page, which would reject a
    perfectly good license.
    """
    r = api.req("GET", "/api/vocabularies/licenses/%s" % lic, quiet=True)
    if r is None:
        print("  WARNING: could not reach the license vocabulary (no response); "
              "'%s' not verified" % lic)
        return True
    if r.ok:
        try:
            entry = r.json()
        except ValueError:
            entry = {}
        print("  license '%s' = %s" % (lic, license_title(entry) or "(no title)"))
        return True
    if r.status_code != 404:
        print("  WARNING: license vocabulary returned HTTP %s; '%s' not verified"
              % (r.status_code, lic))
        return True

    print("  ERROR: '%s' is not a Zenodo license id." % lic)
    if lic != lic.lower():
        probe = api.req("GET", "/api/vocabularies/licenses/%s" % lic.lower(), quiet=True)
        if probe is not None and probe.ok:
            print("  license ids are lowercase -- try '%s'" % to_ascii(lic.lower()))
            return False
    near = license_suggestions(api, lic)
    if near:
        print("  did you mean: " + ", ".join(to_ascii(i) for i in near))
    else:
        print("  browse ids at %s/api/vocabularies/licenses?q=%s"
              % (BASE, to_ascii(lic)))
    return False


def apply_one(api, dep_id, before_meta, new_meta):
    """re-check -> edit -> PUT -> publish, with discard-on-failure rollback.

    Returns (status, detail); status is 'ok', 'skipped', 'rolled-back',
    'unknown' or 'failed'.

    The re-check is not paranoia. PUT replaces the whole metadata object, and
    the diff the user approved was computed from a snapshot taken before the
    confirmation prompt -- possibly minutes ago, and earlier still for the last
    record of a long batch. If anything edited the record in between, writing
    the stale plan would silently discard that edit. So the published state is
    read again immediately before touching it and must still match byte for
    byte; if it does not, the record is left completely alone.
    """
    print("      GET  /api/deposit/depositions/%s   (re-check before writing)" % dep_id)
    cur = get_deposition(api, dep_id)
    if cur is None:
        return "skipped", "could not re-read it before writing; left alone"
    state = cur.get("state")
    if state != "done":
        return "skipped", ("state is now '%s', not a clean published record; "
                           "left alone" % to_ascii(state))
    if clean_metadata(cur.get("metadata")) != before_meta:
        return "skipped", ("metadata changed since the diff was printed; "
                           "left alone -- re-run to see the current state")

    print("      POST /actions/edit")
    r = api.req("POST", "/api/deposit/depositions/%s/actions/edit" % dep_id)
    if r is None or not r.ok:
        return "failed", "edit action refused; record untouched"

    def rollback(reason):
        print("      POST /actions/discard   (rolling back)")
        d = api.req("POST", "/api/deposit/depositions/%s/actions/discard" % dep_id)
        if d is not None and d.ok:
            return "rolled-back", reason + "; discarded, record is as it was"
        return "failed", (reason + "; DISCARD ALSO FAILED -- record left in edit "
                          "mode, fix it in the web UI")

    print("      PUT  /api/deposit/depositions/%s" % dep_id)
    r = api.req("PUT", "/api/deposit/depositions/%s" % dep_id,
                json={"metadata": new_meta})
    if r is None or not r.ok:
        # Either way the record is still an unpublished edit session, so
        # discarding restores exactly the published state it had.
        return rollback("metadata update rejected")

    print("      POST /actions/publish")
    r = api.req("POST", "/api/deposit/depositions/%s/actions/publish" % dep_id)
    if r is None:
        # No answer at all: the publish may or may not have gone through.
        # Discarding here could throw away a change that actually landed, so
        # stop and hand it to a human instead of guessing.
        return "unknown", ("no response to the publish call -- it may or may not "
                           "have gone through; check the record in the web UI "
                           "before re-running")
    if not r.ok:
        return rollback("publish failed")

    try:
        doi = (r.json() or {}).get("doi", "")
    except (ValueError, AttributeError):
        doi = ""
    return "ok", "published" + ((" " + to_ascii(doi)) if doi else "")


# --------------------------------------------------------------------- main

def _parser():
    p = argparse.ArgumentParser(
        prog="zenodo_bulk_meta.py",
        description="Audit (default, read-only) or bulk-fix metadata on your "
                    "published Zenodo records.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python zenodo_bulk_meta.py
  python zenodo_bulk_meta.py --record 22026733
  python zenodo_bulk_meta.py --add-keywords "digital twin,manufacturing"
  python zenodo_bulk_meta.py --add-keywords "digital twin" --apply
  python zenodo_bulk_meta.py --set-license cc-by-4.0 --record 22026733 --apply
  python zenodo_bulk_meta.py --set-orcid 0000-0002-1825-0097 --creator "Doe" --apply
  python zenodo_bulk_meta.py --set-affiliation "IIT Bombay" --creator "Doe" --apply

notes:
  Nothing is written without --apply AND typing APPLY at the prompt.
  Exactly one mutation flag per run.
  Republishing a metadata edit is irreversible; files are never touched.
  Host comes from ZENODO_BASE (default https://zenodo.org), token from ZENODO_TOKEN.

exit codes:
  0  clean: audit finished, or every planned record was republished
  1  no ZENODO_TOKEN, or --record pointed at an unpublished draft
  2  bad arguments, or the API could not be read
  3  confirmation refused (nothing was written)
  4  a record failed, or a publish result is unknown -- check it by hand
  5  nothing broke, but some records were skipped or rolled back
""")
    p.add_argument("--record", metavar="ID",
                   help="limit to one deposition id (audit also prints its full metadata)")
    p.add_argument("--set-orcid", metavar="ORCID",
                   help="add an orcid to creators matching --creator "
                        "(never overwrites a different existing orcid)")
    p.add_argument("--set-affiliation", metavar="TEXT",
                   help="set the affiliation of creators matching --creator")
    p.add_argument("--creator", metavar="NAME",
                   help="case-insensitive substring of the creator name to match; "
                        "required by --set-orcid / --set-affiliation")
    p.add_argument("--add-keywords", metavar="a,b,c",
                   help="comma-separated keywords to union with the existing ones "
                        "(never removes any)")
    p.add_argument("--set-license", metavar="ID",
                   help="license id, e.g. cc-by-4.0 "
                        "(checked against /api/vocabularies/licenses)")
    p.add_argument("--set-venue", nargs="+", metavar="PUB_TYPE [JOURNAL]",
                   help="set publication_type and, optionally, journal_title. "
                        'e.g. --set-venue article "Life of Research", or just '
                        "--set-venue article to set the type and leave the "
                        "journal unchanged.")
    p.add_argument("--apply", action="store_true",
                   help="actually run edit -> update -> publish after you type APPLY")
    return p


def parse_args(argv):
    parser = _parser()
    args = parser.parse_args(argv)

    mutations = [n for n, v in (("--set-orcid", args.set_orcid),
                                ("--set-affiliation", args.set_affiliation),
                                ("--add-keywords", args.add_keywords),
                                ("--set-license", args.set_license),
                                ("--set-venue", args.set_venue)) if v]
    if len(mutations) > 1:
        parser.error("one mutation flag at a time, got: " + ", ".join(mutations))
    if (args.set_orcid or args.set_affiliation) and not args.creator:
        parser.error("--set-orcid / --set-affiliation need --creator NAME")
    if args.apply and not mutations:
        parser.error("--apply needs a mutation flag; without one this is an audit")

    if args.set_venue:
        if len(args.set_venue) > 2:
            parser.error("--set-venue takes PUB_TYPE and optionally JOURNAL; "
                         "quote a journal name that contains spaces")
        pub_type = args.set_venue[0].strip()
        journal = args.set_venue[1].strip() if len(args.set_venue) == 2 else ""
        if pub_type not in VALID_PUB_TYPES:
            parser.error("invalid publication type %r; valid: %s"
                         % (pub_type, ", ".join(sorted(VALID_PUB_TYPES))))
        args.set_venue = (pub_type, journal)

    if args.set_orcid:
        args.set_orcid = args.set_orcid.strip().upper()
        if not ORCID_RE.match(args.set_orcid):
            parser.error("orcid must look like 0000-0002-1825-0097")
        if not orcid_checksum_ok(args.set_orcid):
            parser.error("orcid %s fails its ISO 7064 check digit" % args.set_orcid)
    if args.add_keywords:
        args.add_keywords = [k.strip() for k in args.add_keywords.split(",") if k.strip()]
        if not args.add_keywords:
            parser.error("--add-keywords was empty after parsing")
    if args.set_license:
        args.set_license = args.set_license.strip()
    if args.record:
        # Legacy deposition ids are integers. Insisting on that also keeps a
        # stray path fragment from being pasted straight into a request URL.
        args.record = args.record.strip()
        if not args.record.isdigit():
            parser.error("--record takes a numeric deposition id, e.g. 22026733")

    return args, mutations


def main(argv):
    args, mutations = parse_args(argv)

    if not TOKEN:
        print("ZENODO_TOKEN is not set in this shell.")
        print("This tool reads and writes your own depositions, so a token is required.")
        print('  PowerShell:  $env:ZENODO_TOKEN="..."')
        print("Create one at %s/account/settings/applications/tokens/new/" % BASE)
        return 1

    api = Api(TOKEN)
    print("host  : %s" % BASE)
    print("token : %s (%d chars)" % (mask_token(TOKEN), len(TOKEN)))
    print("mode  : %s" % ("FIX (%s)" % mutations[0] if mutations
                          else "AUDIT (read-only)"))

    if args.set_license and not validate_license(api, args.set_license):
        return 2

    # ---- collect the target depositions
    if args.record:
        dep = get_deposition(api, args.record)
        if dep is None:
            return 2
        if not is_published(dep):
            print("\n[%s] state=%s -- this is an unpublished draft, not a published "
                  "record." % (dep.get("id"), dep.get("state")))
            print("This tool only touches published records; edit the draft in the "
                  "web UI instead.")
            return 1
        deps = [dep]
    else:
        banner("LISTING DEPOSITIONS  GET /api/deposit/depositions")
        allrec = list_depositions(api)
        if allrec is None:
            return 2
        deps = [d for d in allrec if is_published(d)]
        print("  %d deposition(s): %d published, %d draft(s)"
              % (len(allrec), len(deps), len(allrec) - len(deps)))
        if not deps:
            print("  nothing published yet -- nothing to do.")
            return 0

    if not mutations:
        return run_audit(api, deps, args)
    return run_fix(api, deps, args)


def run_audit(api, deps, args):
    banner("METADATA AUDIT")
    rows, results = audit_rows(api, deps)
    print_audit(rows, results)

    if args.record and results:
        full, meta, res, score = results[0]
        banner("RECORD %s -- CHECK DETAIL" % full.get("id"))
        print("  title        : %s" % trunc(meta.get("title") or full.get("title"), 70))
        print("  doi          : %s" % to_ascii(full.get("doi") or meta.get("doi")))
        print("  state        : %s" % to_ascii(full.get("state")))
        print("  access_right : %s" % to_ascii(meta.get("access_right")))
        print("  license      : %s" % (to_ascii(license_id(meta)) or "(none)"))
        print("  url          : %s" % to_ascii((full.get("links") or {}).get("html")))
        print()
        drows = [[field, "GAP" if res[k][0] else "ok", res[k][1]]
                 for k, _, _label, field in CHECKS]
        print(fmt_table(["FIELD", "RESULT", "DETAIL"], drows, ["l", "c", "c"]))
        print("\n  score %d/%d" % (score, len(CHECKS)))
        banner("RECORD %s -- FULL METADATA (as the deposit API returns it)"
               % full.get("id"))
        print(json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=True))

    if results:
        print("\nRead-only run. To change anything, pick one mutation flag "
              "(see --help) and add --apply.")
    return 0


def run_fix(api, deps, args):
    banner("PLANNING CHANGES  (nothing is written yet)")
    planned, skipped = [], []
    total = len(deps)
    for i, d in enumerate(deps, 1):
        dep_id = d.get("id")
        print("  [%d/%d] GET /api/deposit/depositions/%s" % (i, total, dep_id))
        full = get_deposition(api, dep_id)
        if i < total:
            time.sleep(SLEEP)
        if full is None:
            skipped.append((dep_id, "could not read the deposition"))
            continue
        state = full.get("state")
        if state != "done":
            skipped.append((dep_id, "state=%s, not a clean published record" % state))
            continue
        before = clean_metadata(full.get("metadata"))
        after, notes = plan(before, args)
        for n in notes:
            print("        note: %s" % n)
        if after == before:
            continue
        planned.append({"id": dep_id,
                        "title": before.get("title") or full.get("title") or "(untitled)",
                        "before": before, "after": after})

    if skipped:
        print("\n  skipped:")
        for dep_id, why in skipped:
            print("    [%s] %s" % (dep_id, why))

    if not planned:
        print("\nNo record needs this change. Nothing to do.")
        return 0

    banner("DIFF -- %d record(s) would change" % len(planned))
    for p in planned:
        print("\n[%s] %s" % (p["id"], trunc(p["title"], 60)))
        for line in diff_text(p["id"], p["before"], p["after"]):
            print("  " + to_ascii(line))

    print()
    print(fmt_table(["ID", "TITLE", "CHANGE"],
                    [[str(p["id"]), trunc(p["title"], 40), to_ascii(describe(args))]
                     for p in planned],
                    ["r", "l", "l"]))

    if not args.apply:
        print("\nDRY RUN -- nothing was written.")
        print("Re-run the same command with --apply to be asked for confirmation.")
        return 0

    banner("CONFIRM")
    print("  host         : %s" % BASE)
    print("  records      : %d" % len(planned))
    print("  change       : %s" % to_ascii(describe(args)))
    print("  cycle        : GET (re-check) -> POST /actions/edit -> PUT metadata")
    print("                 -> POST /actions/publish")
    print("  IRREVERSIBLE : republishing a record cannot be undone. The metadata shown")
    print("                 above goes live immediately as a new revision of the")
    print("                 record. Files are NOT touched by this script.")
    print("  on drift     : each record is re-read right before it is written; if it")
    print("                 no longer matches the diff above it is skipped, not")
    print("                 overwritten.")
    print("  on failure   : POST /actions/discard rolls that record back.")
    if not sys.stdin.isatty():
        print("\n  stdin is not a terminal -- refusing to apply without an "
              "interactive confirmation.")
        return 3
    try:
        answer = input("\n  type APPLY to proceed (anything else aborts): ")
    except (EOFError, KeyboardInterrupt):
        print("\n  aborted.")
        return 3
    if answer.strip() != "APPLY":
        print("  aborted -- nothing was written.")
        return 3

    banner("APPLYING")
    results = []
    for i, p in enumerate(planned, 1):
        print("\n  [%d/%d] [%s] %s" % (i, len(planned), p["id"], trunc(p["title"], 50)))
        status, detail = apply_one(api, p["id"], p["before"], p["after"])
        print("      %s: %s" % (status.upper(), detail))
        results.append((p["id"], p["title"], status, detail))
        if i < len(planned):
            time.sleep(SLEEP)

    banner("RESULT")
    print(fmt_table(["ID", "TITLE", "STATUS", "DETAIL"],
                    [[str(i), trunc(t, 34), s.upper(), trunc(d, 46)]
                     for i, t, s, d in results],
                    ["r", "l", "l", "l"]))
    tally = {}
    for _, _, s, _ in results:
        tally[s] = tally.get(s, 0) + 1
    print("\n  %d published, %d skipped, %d rolled back, %d unknown, %d failed"
          % (tally.get("ok", 0), tally.get("skipped", 0), tally.get("rolled-back", 0),
             tally.get("unknown", 0), tally.get("failed", 0)))
    if tally.get("rolled-back"):
        print("  rolled-back records are exactly what they were before.")
    if tally.get("skipped"):
        print("  skipped records were never written to at all.")
    if tally.get("unknown"):
        print("  UNKNOWN records must be checked by hand before you re-run.")
    if tally.get("failed"):
        print("  FAILED records need a look: check the messages above and the "
              "web UI before re-running.")
    if tally.get("failed") or tally.get("unknown"):
        return 4
    if tally.get("ok", 0) != len(results):
        # Nothing is broken, but the change did not fully land -- do not let a
        # calling script read that as success.
        return 5
    return 0


def describe(args):
    if args.set_orcid:
        return "set orcid %s on creators matching '%s'" % (args.set_orcid, args.creator)
    if args.set_affiliation:
        return ("set affiliation '%s' on creators matching '%s'"
                % (args.set_affiliation, args.creator))
    if args.add_keywords:
        return "add keywords: " + ", ".join(args.add_keywords)
    if args.set_venue:
        return "set venue %s / %s" % (args.set_venue[0], args.set_venue[1] or "(no journal)")
    if args.set_license:
        return "set license " + args.set_license
    return "no change"


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\ninterrupted.")
        sys.exit(130)
