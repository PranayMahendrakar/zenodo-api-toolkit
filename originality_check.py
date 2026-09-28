r"""Check a draft for copied text before it can become a permanent record.

    python originality_check.py drafts/paper.md
    python originality_check.py drafts/paper.md --verbose
    python originality_check.py --baseline          # every draft against the rest

cite_check.py proves each citation is real. Nothing checked that the text
itself was not lifted from those citations - or recycled from the author's own
earlier papers, which at two a day on neighbouring themes is the likelier
failure. This does both, against the two sources a copy would most plausibly
come from:

  * the abstract of every cited arXiv paper, fetched from DataCite (the DOI
    registry, which serves arXiv abstracts without arXiv's rate limit);
  * the full text of every other draft in drafts/ - the author's own corpus.

What it measures, on the paper's own prose only (front matter, References,
tables, figures, code and anything inside quotation marks are excluded, since
an attributed quote is not plagiarism):

  * the longest run of consecutive words shared with any one source;
  * for a cited abstract, how much of that abstract reappears in the draft;
  * for an earlier paper, how much of THIS draft reappears in it.

What it cannot see: the open web, and non-arXiv references, which Crossref
mostly serves without abstracts. Those are counted and reported, never
silently treated as checked. A commercial service (iThenticate) is the only
way to cover the web, and it has no free API.

Exit 0 = original, 2 = copied text found, 3 = could not check enough to say.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DRAFTS = os.path.join(HERE, "drafts")
CACHE = os.path.join(HERE, ".cache", "abstracts.json")

K = 8                      # shingle length in words
# Thresholds, calibrated on the author's existing corpus with --baseline.
MAX_RUN_SOURCE = 20        # consecutive words shared with a cited abstract
MAX_ABSTRACT_REUSE = 0.15  # share of an abstract's 8-grams found in the draft
# 120, not 40: calibrated on the corpus. The author's papers deliberately
# reuse a ~60-word scope disclaimer ("no experiments were run for this
# paper..."), and reusing one's own methods boilerplate is not plagiarism.
# A recycled PARAGRAPH is: that is what this catches.
MAX_RUN_OWN = 120          # consecutive words shared with an earlier paper
MAX_OWN_REUSE = 0.05       # share of the draft's 8-grams found in one earlier paper
MIN_CHECKABLE = 0.5        # fraction of cited arXiv abstracts that must be fetched
MAX_QUOTED = 0.15          # share of prose in quotation marks before it is noted

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# -- text -------------------------------------------------------------------

def prose(md: str) -> str:
    """The paper's own words: no front matter, references, tables, figures,
    code, blockquotes, or quoted passages."""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        md = md[end + 4:] if end != -1 else md
    md = re.split(r"(?m)^##\s+References\b", md)[0]
    md = re.sub(r"(?s)```.*?```", " ", md)
    out = []
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith(("|", "!", ">", "<!--")):
            continue
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    return strip_quotes(text)[0]


# A quotation may wrap across lines - drafts are hard-wrapped at ~80 columns,
# so most quotes of any length do. The first version only matched single-line
# quotes and flagged five properly attributed quotations in one paper as
# copied text. Bounded at 800 characters so one stray quote mark cannot
# swallow half a paper between itself and the next one.
QUOTE = re.compile(r'"[^"]{3,800}?"|“[^”]{3,800}?”')


def strip_quotes(text: str) -> tuple[str, int]:
    """Remove attributed quotations; return the text and the words removed."""
    removed = sum(len(m.group(0).split()) for m in QUOTE.finditer(text))
    return QUOTE.sub(" ", text), removed


def quoted_share(md: str) -> float:
    """How much of the paper's prose is other people's words, in quotes.

    Excluding quotes is right - an attributed quotation is not plagiarism - but
    it must not become a way to hide a paper that is mostly quotation. Reported
    alongside the verdict.
    """
    if md.startswith("---"):
        end = md.find("\n---", 3)
        md = md[end + 4:] if end != -1 else md
    md = re.split(r"(?m)^##\s+References\b", md)[0]
    md = re.sub(r"(?s)```.*?```", " ", md)
    md = "\n".join(l for l in md.split("\n")
                   if not l.strip().startswith(("|", "!", ">", "<!--")))
    total = len(md.split())
    _, removed = strip_quotes(md)
    return removed / total if total else 0.0


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def shingles(tokens: list[str]) -> set[tuple]:
    return {tuple(tokens[i:i + K]) for i in range(len(tokens) - K + 1)}


def longest_run(draft: list[str], source: set[tuple]) -> tuple[int, str]:
    """Longest stretch of the draft whose every 8-gram also occurs in source."""
    best, best_at, cur, start = 0, 0, 0, 0
    for i in range(len(draft) - K + 1):
        if tuple(draft[i:i + K]) in source:
            if cur == 0:
                start = i
            cur += 1
            if cur > best:
                best, best_at = cur, start
        else:
            cur = 0
    if best == 0:
        return 0, ""
    n = best + K - 1
    return n, " ".join(draft[best_at:best_at + n])


# -- sources ----------------------------------------------------------------

def cited_arxiv_ids(md: str) -> list[str]:
    refs = re.split(r"(?m)^##\s+References\b", md)
    body = refs[1] if len(refs) > 1 else ""
    ids = re.findall(r"arXiv[:.]\s*(\d{4}\.\d{4,5})", body)
    return list(dict.fromkeys(re.sub(r"v\d+$", "", i) for i in ids))


def load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, CACHE)


def fetch_abstract(arxiv_id: str, cache: dict) -> str | None:
    """Abstract text, '' if the record has none, None if unreachable."""
    if arxiv_id in cache:
        return cache[arxiv_id]
    url = "https://api.datacite.org/dois/10.48550/arXiv.%s" % arxiv_id
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30,
                             headers={"User-Agent": "originality_check/1.0"})
            if r.status_code == 404:
                cache[arxiv_id] = ""
                return ""
            r.raise_for_status()
            attrs = r.json()["data"]["attributes"]
            text = " ".join(d.get("description", "")
                            for d in attrs.get("descriptions", [])
                            if d.get("descriptionType") == "Abstract")
            cache[arxiv_id] = text
            return text
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(2 * (attempt + 1))
    return None


def is_paper(md: str) -> bool:
    """A paper has YAML front matter with a title.

    drafts/ also holds the section fragments the first paper was assembled
    from (sec1_intro.md and so on). Compared as if they were papers they made
    that paper look 92% recycled - from itself.
    """
    if not md.startswith("---"):
        return False
    end = md.find("\n---", 3)
    head = md[:end] if end != -1 else md
    return re.search(r"(?m)^title:", head) is not None


def own_corpus(exclude: str) -> dict[str, list[str]]:
    out = {}
    if not os.path.isdir(DRAFTS):
        return out
    me = os.path.basename(exclude)
    for f in sorted(os.listdir(DRAFTS)):
        if not f.endswith(".md") or f == me:
            continue
        with open(os.path.join(DRAFTS, f), encoding="utf-8", errors="replace") as fh:
            md = fh.read()
        if is_paper(md):
            out[f] = words(prose(md))
    return out


# -- the check --------------------------------------------------------------

def check(path: str, verbose: bool = False, corpus=None, cache=None) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        md = fh.read()
    draft = words(prose(md))
    dsh = shingles(draft)
    result = {"paper": os.path.basename(path), "words": len(draft),
              "problems": [], "notes": [], "quoted": quoted_share(md)}
    if result["quoted"] > MAX_QUOTED:
        result["notes"].append(
            "%.0f%% of the prose is direct quotation - attributed, so not "
            "plagiarism, but more than a paper should lean on" % (100 * result["quoted"]))
    if len(draft) < 500:
        result["problems"].append("too little prose to check (%d words)" % len(draft))
        return result

    # Cited sources.
    cache = load_cache() if cache is None else cache
    ids = cited_arxiv_ids(md)
    fetched = unreachable = empty = 0
    worst_src = (0, "", "")
    for aid in ids:
        text = fetch_abstract(aid, cache)
        if text is None:
            unreachable += 1
            continue
        if not text:
            empty += 1
            continue
        fetched += 1
        src = words(text)
        ssh = shingles(src)
        if not ssh:
            continue
        run, span = longest_run(draft, ssh)
        reuse = len(ssh & dsh) / len(ssh)
        if run > worst_src[0]:
            worst_src = (run, aid, span)
        if run >= MAX_RUN_SOURCE:
            result["problems"].append(
                "%d consecutive words copied from the abstract of arXiv:%s: \"%s\""
                % (run, aid, span[:160]))
        if reuse >= MAX_ABSTRACT_REUSE:
            result["problems"].append(
                "%.0f%% of the abstract of arXiv:%s reappears in the draft"
                % (100 * reuse, aid))
    result["cited_arxiv"] = len(ids)
    result["abstracts_checked"] = fetched
    result["longest_from_source"] = worst_src[0]
    if ids and (fetched + empty) / len(ids) < MIN_CHECKABLE:
        result["unverifiable"] = True
        result["notes"].append(
            "only %d of %d cited arXiv abstracts could be fetched - not enough "
            "to call this original" % (fetched, len(ids)))

    # The author's own earlier papers.
    corpus = own_corpus(path) if corpus is None else corpus
    worst_own = (0.0, 0, "", "")
    for name, toks in corpus.items():
        if name == os.path.basename(path):
            continue
        osh = shingles(toks)
        if not osh:
            continue
        reuse = len(dsh & osh) / max(len(dsh), 1)
        run, span = longest_run(draft, osh)
        if (reuse, run) > worst_own[:2]:
            worst_own = (reuse, run, name, span)
        if run >= MAX_RUN_OWN or reuse >= MAX_OWN_REUSE:
            result["problems"].append(
                "recycles text from %s: %.1f%% of this draft, longest shared run "
                "%d words: \"%s\"" % (name, 100 * reuse, run, span[:160]))
    result["own_papers_checked"] = len(corpus)
    result["closest_own"] = {"paper": worst_own[2], "reuse": round(worst_own[0], 4),
                             "run": worst_own[1]}
    return result


def report(r: dict) -> int:
    print("originality: %s  (%d words of prose)" % (r["paper"], r["words"]))
    print("  cited arXiv papers   : %d, abstracts compared %d"
          % (r.get("cited_arxiv", 0), r.get("abstracts_checked", 0)))
    print("  longest run shared with a cited abstract: %d words (limit %d)"
          % (r.get("longest_from_source", 0), MAX_RUN_SOURCE))
    c = r.get("closest_own", {})
    print("  own papers compared  : %d; closest %s - %.1f%% shared, longest run %d words"
          % (r.get("own_papers_checked", 0), c.get("paper") or "-",
             100 * c.get("reuse", 0), c.get("run", 0)))
    print("  direct quotation     : %.1f%% of the prose (excluded from the comparison)"
          % (100 * r.get("quoted", 0)))
    print("  not covered          : the open web, and cited works with no public abstract")
    for n in r["notes"]:
        print("  NOTE: " + n)
    for p in r["problems"]:
        print("  FAIL: " + p)
    if r["problems"]:
        return 2
    if r.get("unverifiable"):
        return 3
    print("  PASS: no copied or recycled text found in what could be checked")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("draft", nargs="?")
    ap.add_argument("--baseline", action="store_true",
                    help="check every draft against the rest and summarise")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    cache = load_cache()
    try:
        if args.baseline:
            texts = {f: open(os.path.join(DRAFTS, f), encoding="utf-8",
                             errors="replace").read()
                     for f in sorted(os.listdir(DRAFTS)) if f.endswith(".md")}
            files = [f for f, md in texts.items() if is_paper(md)]
            corpus = {f: words(prose(texts[f])) for f in files}
            rows = []
            for f in files:
                r = check(os.path.join(DRAFTS, f), corpus=corpus, cache=cache)
                rows.append(r)
                c = r.get("closest_own", {})
                print("%-44s src-run %3d  own %5.1f%% run %4d  quoted %4.1f%%  %s%s" % (
                    f[:44], r.get("longest_from_source", 0), 100 * c.get("reuse", 0),
                    c.get("run", 0), 100 * r.get("quoted", 0), (c.get("paper") or "")[:26],
                    "  <-- " + str(len(r["problems"])) + " problem(s)" if r["problems"] else ""))
            return 0
        if not args.draft:
            ap.error("give a draft, or --baseline")
        return report(check(args.draft, args.verbose, cache=cache))
    finally:
        save_cache(cache)


if __name__ == "__main__":
    raise SystemExit(main())
