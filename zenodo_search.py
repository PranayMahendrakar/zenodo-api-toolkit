"""
Literature mining over Zenodo's public record corpus (~3M records).

This tool is STRICTLY READ-ONLY, by construction rather than by good intentions:
the HTTP session it uses refuses to issue anything but GET/HEAD, so there is no
code path -- present or future -- that can create, edit, publish or delete
anything on Zenodo. No --apply/--publish flag exists because nothing here can
write. The only files it ever touches are the export paths you name yourself.

It needs no token at all. If ZENODO_TOKEN happens to be set it is used for one
thing only: guests are capped at 25 results per page, authenticated callers get
100. The token is never printed (only a masked hint, and only if it is long
enough that the hint is not the whole token).

Usage:
    python zenodo_search.py "digital twin manufacturing"
    python zenodo_search.py "graph neural network" --type publication --subtype preprint
    python zenodo_search.py "protein folding" --pages 4 --min-downloads 100 --sort mostrecent
    python zenodo_search.py "llm evaluation" --from 2024-01-01 --to 2025-12-31 --csv hits.csv
    python zenodo_search.py "knowledge graph" --pages 3 --bib refs.bib --json hits.json
    python zenodo_search.py --similar 14040453 --pages 3

    (PowerShell, optional, only raises the page-size cap:  $env:ZENODO_TOKEN="...")

Notes on the live API, verified against zenodo.org:
    - guest page size max 25, authenticated max 100  (HTTP 400 above that)
    - page * size must stay <= 10000 (deep pagination window)
    - type= / subtype= are real query params; they map onto resource_type
    - publication_date:[YYYY-MM-DD TO YYYY-MM-DD] works inside q, and '*' is a
      valid open end, so --from/--to are pushed server-side and the printed
      total stays honest. A client-side re-check catches partial dates.
"""

import argparse
import csv
import datetime
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata

import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org").rstrip("/")
TOKEN = os.environ.get("ZENODO_TOKEN")

GUEST_MAX_SIZE = 25          # zenodo rejects size>25 without a token
AUTH_MAX_SIZE = 100
MAX_WINDOW = 10000           # page * size ceiling
SLEEP_BETWEEN_PAGES = 0.4    # stay well under 60 req/min as a guest

TYPES = ["publication", "dataset", "software", "poster", "presentation"]
SUBTYPES = ["article", "preprint", "conferencepaper", "report", "thesis"]
SORTS = ["mostrecent", "bestmatch"]

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "using", "used", "into",
    "are", "was", "were", "its", "their", "our", "your", "not", "but", "all",
    "can", "via", "under", "over", "between", "among", "based", "toward",
    "towards", "new", "novel", "study", "studies", "analysis", "approach",
    "approaches", "method", "methods", "results", "data", "dataset", "paper",
    "case", "review", "use", "how", "why", "what", "when", "who", "does",
    "more", "most", "some", "such", "than", "then", "they", "them", "have",
    "has", "had", "been", "being", "will", "would", "could", "should", "may",
    "one", "two", "three", "first", "second", "also", "other", "others",
    "within", "without", "about", "after", "before", "during", "through",
}


# ---------------------------------------------------------------- text helpers

# NFKD leaves these alone, so they would otherwise vanish from titles entirely.
# The Zenodo corpus is very multilingual, so it is worth the extra map.
# (codepoint, replacement) pairs -- kept as integers so this source file
# stays 100% ASCII and cannot be mangled by a cp1252 round-trip.
_TRANSLIT_PAIRS = (
    (0x0131, "i"    ),  # dotless i (Turkish)
    (0x0130, "I"    ),  # dotted capital I (Turkish)
    (0x00f8, "o"    ),  # o with stroke
    (0x00d8, "O"    ),  # O with stroke
    (0x0111, "d"    ),  # d with stroke
    (0x0110, "D"    ),  # D with stroke
    (0x00df, "ss"   ),  # sharp s
    (0x00e6, "ae"   ),  # ae ligature
    (0x00c6, "AE"   ),  # AE ligature
    (0x0153, "oe"   ),  # oe ligature
    (0x0152, "OE"   ),  # OE ligature
    (0x0142, "l"    ),  # l with stroke
    (0x0141, "L"    ),  # L with stroke
    (0x00fe, "th"   ),  # thorn
    (0x00de, "Th"   ),  # capital thorn
    (0x00f0, "d"    ),  # eth
    (0x00d0, "D"    ),  # capital eth
    (0x2013, "-"    ),  # en dash
    (0x2014, "-"    ),  # em dash
    (0x2018, "'"    ),  # left single quote
    (0x2019, "'"    ),  # right single quote
    (0x201c, '"'    ),  # left double quote
    (0x201d, '"'    ),  # right double quote
    (0x2026, "..."  ),  # ellipsis
    (0x00a0, " "    ),  # non-breaking space
)

TRANSLIT = {chr(cp): repl for cp, repl in _TRANSLIT_PAIRS}


def ascii_safe(text):
    """Fold to plain ASCII. The Windows console is cp1252 and dies on unicode."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = "".join(TRANSLIT.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def truncate(text, width):
    text = ascii_safe(text)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[:width - 3] + "..."


def norm_date(value):
    """Pad a partial EDTF-ish date ('2024', '2024-05') out to YYYY-MM-DD."""
    if not value:
        return None
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", str(value))
    if not m:
        return None
    return "%s-%s-%s" % (m.group(1), m.group(2) or "01", m.group(3) or "01")


def as_total(value, fallback=0):
    """hits.total is an int on zenodo.org, but upstream InvenioRDM can send
    {"value": N, "relation": "eq"}. Accept either without exploding."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        inner = value.get("value")
        return inner if isinstance(inner, int) else fallback
    return fallback


def fmt_int(value):
    """Thousands-separated, tolerating either hits.total shape, or nothing."""
    if isinstance(value, dict):
        value = as_total(value, None)
    return format(value, ",") if isinstance(value, int) else "?"


def meta_of(record):
    """Always hand back a dict.

    record.get("metadata", {}) is NOT enough: restricted and half-migrated
    records really do come back with "metadata": null, and the default only
    fires when the key is absent, not when it is present-but-null. Same story
    for creators/stats/links, so every accessor below goes through a guard.
    """
    if not isinstance(record, dict):
        return {}
    meta = record.get("metadata")
    return meta if isinstance(meta, dict) else {}


def creators_of(record):
    """List of creator dicts, with null/!dict entries dropped."""
    raw = meta_of(record).get("creators") or []
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def year_of(record):
    d = norm_date(meta_of(record).get("publication_date"))
    return d[:4] if d else ""


def first_author(record):
    creators = creators_of(record)
    if not creators:
        return ""
    return creators[0].get("name") or creators[0].get("family_name") or ""


def last_name(name):
    """'Doe, John' -> Doe   'Jane Q. Roe' -> Roe   'van der Berg, Jan' -> Berg"""
    name = ascii_safe(name)
    if not name:
        return "anon"
    head = name.split(",")[0] if "," in name else name
    words = [w for w in re.findall(r"[A-Za-z]+", head) if len(w) > 1]
    return words[-1] if words else "anon"


def _stats(record):
    stats = record.get("stats") if isinstance(record, dict) else None
    return stats if isinstance(stats, dict) else {}


def downloads_of(record):
    try:
        return int(_stats(record).get("downloads") or 0)
    except (TypeError, ValueError):
        return 0


def views_of(record):
    try:
        return int(_stats(record).get("views") or 0)
    except (TypeError, ValueError):
        return 0


def keywords_of(record):
    """Zenodo puts free keywords in metadata.keywords (list of str) and/or in
    metadata.subjects (list of dicts). Both are often null, and a single
    'keyword' is frequently a whole semicolon/comma separated blob."""
    meta = meta_of(record)
    raw = []
    for k in meta.get("keywords") or []:
        if isinstance(k, str):
            raw.append(k)
    for s in meta.get("subjects") or []:
        if isinstance(s, dict):
            raw.append(s.get("subject") or s.get("title") or "")
        elif isinstance(s, str):
            raw.append(s)
    out = []
    for blob in raw:
        for part in re.split(r"[;,]", blob):
            part = ascii_safe(part).strip(" .").lower()
            if part and len(part) > 2 and part not in out:
                out.append(part)
    return out


# ------------------------------------------------------------------ http layer

class ReadOnlySession(requests.Session):
    """A requests Session that physically cannot mutate anything on Zenodo.

    The read-only promise is enforced here rather than merely by the absence of
    POST/PUT/DELETE calls, so a future edit that adds a write silently is turned
    into a loud RuntimeError instead of a published record.
    """

    def request(self, method, url, *args, **kwargs):
        if str(method).upper() not in ("GET", "HEAD"):
            raise RuntimeError(
                "zenodo_search is read-only; blocked %s %s" % (method, url))
        return super().request(method, url, *args, **kwargs)


def build_session():
    s = ReadOnlySession()
    s.headers.update({"Accept": "application/json",
                      "User-Agent": "zenodo_search.py (read-only literature mining)"})
    if TOKEN:
        s.headers.update({"Authorization": "Bearer " + TOKEN})
    return s


def api_get(session, url, params=None, tries=3):
    """GET with 429 backoff. Returns parsed json or None; never raises upward."""
    for attempt in range(1, tries + 1):
        try:
            r = session.get(url, params=params, timeout=60)
        except requests.RequestException as exc:
            print("  network error: " + ascii_safe(exc)[:160])
            if attempt == tries:
                return None
            time.sleep(2 * attempt)
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                print("  HTTP 200 but body was not JSON: " + ascii_safe(r.text)[:160])
                return None

        if r.status_code == 429:
            # Retry-After is allowed to be an HTTP-date rather than seconds;
            # int() on that used to raise straight out of the backoff handler.
            try:
                wait = int(float(r.headers.get("Retry-After", "")))
            except (TypeError, ValueError):
                wait = 5 * attempt
            wait = max(1, min(wait, 120))
            print("  HTTP 429 rate limited, backing off %ds (attempt %d/%d)"
                  % (wait, attempt, tries))
            time.sleep(wait)
            continue

        print("  HTTP %d for %s" % (r.status_code, ascii_safe(r.url)[:120]))
        print("  " + ascii_safe(r.text)[:300])
        return None
    return None


def effective_size(requested):
    cap = AUTH_MAX_SIZE if TOKEN else GUEST_MAX_SIZE
    size = max(1, min(requested, AUTH_MAX_SIZE))
    if size > cap:
        print("note: page size %d needs a token; zenodo caps guests at %d. "
              "Using %d." % (size, cap, cap))
        size = cap
    return size


# --------------------------------------------------------------- search itself

def build_query(query, date_from, date_to):
    parts = []
    if query:
        parts.append("(" + query + ")")
    if date_from or date_to:
        lo = date_from or "*"
        hi = date_to or "*"
        parts.append("publication_date:[%s TO %s]" % (lo, hi))
    return " AND ".join(parts) if parts else "*"


def search(session, q, size, pages, rtype=None, subtype=None, sort="bestmatch"):
    """Pull `pages` pages. Returns (records, total_in_corpus)."""
    records = []
    total = 0
    seen = set()
    for page in range(1, pages + 1):
        if page * size > MAX_WINDOW:
            print("note: stopping at page %d; zenodo's deep-paging window is "
                  "%d results (page * size)." % (page - 1, MAX_WINDOW))
            break
        params = {"q": q, "size": size, "page": page, "sort": sort}
        if rtype:
            params["type"] = rtype
        if subtype:
            params["subtype"] = subtype
        data = api_get(session, BASE + "/api/records", params)
        if data is None:
            break
        hits = data.get("hits") or {}
        total = as_total(hits.get("total"), total)
        batch = hits.get("hits") or []
        if not isinstance(batch, list):
            batch = []
        for rec in batch:
            if rec.get("id") not in seen:
                seen.add(rec.get("id"))
                records.append(rec)
        if len(batch) < size:
            break
        if page < pages:
            time.sleep(SLEEP_BETWEEN_PAGES)
    return records, total


def apply_filters(records, date_from, date_to, min_downloads):
    """Belt-and-braces client-side pass. Records with an unparseable date are
    kept rather than silently dropped."""
    out = []
    for rec in records:
        if min_downloads and downloads_of(rec) < min_downloads:
            continue
        d = norm_date(meta_of(rec).get("publication_date"))
        if d:
            if date_from and d < date_from:
                continue
            if date_to and d > date_to:
                continue
        out.append(rec)
    return out


# ---------------------------------------------------------------- similar mode

def title_terms(title, limit=8):
    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", ascii_safe(title).lower())
    terms, seen = [], set()
    for w in words:
        if len(w) < 4 or w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        terms.append(w)
        if len(terms) >= limit:
            break
    return terms


def extract_terms(record, max_terms=12):
    """Keywords first (they are curated), then distinctive title words."""
    terms = []
    for k in keywords_of(record):
        if k not in terms:
            terms.append(k)
    for t in title_terms(meta_of(record).get("title", "")):
        if t not in terms and not any(t in k for k in terms):
            terms.append(t)
    return terms[:max_terms]


def terms_to_query(terms):
    parts = []
    for t in terms:
        parts.append('"' + t + '"' if " " in t else t)
    return " OR ".join(parts)


def score_record(record, terms):
    """Weighted overlap: a keyword hit is worth more than a description hit."""
    title = ascii_safe(meta_of(record).get("title", "")).lower()
    kws = " ; ".join(keywords_of(record))
    desc = strip_html(meta_of(record).get("description", ""))
    desc = ascii_safe(desc).lower()[:3000]
    score = 0
    matched = []
    for t in terms:
        hit = 0
        if t in kws:
            hit = 3
        elif t in title:
            hit = 2
        elif t in desc:
            hit = 1
        if hit:
            score += hit
            matched.append(t)
    return score, matched


def run_similar(session, args):
    print("fetching seed record %s ..." % args.similar)
    seed = api_get(session, BASE + "/api/records/" + str(args.similar))
    if seed is None:
        print("could not fetch that record - check the id.")
        return 1

    seed_title = ascii_safe(meta_of(seed).get("title", ""))
    seed_concept = str(seed.get("conceptrecid") or "")
    terms = extract_terms(seed)
    if not terms:
        print("no usable keywords or title terms on that record.")
        return 1

    print("seed  : [%s] %s" % (seed.get("id"), truncate(seed_title, 70)))
    print("doi   : %s" % seed.get("doi"))
    print("terms : %s" % ", ".join(terms))
    print()

    q = build_query(terms_to_query(terms), args.date_from, args.date_to)
    size = effective_size(args.size)
    records, total = search(session, q, size, args.pages,
                            args.type, args.subtype, "bestmatch")

    # drop the seed itself and every other version of the same record
    kept = []
    for rec in records:
        if str(rec.get("id")) == str(args.similar):
            continue
        if seed_concept and str(rec.get("conceptrecid") or "") == seed_concept:
            continue
        kept.append(rec)

    kept = apply_filters(kept, args.date_from, args.date_to, args.min_downloads)
    scored = []
    for rec in kept:
        score, matched = score_record(rec, terms)
        if score > 0:
            rec["_score"] = score
            rec["_matched"] = matched
            scored.append(rec)
    scored.sort(key=lambda r: (-r["_score"], -downloads_of(r)))

    print_table(scored, total, len(records), scored_mode=True)
    if scored:
        print()
        # the table shows the DOI, which for externally-minted DOIs is not the
        # zenodo record id, so print both -- the id is what --similar takes.
        print("shared terms for the top matches:")
        for rec in scored[:5]:
            print("  %-11s %-28s score %3d  %s"
                  % ("[%s]" % rec.get("id"),
                     ascii_safe(rec.get("doi") or "")[:28],
                     rec["_score"], ", ".join(rec["_matched"])))
    export_all(scored, args)
    return 0


# ----------------------------------------------------------------- table + out

def print_table(records, total, fetched, scored_mode=False):
    term_width = shutil.get_terminal_size((120, 25)).columns
    budget = max(90, min(term_width - 1, 160))

    w_doi, w_year, w_dl, w_auth, w_score = 26, 4, 6, 20, 5
    fixed = w_doi + w_year + w_dl + w_auth + (w_score if scored_mode else 0)
    n_cols = 5 + (1 if scored_mode else 0)
    seps = 3 * (n_cols - 1) + 4
    w_title = max(24, budget - fixed - seps)

    cols = [("DOI", w_doi), ("YEAR", w_year), ("DL", w_dl)]
    if scored_mode:
        cols.append(("SCORE", w_score))
    cols += [("TITLE", w_title), ("FIRST AUTHOR", w_auth)]

    rule = "+" + "+".join("-" * (w + 2) for _, w in cols) + "+"
    header = "| " + " | ".join(("%-*s" % (w, h))[:w] for h, w in cols) + " |"

    print("total matching records in corpus : %s" % fmt_int(total))
    print("fetched this run                 : %d" % fetched)
    print("shown after filters              : %d" % len(records))
    print()
    if not records:
        print("(no records matched)")
        return

    print(rule)
    print(header)
    print(rule)
    for rec in records:
        doi = ascii_safe(rec.get("doi") or "")
        cells = ["%-*s" % (w_doi, truncate(doi, w_doi)),
                 "%-*s" % (w_year, year_of(rec)),
                 "%-*d" % (w_dl, downloads_of(rec))]
        if scored_mode:
            cells.append("%-*d" % (w_score, rec.get("_score", 0)))
        title = truncate(meta_of(rec).get("title", ""), w_title)
        cells.append("%-*s" % (w_title, title))
        cells.append("%-*s" % (w_auth, truncate(first_author(rec), w_auth)))
        print("| " + " | ".join(cells) + " |")
    print(rule)


def flatten(rec):
    meta = meta_of(rec)
    rtype = meta.get("resource_type")
    rtype = rtype if isinstance(rtype, dict) else {}
    links = rec.get("links")
    links = links if isinstance(links, dict) else {}
    return {
        "id": rec.get("id"),
        "doi": rec.get("doi"),
        "title": strip_html(meta.get("title", "")),
        "year": year_of(rec),
        "publication_date": meta.get("publication_date"),
        "first_author": first_author(rec),
        "all_authors": "; ".join(c.get("name") or "" for c in creators_of(rec)),
        "type": rtype.get("type"),
        "subtype": rtype.get("subtype"),
        "downloads": downloads_of(rec),
        "views": views_of(rec),
        "keywords": "; ".join(keywords_of(rec)),
        "url": links.get("self_html") or rec.get("doi_url"),
        "score": rec.get("_score"),
    }


def write_csv(records, path):
    rows = [flatten(r) for r in records]
    fields = list(rows[0].keys()) if rows else ["id", "doi", "title"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    print("wrote %d rows to %s" % (len(rows), path))


def write_json(records, path):
    rows = [flatten(r) for r in records]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    print("wrote %d records to %s" % (len(rows), path))


TEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def tex_escape(text):
    text = ascii_safe(text)
    return "".join(TEX_ESCAPES.get(ch, ch) for ch in text)


def cite_key(rec, used):
    """firstauthorlastname + year + first significant title word."""
    meta = meta_of(rec)
    name = re.sub(r"[^a-z]", "", last_name(first_author(rec)).lower()) or "anon"
    year = year_of(rec) or "nodate"
    words = title_terms(meta.get("title", ""), limit=1)
    word = re.sub(r"[^a-z]", "", words[0].lower()) if words else ""
    key = name + year + (word or "untitled")
    # Suffixes must stay [a-z]: chr(ord("a") + 26) is "{", which would inject an
    # unbalanced brace into the .bib and break the entry.
    base, suffix = key, 0
    while key in used:
        n, tag = suffix, ""
        while True:
            tag = chr(ord("a") + (n % 26)) + tag
            n = n // 26 - 1
            if n < 0:
                break
        key = base + tag
        suffix += 1
    used.add(key)
    return key


def write_bib(records, path):
    used = set()
    entries = []
    for rec in records:
        meta = meta_of(rec)
        rtype = meta.get("resource_type")
        rtype = rtype if isinstance(rtype, dict) else {}
        kind = "article" if rtype.get("type") == "publication" else "misc"
        key = cite_key(rec, used)
        authors = [c.get("name") for c in creators_of(rec) if c.get("name")]
        fields = [
            ("author", " and ".join(tex_escape(a) for a in authors) or "Unknown"),
            ("title", tex_escape(strip_html(meta.get("title", "")))),
            ("year", year_of(rec)),
            ("publisher", "Zenodo"),
            ("doi", ascii_safe(rec.get("doi") or "")),
            ("url", ascii_safe(rec.get("doi_url")
                               or (rec.get("links") or {}).get("self_html") or "")),
        ]
        kws = keywords_of(rec)
        if kws:
            fields.append(("keywords", tex_escape(", ".join(kws))))
        if rtype.get("title"):
            fields.append(("note", tex_escape(rtype["title"])))
        body = ",\n".join("  %-10s = {%s}" % (k, v) for k, v in fields if v)
        entries.append("@%s{%s,\n%s\n}" % (kind, key, body))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("% generated by zenodo_search.py -- read-only export\n\n")
        fh.write("\n\n".join(entries) + "\n")
    print("wrote %d BibTeX entries to %s" % (len(entries), path))


def export_all(records, args):
    """Write whichever exports were asked for. A bad path or a file locked by
    Excel is reported as one line, never as a traceback."""
    if not records:
        if args.csv or args.json or args.bib:
            print("nothing to export - no records survived the filters.")
        return
    for path, writer, label in ((args.csv, write_csv, "CSV"),
                                (args.json, write_json, "JSON"),
                                (args.bib, write_bib, "BibTeX")):
        if not path:
            continue
        try:
            writer(records, path)
        except OSError as exc:
            print("could not write %s to %s" % (label, ascii_safe(path)))
            print("  %s: %s" % (type(exc).__name__, ascii_safe(exc)[:200]))


# ------------------------------------------------------------------------ main

def parse_date(value):
    """Strict YYYY-MM-DD. The shape regex alone was not enough: '2024-13-45'
    matched, Zenodo accepted the malformed range without complaint, and the run
    came back with a confident-looking but wrong total."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value or ""):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD, got '%s'" % value)
    try:
        datetime.date(*(int(p) for p in value.split("-")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "'%s' is not a real calendar date (%s)" % (value, exc))
    return value


def build_parser():
    p = argparse.ArgumentParser(
        prog="zenodo_search.py",
        description="Read-only literature mining over Zenodo's public records. "
                    "No token required; this tool never writes to Zenodo.",
        epilog="examples:\n"
               '  python zenodo_search.py "digital twin manufacturing"\n'
               '  python zenodo_search.py "graph neural network" --type publication --subtype preprint\n'
               '  python zenodo_search.py "llm evaluation" --pages 3 --min-downloads 50 --bib refs.bib\n'
               "  python zenodo_search.py --similar 14040453 --pages 3\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", nargs="?",
                   help="free-text query (the Zenodo query DSL is allowed)")
    p.add_argument("--size", type=int, default=25,
                   help="results per page (guest max 25, authenticated max 100)")
    p.add_argument("--pages", type=int, default=1, help="how many pages to pull")
    p.add_argument("--type", choices=TYPES, help="resource_type filter")
    p.add_argument("--subtype", choices=SUBTYPES,
                   help="resource_type subtype filter")
    p.add_argument("--from", dest="date_from", type=parse_date,
                   metavar="YYYY-MM-DD", help="earliest publication_date")
    p.add_argument("--to", dest="date_to", type=parse_date,
                   metavar="YYYY-MM-DD", help="latest publication_date")
    p.add_argument("--sort", choices=SORTS, default="bestmatch",
                   help="result ordering (default: bestmatch)")
    p.add_argument("--min-downloads", type=int, default=0, dest="min_downloads",
                   metavar="N", help="drop records below N downloads")
    p.add_argument("--similar", metavar="RECORD_ID",
                   help="find records related to this one by keyword/title overlap")
    p.add_argument("--csv", metavar="PATH", help="export results to CSV")
    p.add_argument("--json", metavar="PATH", help="export results to JSON")
    p.add_argument("--bib", metavar="PATH", help="export results to BibTeX")
    return p


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.query and not args.similar:
        parser.print_help()
        print()
        print("error: give a query string, or --similar <record_id>.")
        return 2
    if args.pages < 1:
        print("error: --pages must be >= 1")
        return 2
    if args.date_from and args.date_to and args.date_from > args.date_to:
        print("error: --from is later than --to")
        return 2

    print("host : %s" % BASE)
    if TOKEN:
        # Only ever show the ends of a token long enough that the ends are not
        # the whole thing; a short/pasted-wrong token would otherwise be echoed
        # to the console in full.
        shown = ("%s...%s" % (TOKEN[:4], TOKEN[-4:])) if len(TOKEN) >= 16             else "%d chars, hidden" % len(TOKEN)
        print("auth : ZENODO_TOKEN present (%s) - used only to raise the "
              "page-size cap" % shown)
    else:
        print("auth : none (public search needs no token)")
    print("mode : READ-ONLY (GET requests only; nothing is created or changed)")
    print()

    session = build_session()
    if args.similar:
        if args.query:
            print("note: --similar is set, so the query '%s' is ignored."
                  % ascii_safe(args.query)[:60])
        return run_similar(session, args)

    q = build_query(args.query, args.date_from, args.date_to)
    size = effective_size(args.size)
    print("query : %s" % ascii_safe(q))
    if args.type or args.subtype:
        print("filter: type=%s subtype=%s" % (args.type or "-", args.subtype or "-"))
    print("paging: %d page(s) x %d, sort=%s" % (args.pages, size, args.sort))
    print()

    records, total = search(session, q, size, args.pages,
                            args.type, args.subtype, args.sort)
    fetched = len(records)
    records = apply_filters(records, args.date_from, args.date_to,
                            args.min_downloads)
    print_table(records, total, fetched)
    export_all(records, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
