#!/usr/bin/env python3
"""cite_check.py -- verify that every citation in a draft actually exists.

LLM-assisted drafts get retracted for inventing plausible-looking references.
This tool extracts every citation from a draft (.md / .txt / .bib) and checks it
against the live scholarly record:

  * DOIs      -> https://doi.org/<doi>  (Accept: application/vnd.citationstyles.csl+json)
                 falling back to https://api.crossref.org/works/<doi>
  * arXiv ids -> http://export.arxiv.org/api/query?id_list=<id>  (Atom XML)
  * BibTeX    -> entries parsed with a small state machine (no external deps);
                 the doi/eprint field of each entry is checked
  * title-only references -> Crossref bibliographic search, best match reported
                 with a similarity score so a human can eyeball it

The critical check is not "does the DOI resolve" but "does it resolve to the
thing the draft says it is".  A real DOI attached to the wrong title is the most
dangerous failure mode: it looks verified but cites something else entirely.
Every citation whose title is written out in the draft is compared against the
title the API returned, and divergence is flagged MISMATCH.

STATUS values
  OK            identifier resolves and (if a title was written) the title agrees
  NOT-FOUND     identifier does not exist -- likely fabricated
  MISMATCH      identifier exists but points at a different work than claimed
  UNVERIFIABLE  could not be checked (no identifier and no confident Crossref
                match, or a network/API failure).  Not proof of fabrication:
                books, theses and many venues are simply not in Crossref.

Exit codes: 0 clean, 1 NOT-FOUND or MISMATCH present (or UNVERIFIABLE with
--strict), 2 usage/IO error.  Suitable as a gate before a publish step.

Network use is strictly read-only (HTTP GET), rate-limited to ~5 req/s with a
descriptive User-Agent, 429-aware backoff and an in-memory cache so a repeated
identifier is fetched once.  The draft file is opened read-only and is never
modified.

Usage:
  python cite_check.py draft.md
  python cite_check.py refs.bib --verbose
  python cite_check.py draft.md --json > report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
import time
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("cite_check requires the 'requests' package (pip install requests)\n")
    raise SystemExit(2)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

# Crossref's "polite pool" gives a contact address better rate limits. It is
# a courtesy header, not authentication - the lookups work without it, just in
# the anonymous pool. Kept out of the source so a public checkout does not
# publish someone's inbox; set CROSSREF_MAILTO to opt into the polite pool.
MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
USER_AGENT = ("cite_check/1.0 (mailto:%s)" % MAILTO) if MAILTO else "cite_check/1.0"


def _polite(params):
    """Add the Crossref polite-pool contact, when one is configured."""
    if MAILTO:
        params = dict(params, mailto=MAILTO)
    return params


MIN_INTERVAL = 0.2          # seconds between outbound requests (be polite)
MAX_RETRIES = 4
DEFAULT_TIMEOUT = 30
MAX_BACKOFF = 16.0

DOI_API = "https://doi.org/"
CROSSREF_WORK = "https://api.crossref.org/works/"
CROSSREF_SEARCH = "https://api.crossref.org/works"
ARXIV_API = "http://export.arxiv.org/api/query"

OK = "OK"
NOT_FOUND = "NOT-FOUND"
MISMATCH = "MISMATCH"
UNVERIFIABLE = "UNVERIFIABLE"

PROBLEM_STATUSES = (NOT_FOUND, MISMATCH)

# similarity thresholds
DEFAULT_MISMATCH_THRESHOLD = 0.72   # below this, a written title is a MISMATCH
DEFAULT_SEARCH_THRESHOLD = 0.90     # title-only search: at/above this we call it OK

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# --------------------------------------------------------------------------- #
# identifier patterns
# --------------------------------------------------------------------------- #

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>{}\\,;]+", re.I)

_ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?"
_ARXIV_OLD = r"[a-z][a-z-]+(?:\.[A-Za-z]{2})?/\d{7}(?:v\d+)?"
ARXIV_RE = re.compile(
    r"(?:arxiv\s*[:=]\s*|arxiv\.org/(?:abs|pdf)/|arxiv\s+)"
    r"(" + _ARXIV_NEW + r"|" + _ARXIV_OLD + r")",
    re.I,
)
ARXIV_BARE_RE = re.compile(r"^\s*(?:arxiv\s*[:/]\s*)?(" + _ARXIV_NEW + r"|" + _ARXIV_OLD + r")\s*$", re.I)

# a line that starts a new reference in a markdown/plain-text bibliography
MARKER_RE = re.compile(r"^\s*(?:[-*+\u2022]\s+|\[\d{1,3}\]\s*|\(\d{1,3}\)\s*|\d{1,3}[.)]\s+)")
REF_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\d+\.?\s*)?(references|bibliography|works\s+cited|literature\s+cited|citations)\s*:?\s*$",
    re.I,
)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")

MD_LINK_RE = re.compile(r"\[([^\]\n]{6,300})\]\(([^)\s]+)[^)]*\)")
QUOTED_RE = re.compile(
    "\u201c([^\u201d]{8,300})\u201d"           # curly double quotes
    "|\"([^\"\n]{8,300})\""                     # straight double quotes
    "|\u2018([^\u2019]{8,300})\u2019"           # curly single quotes
)
EMPH_RE = re.compile(r"\*\*([^*\n]{8,300})\*\*|\*([^*\n]{8,300})\*|_([^_\n]{8,300})_")
URLISH_RE = re.compile(r"(?:https?://\S+|www\.\S+|doi\s*:\s*\S+|arxiv\s*:\s*\S+)", re.I)
YEAR_PAREN_RE = re.compile(r"\(\s*((?:1[89]|20)\d{2})[a-z]?\s*\)\s*[.:]?\s*")

ABBREVS = {
    "eds", "ed", "vol", "no", "pp", "p", "al", "trans", "rev", "st", "mr", "ms",
    "mrs", "dr", "prof", "jr", "sr", "inc", "univ", "dept", "fig", "eq", "ch",
}


# --------------------------------------------------------------------------- #
# HTTP: GET-only, polite, cached
# --------------------------------------------------------------------------- #

class Fetcher:
    """Rate-limited, retrying, GET-only HTTP helper with an in-memory cache."""

    def __init__(self, timeout=DEFAULT_TIMEOUT, delay=MIN_INTERVAL, verbose=False):
        self.session = requests.Session()
        headers = {"User-Agent": USER_AGENT}
        if MAILTO:
            headers["From"] = MAILTO
        self.session.headers.update(headers)
        self.timeout = timeout
        self.delay = delay
        self.verbose = verbose
        self.cache = {}
        self.requests_made = 0
        self.cache_hits = 0
        # Lookups that exhausted their retries and never got an answer. A
        # citation behind one of these is unchecked, not clean - see the
        # verdict in main().
        self.transport_failures = 0
        self._last_call = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_call = time.monotonic()

    def get(self, url, headers=None, params=None):
        """Return (response, error_string).  Exactly one of them is None."""
        last_err = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                self.requests_made += 1
                if self.verbose:
                    sys.stderr.write("  GET %s\n" % url)
                resp = self.session.get(
                    url, headers=headers, params=params,
                    timeout=self.timeout, allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_err = "%s: %s" % (type(exc).__name__, exc)
                if attempt == MAX_RETRIES - 1:
                    break
                time.sleep(min(2.0 ** attempt, MAX_BACKOFF))
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES - 1:
                    self.transport_failures += 1
                    return resp, None
                wait = min(2.0 ** attempt, MAX_BACKOFF)
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.strip().isdigit():
                    wait = min(float(retry_after.strip()), 60.0)
                if self.verbose:
                    sys.stderr.write("  HTTP %d, backing off %.1fs\n" % (resp.status_code, wait))
                time.sleep(wait)
                continue

            return resp, None

        self.transport_failures += 1
        return None, last_err or "request failed after %d attempts" % MAX_RETRIES


# --------------------------------------------------------------------------- #
# record normalisation
# --------------------------------------------------------------------------- #

def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def _clean_api_title(text):
    text = re.sub(r"<[^>]+>", " ", text or "")     # Crossref sometimes embeds markup
    return re.sub(r"\s+", " ", text).strip()


def record_from_csl(data, source):
    """Normalise a CSL-JSON / Crossref message dict into our record shape."""
    title = _clean_api_title(_first(data.get("title")))
    subtitle = _clean_api_title(_first(data.get("subtitle")))
    if subtitle and subtitle.lower() not in title.lower():
        title = "%s: %s" % (title, subtitle)

    authors = []
    for person in (data.get("author") or []):
        if not isinstance(person, dict):
            continue
        if person.get("literal"):
            authors.append(person["literal"].strip())
        else:
            name = " ".join(x for x in (person.get("given"), person.get("family")) if x)
            if name.strip():
                authors.append(name.strip())

    year = None
    for key in ("issued", "published-print", "published-online", "published", "created"):
        blob = data.get(key) or {}
        if not isinstance(blob, dict):
            continue
        parts = blob.get("date-parts") or []
        if parts and parts[0]:
            try:
                year = int(parts[0][0])
                break
            except (TypeError, ValueError, IndexError):
                continue

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "container": _clean_api_title(_first(data.get("container-title")))
                     or _clean_api_title(data.get("publisher") or ""),
        "doi": (data.get("DOI") or data.get("doi") or "").lower() or None,
        "type": data.get("type") or None,
        "url": data.get("URL") or data.get("url") or None,
        "source": source,
    }


# --------------------------------------------------------------------------- #
# lookups
# --------------------------------------------------------------------------- #

def normalize_doi(raw):
    doi = (raw or "").strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", doi, flags=re.I)
    return strip_trailing_punct(doi)


def strip_trailing_punct(token):
    """Trim punctuation a DOI picked up from surrounding prose/markup."""
    while token:
        last = token[-1]
        if last in ".,;:'\"*_>":
            token = token[:-1]
        elif last in ")]}":
            opener = {")": "(", "]": "[", "}": "{"}[last]
            if token.count(opener) < token.count(last):
                token = token[:-1]
            else:
                break
        else:
            break
    return token


def lookup_doi(fetcher, doi):
    """Return (record | None, error | None).  record None + error None => no such DOI."""
    key = ("doi", doi.lower())
    if key in fetcher.cache:
        fetcher.cache_hits += 1
        return fetcher.cache[key]

    encoded = quote(doi, safe="/-._;()<>+:")
    resp, err = fetcher.get(
        DOI_API + encoded,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    if resp is not None and resp.status_code == 200:
        try:
            result = (record_from_csl(resp.json(), "doi.org"), None)
        except ValueError:
            result = (None, "doi.org returned non-JSON")
    elif resp is not None and resp.status_code in (404, 410):
        result = (None, None)          # authoritative: no such DOI
    else:
        detail = err or ("HTTP %d from doi.org" % resp.status_code if resp is not None else "no response")
        result = (None, detail)

    # fall back to Crossref when doi.org did not give us usable JSON
    if result[0] is None and result[1] is not None:
        resp2, err2 = fetcher.get(
            CROSSREF_WORK + encoded,
            headers={"Accept": "application/json"},
            params=_polite({}),
        )
        if resp2 is not None and resp2.status_code == 200:
            try:
                msg = resp2.json().get("message") or {}
                result = (record_from_csl(msg, "crossref"), None)
            except ValueError:
                result = (None, "crossref returned non-JSON")
        elif resp2 is not None and resp2.status_code == 404:
            result = (None, None)
        else:
            detail = err2 or ("HTTP %d" % resp2.status_code if resp2 is not None else "no response")
            result = (None, "%s; crossref: %s" % (result[1], detail))

    fetcher.cache[key] = result
    return result


def _bare_arxiv_id(arxiv_id):
    """2506.14758v2 -> 2506.14758.

    DataCite mints its DOI against the versionless id; asking for a versioned
    one 404s even though the paper plainly exists.
    """
    return re.sub(r"v\d+$", "", (arxiv_id or "").strip())


def lookup_arxiv(fetcher, arxiv_id):
    """Return (record | None, error | None)."""
    key = ("arxiv", arxiv_id.lower())
    if key in fetcher.cache:
        fetcher.cache_hits += 1
        return fetcher.cache[key]

    resp, err = fetcher.get(ARXIV_API, params={"id_list": arxiv_id, "max_results": 1})
    if resp is None:
        result = (None, err)
    elif resp.status_code == 400:
        result = (None, None)          # arXiv rejects malformed / unknown ids
    elif resp.status_code != 200:
        result = (None, "HTTP %d from export.arxiv.org" % resp.status_code)
    else:
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            result = (None, "could not parse arXiv Atom feed: %s" % exc)
        else:
            entry = root.find("atom:entry", ATOM_NS)
            if entry is None:
                result = (None, None)
            else:
                entry_id = entry.findtext("atom:id", "", ATOM_NS) or ""
                title = _clean_api_title(entry.findtext("atom:title", "", ATOM_NS))
                if "api/errors" in entry_id or title.lower().startswith("error"):
                    result = (None, None)      # arXiv's "not found" sentinel entry
                else:
                    authors = [
                        _clean_api_title(a.findtext("atom:name", "", ATOM_NS))
                        for a in entry.findall("atom:author", ATOM_NS)
                    ]
                    published = entry.findtext("atom:published", "", ATOM_NS) or ""
                    year = int(published[:4]) if published[:4].isdigit() else None
                    doi = entry.findtext("arxiv:doi", None, ATOM_NS)
                    journal = entry.findtext("arxiv:journal_ref", None, ATOM_NS)
                    result = ({
                        "title": title,
                        "authors": [a for a in authors if a],
                        "year": year,
                        "container": _clean_api_title(journal) if journal else "arXiv preprint",
                        "doi": doi.lower().strip() if doi else None,
                        "type": "preprint",
                        "url": entry_id or None,
                        "source": "arxiv",
                    }, None)

    fetcher.cache[key] = result
    return result


def search_crossref(fetcher, title, rows=3):
    """Return (candidate_records, error | None) for a title-only reference."""
    key = ("search", re.sub(r"\s+", " ", title.strip().lower())[:300], rows)
    if key in fetcher.cache:
        fetcher.cache_hits += 1
        return fetcher.cache[key]

    resp, err = fetcher.get(
        CROSSREF_SEARCH,
        headers={"Accept": "application/json"},
        params=_polite({
            "query.bibliographic": title,
            "rows": rows,
            "select": "DOI,title,subtitle,author,issued,container-title,publisher,type,URL",
        }),
    )
    if resp is None:
        result = ([], err)
    elif resp.status_code != 200:
        result = ([], "HTTP %d from api.crossref.org" % resp.status_code)
    else:
        try:
            items = (resp.json().get("message") or {}).get("items") or []
        except ValueError:
            result = ([], "crossref returned non-JSON")
        else:
            result = ([record_from_csl(item, "crossref-search") for item in items], None)

    fetcher.cache[key] = result
    return result


# --------------------------------------------------------------------------- #
# title normalisation and similarity
# --------------------------------------------------------------------------- #

LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\s*\{([^{}]*)\}")
LATEX_BARE_RE = re.compile(r"\\[a-zA-Z]+\s*")
# accents:  \"{o} \'e \^{i} \c{c}  ->  o e i c
LATEX_ACCENT_RE = re.compile(r"\\[`'^\"~=.vuHcdbrk]\s*\{?([A-Za-z])\}?")
# macros that stand for literal text rather than formatting
LATEX_MACROS = [
    (r"\LaTeXe", "LaTeX2e"), (r"\BibTeX", "BibTeX"), (r"\LaTeX", "LaTeX"),
    (r"\TeX", "TeX"), (r"\ldots", "..."), (r"\dots", "..."),
    (r"\textendash", "-"), (r"\textemdash", "--"), (r"\&", "&"),
]
LATEX_MACRO_RES = [(re.compile(re.escape(name) + r"(?![A-Za-z])"), repl)
                   for name, repl in LATEX_MACROS]


def _delatex(text):
    """Turn a LaTeX/BibTeX title fragment into plain text without eating letters."""
    text = LATEX_ACCENT_RE.sub(r"\1", text)
    for pattern, repl in LATEX_MACRO_RES:
        text = pattern.sub(repl, text)
    for _ in range(3):                       # \emph{\textbf{Title}} -> Title
        text = LATEX_CMD_RE.sub(r"\1", text)
    text = LATEX_BARE_RE.sub(" ", text)
    return text.replace("{", "").replace("}", "")


def clean_written_title(text):
    """Tidy a title as it appears in a draft (markdown / BibTeX residue removed)."""
    if not text:
        return ""
    text = _delatex(text.strip())
    text = re.sub(r"\*\*|\*|__|`", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t.,;:")


def norm_title(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"<[^>]+>", " ", text)
    text = _delatex(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_similarity(written, found):
    """0.0-1.0 similarity between a drafted title and an API title."""
    a, b = norm_title(written), norm_title(found)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    # a drafted title that drops the subtitle is not a mismatch
    if len(short) >= 20 and short in long_:
        ratio = max(ratio, 0.95)
    # token overlap absorbs reordering and punctuation noise
    sa, sb = set(a.split()), set(b.split())
    if sa and sb:
        jaccard = len(sa & sb) / float(len(sa | sb))
        ratio = max(ratio, jaccard * 0.95)
    return round(ratio, 3)


# --------------------------------------------------------------------------- #
# title guessing from free-text reference strings
# --------------------------------------------------------------------------- #

def _split_sentences(text):
    """Split on sentence-ish boundaries without breaking on 'J. Smith' initials."""
    out, buf, i, n = [], [], 0, len(text)
    while i < n:
        ch = text[i]
        buf.append(ch)
        if ch == "." and i + 1 < n and text[i + 1] in " \t":
            word = re.split(r"[\s,;(]", "".join(buf).strip())[-1].rstrip(".")
            is_initial = len(word) == 1 and word.isalpha()
            if not is_initial and word.lower() not in ABBREVS:
                out.append("".join(buf).strip())
                buf = []
        i += 1
    if buf:
        out.append("".join(buf).strip())
    return [s for s in (seg.strip() for seg in out) if s]


def _looks_like_authors(segment):
    tokens = [t for t in re.split(r"\s+", segment.strip()) if t]
    if not tokens:
        return True
    if re.search(r"\bet\s+al\b", segment, re.I):
        return True
    initials = sum(1 for t in tokens if re.fullmatch(r"[A-Z]\.?,?", t))
    if initials >= 2 and "," in segment:
        return True
    if len(tokens) <= 3 and "," in segment and initials >= 1:
        return True
    return False


def _looks_like_venue(segment):
    if re.search(r"\b(?:vol\.?|no\.?|pp\.?|pages|volume|issue|edition|press|publisher)\b",
                 segment, re.I):
        return True
    if re.search(r"\b(?:19|20)\d{2}\b", segment) and len(segment.split()) <= 8:
        return True
    if re.fullmatch(r"[\s\d\W]+", segment):
        return True
    return False


def guess_title(text, identifier=None, permissive=True):
    """Best-effort extraction of the title as written in the draft.

    permissive=False restricts us to unambiguous signals (markdown link text,
    quotes, emphasis) -- used for running prose, where guessing would invent
    mismatches that are really just parsing failures.
    """
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text).strip()

    # 1. markdown link whose target is this very identifier
    if identifier:
        ident_norm = identifier.lower()
        for label, url in MD_LINK_RE.findall(flat):
            if ident_norm in url.lower():
                return clean_written_title(label)

    # 2. explicitly quoted title
    m = QUOTED_RE.search(flat)
    if m:
        candidate = clean_written_title(next(g for g in m.groups() if g))
        if len(candidate.split()) >= 2:
            return candidate

    # 3. emphasised title (*Title* / _Title_ / **Title**)
    m = EMPH_RE.search(flat)
    if m:
        candidate = clean_written_title(next(g for g in m.groups() if g))
        if len(candidate.split()) >= 3:
            return candidate

    if not permissive:
        return ""

    # 4. structural heuristics on a reference-list entry
    body = MARKER_RE.sub("", flat, count=1)
    body = URLISH_RE.sub(" ", body)
    body = re.sub(r"\b(?:doi|arxiv|eprint|url|isbn|pmid)\b\s*[:=]?\s*\S*", " ", body, flags=re.I)
    body = re.sub(r"\s+", " ", body).strip(" .,;:")

    # 4a. APA-ish: Author, A. (2020). Title here. Journal, 1(2), 3-14.
    m = YEAR_PAREN_RE.search(body)
    if m:
        segments = _split_sentences(body[m.end():].strip())
        if segments:
            candidate = clean_written_title(segments[0])
            if len(candidate.split()) >= 3:
                return candidate

    # 4b. otherwise take the first sentence-ish chunk that is neither an author
    #     list nor a venue/pagination tail
    segments = _split_sentences(body)
    for idx, segment in enumerate(segments):
        candidate = clean_written_title(segment)
        if len(candidate.split()) < 4:
            continue
        if idx == 0 and len(segments) > 1 and _looks_like_authors(segment):
            continue
        if _looks_like_venue(segment):
            continue
        return candidate

    if len(segments) == 1:
        candidate = clean_written_title(segments[0])
        if len(candidate.split()) >= 4 and not _looks_like_authors(segments[0]):
            return candidate
    return ""


# --------------------------------------------------------------------------- #
# BibTeX parsing (state machine, no external deps)
# --------------------------------------------------------------------------- #

FIELD_NAME_RE = re.compile(r"\s*([A-Za-z][A-Za-z0-9_+:.-]*)\s*=\s*")
ENTRY_START_RE = re.compile(r"@([A-Za-z]+)\s*([{(])")


def _read_bib_value(text, i):
    """Read one BibTeX field value starting at index i; return (value, next_index)."""
    n = len(text)
    parts = []
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        ch = text[i]
        if ch == "{":
            depth, i = 1, i + 1
            start = i
            while i < n:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            parts.append(text[start:i])
            i += 1
        elif ch == '"':
            depth, i = 0, i + 1
            start = i
            while i < n:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                elif c == '"' and depth <= 0:
                    break
                i += 1
            parts.append(text[start:i])
            i += 1
        else:
            start = i
            while i < n and text[i] not in ",}\n":
                i += 1
            parts.append(text[start:i].strip())
        # string concatenation:  title = "A" # var # "B"
        j = i
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j < n and text[j] == "#":
            i = j + 1
            continue
        break
    return "".join(parts), i


def _clean_bib_value(value):
    value = re.sub(r"%.*", "", value)
    value = value.replace("\\&", "&").replace("\\_", "_")
    value = value.replace("\\%", "%").replace("\\#", "#")
    value = re.sub(r"\\(?:emph|textit|textbf|texttt|mkbibquote|enquote)\s*", "", value)
    value = value.replace("~", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_bibtex(text):
    """Yield dicts: {type, key, fields, line, raw}."""
    entries = []
    pos, n = 0, len(text)
    while True:
        m = ENTRY_START_RE.search(text, pos)
        if not m:
            break
        etype = m.group(1).lower()
        opener = m.group(2)
        closer = "}" if opener == "{" else ")"
        i = m.end()
        depth, brace = 1, 0
        while i < n:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if opener == "{":
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
            else:
                if c == "{":
                    brace += 1
                elif c == "}":
                    brace = max(0, brace - 1)
                elif brace == 0 and c == closer:
                    depth -= 1
                    if depth == 0:
                        break
                elif brace == 0 and c == opener:
                    depth += 1
            i += 1
        body = text[m.end():i]
        pos = min(i + 1, n)

        if etype in ("comment", "preamble", "string"):
            continue

        key, _, rest = body.partition(",")
        key = key.strip()
        fields = {}
        j = 0
        while j < len(rest):
            fm = FIELD_NAME_RE.match(rest, j)
            if not fm:
                nxt = rest.find(",", j)
                if nxt == -1:
                    break
                j = nxt + 1
                continue
            name = fm.group(1).lower()
            value, j = _read_bib_value(rest, fm.end())
            fields[name] = _clean_bib_value(value)
            nxt = rest.find(",", j)
            j = nxt + 1 if nxt != -1 else len(rest)

        entries.append({
            "type": etype,
            "key": key,
            "fields": fields,
            "line": text.count("\n", 0, m.start()) + 1,
            "raw": text[m.start():min(i + 1, n)],
        })
    return entries


# --------------------------------------------------------------------------- #
# citation extraction
# --------------------------------------------------------------------------- #

def _make_citation(kind, ident, written_title, line, context, **extra):
    cite = {
        "kind": kind,
        "identifier": ident,
        "title_written": written_title or "",
        "line": line,
        "context": re.sub(r"\s+", " ", context).strip()[:400],
    }
    cite.update(extra)
    return cite


def find_identifiers(text):
    """Return (dois, arxiv_ids) found in a chunk of text, order-preserving."""
    dois, seen = [], set()
    for match in DOI_RE.finditer(text):
        doi = strip_trailing_punct(match.group(0))
        if len(doi) < 8:
            continue
        low = doi.lower()
        if low not in seen:
            seen.add(low)
            dois.append(doi)

    ids, seen_a = [], set()
    for match in ARXIV_RE.finditer(text):
        aid = match.group(1).strip(" .,;:)]}")
        low = aid.lower()
        if low not in seen_a:
            seen_a.add(low)
            ids.append(aid)
    return dois, ids


def split_units(text):
    """Split a markdown/plain draft into reference-sized units.

    Returns a list of (start_line, unit_text, reference_like).
    """
    lines = text.splitlines()
    units = []
    current = None            # [start_line, [lines], reference_like]
    in_ref_section = False

    def flush():
        if current and any(l.strip() for l in current[1]):
            units.append((current[0], "\n".join(current[1]), current[2]))

    for idx, line in enumerate(lines, start=1):
        if REF_HEADING_RE.match(line):
            flush()
            current = None
            in_ref_section = True
            continue
        if HEADING_RE.match(line):
            flush()
            current = None
            in_ref_section = False
            continue
        if not line.strip():
            flush()
            current = None
            continue
        if MARKER_RE.match(line):
            flush()
            current = [idx, [line], True]
            continue
        if current is None:
            current = [idx, [line], in_ref_section]
        else:
            current[1].append(line)
    flush()
    return units


def extract_from_text(text, want_title_only=True):
    citations = []
    for start_line, unit, ref_like in split_units(text):
        dois, arxiv_ids = find_identifiers(unit)
        # One reference, one check. A reference carrying both a DOI and an
        # arXiv id names a single work, and checking it twice bought nothing
        # while doubling the request count. Worse, it made the run only as
        # reliable as its least reliable source: on 2026-09-13 every DOI in a
        # draft resolved through doi.org and the run still failed, because the
        # duplicate arXiv lookups hit a 429. doi.org is the steadier of the
        # two, so when both are present the DOI wins and the arXiv id is
        # skipped. A wrong DOI still fails as NOT-FOUND, so nothing is lost.
        if dois or arxiv_ids:
            for doi in dois:
                citations.append(_make_citation(
                    "doi", normalize_doi(doi),
                    guess_title(unit, identifier=doi, permissive=ref_like),
                    start_line, unit))
            if not dois:
                for aid in arxiv_ids:
                    citations.append(_make_citation(
                        "arxiv", aid,
                        guess_title(unit, identifier=aid, permissive=ref_like),
                        start_line, unit))
        elif want_title_only and ref_like:
            written = guess_title(unit, permissive=True)
            if written and len(written.split()) >= 4:
                citations.append(_make_citation("title", None, written, start_line, unit))
    return citations


def extract_from_bibtex(text, want_title_only=True):
    citations = []
    for entry in parse_bibtex(text):
        fields = entry["fields"]
        written = clean_written_title(fields.get("title", ""))
        line = entry["line"]
        context = entry["raw"]
        meta = {"bib_key": entry["key"], "bib_type": entry["type"]}

        doi = normalize_doi(fields.get("doi", ""))
        if not doi:
            for candidate_field in ("url", "howpublished", "note"):
                m = DOI_RE.search(fields.get(candidate_field, ""))
                if m:
                    doi = normalize_doi(m.group(0))
                    break

        arxiv_id = ""
        eprint = fields.get("eprint", "").strip()
        archive = (fields.get("archiveprefix", "") or fields.get("eprinttype", "")).lower()
        if eprint and (not archive or "arxiv" in archive):
            m = ARXIV_BARE_RE.match(eprint)
            if m:
                arxiv_id = m.group(1)
        if not arxiv_id:
            blob = " ".join(fields.get(f, "")
                            for f in ("url", "note", "howpublished", "journal", "eprint"))
            m = ARXIV_RE.search(blob)
            if m:
                arxiv_id = m.group(1)

        if doi:
            citations.append(_make_citation("doi", doi, written, line, context, **meta))
        if arxiv_id and not doi:          # DOI wins - see extract_from_text
            citations.append(_make_citation("arxiv", arxiv_id, written, line, context, **meta))
        if not doi and not arxiv_id:
            if want_title_only and written and len(written.split()) >= 2:
                citations.append(_make_citation("title", None, written, line, context, **meta))
            else:
                citations.append(_make_citation(
                    "none", None, written, line, context,
                    note="no DOI, no arXiv id and no usable title in this entry", **meta))
    return citations


def dedupe(citations):
    """Collapse repeat citations of the same identifier + written title."""
    seen, out = {}, []
    for cite in citations:
        key = (cite["kind"],
               (cite["identifier"] or "").lower(),
               norm_title(cite["title_written"])[:160])
        if key in seen:
            seen[key]["occurrences"] += 1
            seen[key].setdefault("also_at_lines", []).append(cite["line"])
            continue
        cite["occurrences"] = 1
        seen[key] = cite
        out.append(cite)
    return out


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def _fill(result, record):
    result["title_found"] = record["title"]
    result["authors"] = record["authors"]
    result["year"] = record["year"]
    result["container"] = record["container"]
    result["resolved_doi"] = record["doi"]
    result["source"] = record["source"]


def _judge_title(result, written, threshold):
    if not written:
        result["status"] = OK
        result["note"] = result["note"] or "identifier resolves; no title in draft to compare"
        return
    sim = title_similarity(written, result["title_found"])
    result["similarity"] = sim
    if sim < threshold:
        result["status"] = MISMATCH
        result["note"] = ("identifier resolves but to a DIFFERENT work "
                          "(title similarity %.2f)" % sim)
    else:
        result["status"] = OK


def verify(citation, fetcher, mismatch_threshold, search_threshold, search_rows):
    result = dict(citation)
    result.update({
        "status": UNVERIFIABLE,
        "title_found": "",
        "similarity": None,
        "authors": [],
        "year": None,
        "container": "",
        "resolved_doi": None,
        "source": None,
        "candidates": [],
        "note": citation.get("note", ""),
    })
    written = citation["title_written"]

    if citation["kind"] in ("doi", "arxiv"):
        if citation["kind"] == "doi":
            record, err = lookup_doi(fetcher, citation["identifier"])
            missing_note = "DOI does not resolve (404) -- probably fabricated"
        else:
            record, err = lookup_arxiv(fetcher, citation["identifier"])
            missing_note = "no such arXiv id -- probably fabricated"

            # export.arxiv.org rate-limits hard and cost three runs in
            # September 2026. Every arXiv paper also has a DataCite DOI,
            # 10.48550/arXiv.<id>, which resolves the same work through an
            # entirely separate service - so when the lookup could not REACH
            # arXiv, ask doi.org instead.
            #
            # A second route to the same verification, not a way around it:
            # the title still has to match, and a fabricated id still fails.
            # The condition is `err is not None`, which means transport
            # trouble; a genuine "no such id" comes back as (None, None) and
            # is deliberately left alone rather than given another chance.
            if record is None and err is not None:
                arxiv_err = err
                alt = "10.48550/arXiv.%s" % _bare_arxiv_id(citation["identifier"])
                alt_record, _ = lookup_doi(fetcher, alt)
                if alt_record is not None:
                    record, err = alt_record, None
                    result["note"] = ("arXiv unreachable (%s); verified via "
                                      "DataCite %s" % (arxiv_err, alt))
        if record is None and err is None:
            result["status"] = NOT_FOUND
            result["note"] = missing_note
        elif record is None:
            result["note"] = "lookup failed: %s" % err
        else:
            _fill(result, record)
            _judge_title(result, written, mismatch_threshold)
        return result

    if citation["kind"] == "title":
        candidates, err = search_crossref(fetcher, written, rows=search_rows)
        if err:
            result["note"] = "Crossref search failed: %s" % err
            return result
        scored = sorted(
            ({"record": c, "similarity": title_similarity(written, c["title"])}
             for c in candidates),
            key=lambda x: x["similarity"], reverse=True,
        )
        result["candidates"] = [{
            "title": s["record"]["title"],
            "doi": s["record"]["doi"],
            "year": s["record"]["year"],
            "similarity": s["similarity"],
        } for s in scored]
        if not scored:
            result["note"] = "no Crossref match at all; add a DOI or verify by hand"
            return result
        best = scored[0]
        _fill(result, best["record"])
        result["similarity"] = best["similarity"]
        if best["similarity"] >= search_threshold:
            result["status"] = OK
            result["note"] = "matched by title search (no identifier in draft)"
        else:
            result["status"] = UNVERIFIABLE
            result["note"] = ("no confident Crossref match (best %.2f) -- "
                              "verify by hand or add a DOI" % best["similarity"])
        return result

    result["note"] = result["note"] or "nothing to verify against"
    return result


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def _cell(text, width):
    text = re.sub(r"\s+", " ", str(text if text not in (None, "") else "-")).strip()
    lines = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False)
    return lines or ["-"]


def render_table(rows, headers, widths):
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [sep, "| " + " | ".join(h.ljust(w)[:w] for h, w in zip(headers, widths)) + " |",
           sep.replace("-", "=")]
    for row in rows:
        cells = [_cell(value, w) for value, w in zip(row, widths)]
        height = max(len(c) for c in cells)
        for i in range(height):
            line = [(cell[i] if i < len(cell) else "").ljust(w)
                    for cell, w in zip(cells, widths)]
            out.append("| " + " | ".join(line) + " |")
        out.append(sep)
    return "\n".join(out)


def short_ident(result):
    if result["kind"] == "doi":
        return result["identifier"]
    if result["kind"] == "arxiv":
        return "arXiv:" + result["identifier"]
    if result.get("bib_key"):
        return "(title-only) @" + result["bib_key"]
    return "(title-only)"



def unreached(results):
    """Citations left unverified because a lookup never reached its source.

    Not the same as the fetcher's failed-request count: a request can fail and
    the citation still be verified, because a failed arXiv call falls back to
    DataCite. What matters for the verdict is whether the citation ended up
    checked, not how many attempts it took.
    """
    return [r for r in results
            if r["status"] == UNVERIFIABLE
            and str(r.get("note", "")).startswith("lookup failed:")]


def print_report(results, path, args, fetcher, counts):
    shown = results if args.verbose else [r for r in results if r["status"] != OK]

    total_width = args.width or max(80, min(shutil.get_terminal_size((110, 25)).columns, 160))
    remaining = max(44, total_width - (12 + 5 + 26 + 5 + 19))
    w_written = remaining // 2
    widths = [12, 5, 26, max(22, w_written), max(22, remaining - w_written), 5]
    headers = ["STATUS", "LINE", "IDENTIFIER", "TITLE AS WRITTEN", "TITLE FOUND", "SIM"]

    print("cite_check 1.0  --  %s" % path)
    print("%d citation(s) extracted, %d network call(s), %d cache hit(s)"
          % (len(results), fetcher.requests_made, fetcher.cache_hits))
    print("")

    if shown:
        rows = []
        for r in shown:
            sim = "-" if r["similarity"] is None else ("%.2f" % r["similarity"])
            rows.append([r["status"], str(r["line"]), short_ident(r),
                         r["title_written"], r["title_found"], sim])
        print(render_table(rows, headers, widths))
    else:
        print("No problems found. (use --verbose to list every citation)")
    print("")

    detail = [r for r in results if r["status"] != OK]
    if detail:
        print("DETAILS")
        print("-" * min(total_width, 78))
        for r in detail:
            print("[%s] line %d  %s" % (r["status"], r["line"], short_ident(r)))
            if r["title_written"]:
                print("    as written : %s" % r["title_written"])
            if r["title_found"]:
                who = ", ".join(r["authors"][:3]) + (" et al." if len(r["authors"]) > 3 else "")
                bits = [x for x in (who, str(r["year"]) if r["year"] else "", r["container"]) if x]
                print("    found      : %s" % r["title_found"])
                if bits:
                    print("                 %s" % " | ".join(bits))
                if r.get("resolved_doi"):
                    print("                 doi: %s" % r["resolved_doi"])
            for cand in r.get("candidates", [])[1:]:
                print("    also seen  : (%.2f) %s [%s]"
                      % (cand["similarity"], cand["title"], cand["doi"] or "no doi"))
            if r["note"]:
                print("    note       : %s" % r["note"])
            if r["context"]:
                snippet = r["context"][:200] + ("..." if len(r["context"]) > 200 else "")
                print("    context    : %s" % snippet)
            print("")

    print("SUMMARY  OK=%d  NOT-FOUND=%d  MISMATCH=%d  UNVERIFIABLE=%d  (total %d)"
          % (counts[OK], counts[NOT_FOUND], counts[MISMATCH], counts[UNVERIFIABLE], len(results)))
    if counts[NOT_FOUND] or counts[MISMATCH]:
        print("FAIL: %d citation(s) could not be substantiated as written."
              % (counts[NOT_FOUND] + counts[MISMATCH]))
    elif unreached(results):
        print("FAIL: %d citation(s) could not be reached at any source. Those are"
              % len(unreached(results)))
        print("      unchecked, not clean.")
    elif counts[UNVERIFIABLE]:
        print("PASS with %d unverifiable citation(s) -- check these by hand."
              % counts[UNVERIFIABLE])
    else:
        print("PASS: every citation resolves and matches its title.")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

SELF_ID_KEYS = ("doi", "record_url", "concept_doi", "url")


def blank_self_identifiers(text):
    """Blank the paper's OWN doi/record_url in its YAML front matter.

    publish_paper.py writes `doi:` and `record_url:` back into the front matter
    after a successful publish. Without this, the next run reads the paper's own
    DOI as a citation, compares it against whatever text happens to sit nearby,
    and reports MISMATCH - which blocks re-staging a paper that has already been
    published, exactly when you need it (a corrected version).

    Only front matter is touched, and lines are replaced with a same-length
    blank so every reported line number still points where it did.
    """
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    head, rest = text[:end], text[end:]
    out = []
    for line in head.split("\n"):
        key = line.split(":", 1)[0].strip().lower()
        if key in SELF_ID_KEYS and ":" in line:
            out.append("")          # keeps the line count, drops the identifier
        else:
            out.append(line)
    return "\n".join(out) + rest


def read_draft(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:   # read-only, always
        return blank_self_identifiers(handle.read())


def looks_like_bibtex(path, text):
    lowered = path.lower()
    if lowered.endswith((".bib", ".bibtex")):
        return True
    return len(re.findall(r"^\s*@[A-Za-z]+\s*[{(]", text, re.M)) >= 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cite_check",
        description="Verify that every citation in a draft actually exists "
                    "and matches the title it is given.",
        epilog="Exit code 1 if any citation is NOT-FOUND or MISMATCH, "
               "so this can gate a publish step.",
    )
    parser.add_argument("draft", help="path to a .md, .txt or .bib file (opened read-only)")
    parser.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="show every citation, not just the problems")
    parser.add_argument("--strict", action="store_true",
                        help="also exit 1 when a citation is UNVERIFIABLE")
    parser.add_argument("--no-title-search", action="store_true",
                        help="skip Crossref search for references without an identifier")
    parser.add_argument("--threshold", type=float, default=DEFAULT_MISMATCH_THRESHOLD,
                        metavar="F",
                        help="title similarity below which a resolved identifier is a "
                             "MISMATCH (default %.2f)" % DEFAULT_MISMATCH_THRESHOLD)
    parser.add_argument("--search-threshold", type=float, default=DEFAULT_SEARCH_THRESHOLD,
                        metavar="F",
                        help="similarity at which a Crossref title search counts as "
                             "verified (default %.2f)" % DEFAULT_SEARCH_THRESHOLD)
    parser.add_argument("--rows", type=int, default=3, metavar="N",
                        help="Crossref search results to consider (default 3)")
    parser.add_argument("--delay", type=float, default=MIN_INTERVAL, metavar="S",
                        help="seconds between API calls (default %.1f)" % MIN_INTERVAL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="S",
                        help="per-request timeout in seconds (default %d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--width", type=int, default=0, metavar="N", help="table width")
    parser.add_argument("--debug", action="store_true",
                        help="log each HTTP request to stderr")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        text = read_draft(args.draft)
    except OSError as exc:
        sys.stderr.write("cite_check: cannot read %s: %s\n" % (args.draft, exc))
        return 2

    is_bib = looks_like_bibtex(args.draft, text)
    want_titles = not args.no_title_search
    citations = dedupe(extract_from_bibtex(text, want_titles) if is_bib
                       else extract_from_text(text, want_titles))

    fetcher = Fetcher(timeout=args.timeout, delay=args.delay, verbose=args.debug)
    results = [verify(c, fetcher, args.threshold, args.search_threshold, args.rows)
               for c in citations]

    counts = {OK: 0, NOT_FOUND: 0, MISMATCH: 0, UNVERIFIABLE: 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    if args.json:
        payload = {
            "tool": "cite_check/1.0",
            "file": args.draft,
            "format": "bibtex" if is_bib else "text",
            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "summary": {
                "total": len(results),
                "ok": counts[OK],
                "not_found": counts[NOT_FOUND],
                "mismatch": counts[MISMATCH],
                "unverifiable": counts[UNVERIFIABLE],
                "network_calls": fetcher.requests_made,
                "cache_hits": fetcher.cache_hits,
            },
            "citations": results,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_report(results, args.draft, args, fetcher, counts)

    if counts[NOT_FOUND] or counts[MISMATCH]:
        return 1

    # A lookup that never reached the API has not been checked, and reporting
    # it as a pass is how an upstream outage turns this gate into a no-op.
    # UNVERIFIABLE alone does not fail the run - it is also what a legitimate
    # "no confident Crossref match" looks like - so the signal used here is
    # the fetcher's exhausted-retry count, which only a real outage produces.
    # On 2026-09-12 and 09-13 export.arxiv.org returned 429 for hours and the
    # daily run had no way to tell that apart from a clean bill of health.
    stranded = unreached(results)
    if stranded:
        print("")
        print("%d citation(s) reached no source at all - not arXiv, not DataCite,"
              % len(stranded))
        print("not Crossref. They are unchecked, not clean. Re-run when the")
        print("outage passes, or add each one's DOI.")
        return 1

    if args.strict and counts[UNVERIFIABLE]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ncite_check: interrupted\n")
        raise SystemExit(130)
