r"""Find real disagreements in recent literature, to feed the topic queue.

    python topic_scout.py --out candidates.md
    python topic_scout.py --areas agents,memory --per-area 120
    python topic_scout.py --since 2026-01-01 --out candidates.md

Why this exists
---------------
topics.md sets a specific bar: a STRONG entry is "a live, two-sided, published
disagreement", and every entry in it carries real arXiv ids and measured values
taken from papers someone read. 450 distinct papers are cited there, 213 of
them from 2026.

Topics invented from memory cannot meet that bar. They would assert tensions
that may not exist and cite work that may not say what they claim - the exact
failure cite_check.py exists to catch, moved one layer earlier, where nothing
checks it. A paper drafted from a fabricated premise either dies at the
citation gate, wasting a run, or passes with a premise nobody verified.

So this does not invent anything. Every candidate it emits is built from a
paper fetched live from the arXiv API: real id, real title, real date, real
sentence. The judgement it automates is narrow and honest - finding papers
that SAY they contest something - and it marks its output as untriaged,
because whether a contested claim makes a good paper is still a human call.

What it looks for
-----------------
Papers whose abstracts explicitly push against prior work: "contrary to",
"we find no evidence", "fails to replicate", "overstated", "we challenge".
That phrasing is a strong signal of a live two-sided dispute, which is
precisely what the queue wants and what a survey-shaped topic lacks.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ATOM = "{http://www.w3.org/2005/Atom}"
API = "http://export.arxiv.org/api/query"
MIN_INTERVAL = 3.0          # arXiv asks for one request per three seconds
HERE = os.path.dirname(os.path.abspath(__file__))

# The areas this author writes in, as exact phrases - ONE QUERY EACH.
#
# Not one compound OR query per area, which is what this tried first: arXiv's
# parser does not bind `(abs:"a" OR abs:"b") AND (cat:x OR cat:y)` the way it
# reads. A search for alignment faking came back with medical image tokenizers
# and speaker recognition, 59 of 60 results off-topic. The same phrase alone,
# with the same category filter, returns exactly the right papers. So each
# phrase is its own query - more requests, but results that mean something.
AREAS: dict[str, list[str]] = {
    "self-improvement": ["self-improving", "recursive self-improvement",
                         "self-modifying", "self-refinement",
                         "self-correction"],
    "self-knowledge": ["introspection", "self-knowledge", "confidence calibration",
                       "metacognition", "self-verification"],
    "memory": ["long-term memory", "episodic memory", "memory consolidation",
               "agent memory", "context compression"],
    "reasoning": ["chain-of-thought", "reasoning trace", "faithful reasoning",
                  "latent reasoning", "process reward"],
    "agents": ["autonomous agent", "agentic", "multi-agent system",
               "agent trajectory", "computer use agent"],
    "evaluation": ["benchmark contamination", "LLM-as-a-judge",
                   "evaluation reliability", "data leakage", "inter-rater"],
    "safety": ["alignment faking", "deceptive alignment", "sandbagging",
               "scheming", "jailbreak", "situational awareness"],
    "interpretability": ["mechanistic interpretability", "sparse autoencoder",
                         "circuit analysis", "activation steering",
                         "linear probe"],
    "uncertainty": ["hallucination", "abstention", "selective prediction",
                    "epistemic uncertainty", "semantic entropy"],
    "generalisation": ["out-of-distribution", "distribution shift",
                       "emergent abilities", "length generalization",
                       "compositional generalization"],
    "rl-feedback": ["RLHF", "reward hacking", "reward model",
                    "preference optimization", "specification gaming"],
    "open-world": ["open-world", "novelty detection", "unknown unknowns",
                   "open-set recognition"],
    "scaling": ["scaling law", "inference-time compute", "test-time compute",
                "emergent capability"],
    "tool-use": ["tool use", "function calling", "retrieval-augmented",
                 "code execution agent"],
    "continual": ["catastrophic forgetting", "continual learning",
                  "model editing", "knowledge editing", "unlearning"],
}

# A paper contesting something is not the same as a paper complaining about
# prior work in its opening paragraph. Almost every abstract says existing
# methods "fail to generalize" - that is motivation, not a disagreement, and
# treating it as one filled the first run with boilerplate.
#
# RESULT markers require the paper's OWN finding to push back: a first-person
# claim, or an explicit statement that some accepted belief is wrong. A
# candidate needs at least one of these. The weaker markers below only add
# weight once a RESULT marker has already qualified the paper.
RESULT = [
    r"contrary to (prior|previous|common|popular|widely|conventional)",
    r"(we|our results?|our findings?|these results?) \w{0,12} ?(find|found|show|observe|reveal|suggest)s? "
    r"(that )?no (evidence|significant|consistent|reliable)",
    r"(our|these|the) (results?|findings?|experiments?) (contradict|challenge|question|refute|overturn)",
    r"(challenge|challenges|question|questions|refute|refutes) (the |this |that )?"
    r"(widely|commonly|often|long)?\s?(held |accepted |assumed )?"
    r"(assumption|claim|belief|view|narrative|consensus|hypothesis)",
    r"(is|are|was|were|may be|appear to be) (largely |often |in fact )?"
    r"(overstated|overestimated|exaggerated|illusory|unfounded|premature)",
    r"fail(s|ed)? to replicate",
    r"we (show|demonstrate|find) that [^.]{0,80}(do(es)? not|cannot|fail)",
    r"\b(a|the) (myth|illusion|mirage)\b",
    # \b matters: without it this fires on "Unsurprisingly", which means
    # the opposite of what the marker is looking for.
    r"\b(surprisingly|counterintuitively|contrary to expectation)",
]
RESULT = [re.compile(p, re.I) for p in RESULT]

# Secondary signals. Never sufficient on their own.
CONTEST = [
    (2, r"(unlike|in contrast to) (prior|previous|existing) work"),
    (2, r"(re-?examine|re-?assess|revisit|reconsider)s?\b"),
    (2, r"(weaker|smaller|lower) than (previously|prior|reported|claimed)"),
    (2, r"(disagree|disagreement|inconsisten\w+|conflicting) (results?|findings?|evidence)"),
    (1, r"(however|yet),? (we|our)"),
    (1, r"(limitation|caveat)s? of (existing|current|prior)"),
]
CONTEST = [(w, re.compile(p, re.I)) for w, p in CONTEST]

# Measured values are what makes an entry writable rather than hand-wavy.
NUMBER = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:percent|%|points?|x|times)\b"
    r"|\b(?:kappa|F1|AUC|accuracy|precision|recall)\b[^.]{0,20}\d",
    re.I)


class Paper:
    def __init__(self, el: ET.Element) -> None:
        def text(tag: str) -> str:
            node = el.find(ATOM + tag)
            return " ".join((node.text or "").split()) if node is not None else ""

        raw_id = text("id")
        self.arxiv_id = re.sub(r"^.*/abs/", "", raw_id)
        self.bare = re.sub(r"v\d+$", "", self.arxiv_id)
        self.title = text("title")
        self.abstract = text("summary")
        self.published = text("published")[:10]
        self.updated = text("updated")[:10]
        self.authors = [
            " ".join((a.find(ATOM + "name").text or "").split())
            for a in el.findall(ATOM + "author")
            if a.find(ATOM + "name") is not None
        ]
        self.categories = [
            c.get("term", "") for c in el.findall(ATOM + "category")
        ]
        self.score = 0
        self.contests = False
        self.signals: list[str] = []
        self.quote = ""

    def assess(self) -> None:
        """Score how strongly this paper contests something, and why.

        Sets self.contests only when the paper's own finding pushes back. A
        score built purely from secondary signals means "this abstract has the
        vocabulary of disagreement", which is not the same thing and is what
        made the first pass mostly boilerplate.
        """
        for pat in RESULT:
            m = pat.search(self.abstract)
            if m:
                self.contests = True
                self.score += 3
                self.signals.append(m.group(0).lower()[:60])
        for weight, pat in CONTEST:
            m = pat.search(self.abstract)
            if m:
                self.score += weight
                self.signals.append(m.group(0).lower())
        if NUMBER.search(self.abstract):
            self.score += 1
            self.signals.append("reports measured values")

        # Quote the sentence carrying a RESULT marker - that is the evidence a
        # human needs to triage without re-reading the abstract.
        for sentence in re.split(r"(?<=[.!?])\s+", self.abstract):
            for pat in RESULT:
                if pat.search(sentence):
                    self.quote = sentence.strip()
                    return
        if self.signals:
            self.quote = self.abstract.split(". ")[0].strip()


def area_phrases(area: str) -> list[str]:
    """An area's phrases, lowercased, for the local relevance check."""
    return [p.lower() for p in AREAS[area]]


def on_topic(p: "Paper", phrases: list[str]) -> bool:
    """Does the paper actually discuss this area?

    arXiv's query parser does not bind AND/OR the way the query reads, so a
    search for alignment faking returned, among other things, a study of
    inter-rater reliability on a Turkish narrative corpus. Checking the
    returned text against the phrases we asked for costs nothing and keeps
    that out of the queue - where, at twenty a day, it would accumulate fast.
    """
    hay = (p.title + " " + p.abstract).lower()
    return any(phrase in hay for phrase in phrases)


_last_call = [0.0]


def fetch(phrase: str, per_area: int, since: str | None, start: int = 0) -> list[Paper]:
    """One arXiv search, politely paced. Returns [] rather than raising."""
    wait = MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)

    params = {
        "search_query":
            'all:"%s" AND (cat:cs.AI OR cat:cs.LG OR cat:cs.CL)' % phrase,
        "start": start,
        "max_results": per_area,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    # requests, not urllib: export.arxiv.org answers urllib with HTTP 406
    # regardless of User-Agent or Accept, and 200 for the same query here.
    try:
        resp = requests.get(API, params=params, timeout=60,
                            headers={"User-Agent": "topic_scout/1.0"})
        resp.raise_for_status()
        body = resp.content
    except Exception as exc:
        print("  ! arXiv query failed: %s" % exc, file=sys.stderr)
        return []
    finally:
        _last_call[0] = time.time()

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        print("  ! unparseable response: %s" % exc, file=sys.stderr)
        return []

    out = []
    for entry in root.findall(ATOM + "entry"):
        p = Paper(entry)
        if not p.arxiv_id or not p.title:
            continue
        if since and p.published < since:
            continue
        out.append(p)
    return out


def already_known(path: str) -> set[str]:
    """Every arXiv id topics.md already cites, versionless.

    A candidate built on a paper the queue has already used is not new, and
    the whole request here was for topics nobody has touched.
    """
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    ids = re.findall(r"arXiv:\s*(\d{4}\.\d{4,5})", text, re.I)
    ids += re.findall(r"10\.48550/arXiv\.(\d{4}\.\d{4,5})", text, re.I)
    return {re.sub(r"v\d+$", "", i) for i in ids}


def render(area: str, p: Paper) -> str:
    """One candidate, in the queue's shape but explicitly untriaged."""
    authors = ", ".join(p.authors[:3]) + (" et al." if len(p.authors) > 3 else "")
    lines = [
        "- [ ] CANDIDATE (%s) - %s" % (area, p.title),
        "      Source: arXiv:%s (%s) %s" % (p.bare, p.published, authors),
        "      doi:10.48550/arXiv.%s" % p.bare,
        "      Signal (%d): %s" % (p.score, "; ".join(dict.fromkeys(p.signals))),
    ]
    if p.quote:
        wrapped = []
        line = "      Contests: "
        for word in p.quote.split():
            if len(line) + len(word) + 1 > 88:
                wrapped.append(line)
                line = "                "
            line += word + " "
        wrapped.append(line.rstrip())
        lines += wrapped
    lines.append("      NOT TRIAGED. Before queueing: confirm the other side of this")
    lines.append("      disagreement exists and is citable, and that no published record")
    lines.append("      already covers it.")
    return "\n".join(lines)


SCOUT_HEADING = "### Scouted (auto, untriaged) - consumed only after the curated entries"


def pool_size(path: str) -> int:
    """Untriaged candidates currently sitting in ## Queue."""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        return sum(1 for line in fh if line.startswith("- [ ] CANDIDATE"))


def append_to_topics(path: str, rendered: list[str], max_pool: int) -> int:
    """Add candidates to the END of ## Queue, and report how many landed.

    Position is the whole design. The curated entries above were triaged by
    hand and ordered deliberately; these were found by a regex. Appending
    after them means the pipeline exhausts human judgement before it touches
    machine judgement, and the queue stops running dry either way.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)

    # The Queue ends where the next top-level heading begins.
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Queue")
    except StopIteration:
        print("no '## Queue' heading in %s; not appending" % path, file=sys.stderr)
        return 0
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))

    pool = sum(1 for l in lines[start:end]
               if l.startswith("- [ ] CANDIDATE"))
    if pool >= max_pool:
        print("scouted pool already holds %d untriaged candidates (max %d); "
              "nothing appended." % (pool, max_pool))
        return 0

    room = max_pool - pool
    rendered = rendered[:room]
    if not rendered:
        return 0

    block: list[str] = []
    if SCOUT_HEADING not in raw:
        block += ["", SCOUT_HEADING, "",
                  "Found by topic_scout.py from live arXiv metadata and appended here",
                  "automatically. Every id, title, date and quoted sentence came from the",
                  "API. They sit at the end of the Queue on purpose: the curated entries",
                  "above are consumed first, and these only when those run out.",
                  ""]
    for entry in rendered:
        block += entry.split("\n") + [""]

    lines[end:end] = block
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(nl.join(lines))
    return len(rendered)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--areas", default="",
                    help="comma-separated subset of: " + ", ".join(AREAS))
    ap.add_argument("--per-area", type=int, default=100,
                    help="papers to pull per area before filtering (default 100)")
    ap.add_argument("--pages", type=int, default=1,
                    help="how many pages of --per-area to pull per area "
                         "(default 1). Raise this to sweep deeper.")
    ap.add_argument("--since", default=None,
                    help="ignore papers published before this date, YYYY-MM-DD")
    ap.add_argument("--min-score", type=int, default=3,
                    help="minimum contest score to emit (default 3)")
    ap.add_argument("--topics", default=os.path.join(HERE, "topics.md"),
                    help="topics.md, read to skip papers already cited")
    ap.add_argument("--out", default=None, help="write candidates here")
    ap.add_argument("--limit", type=int, default=0,
                    help="emit at most this many candidates, best first "
                         "(0 = no cap)")
    ap.add_argument("--append", action="store_true",
                    help="append the candidates to the end of ## Queue in "
                         "--topics, so the daily run never runs dry")
    ap.add_argument("--max-pool", type=int, default=200,
                    help="stop appending once this many untriaged candidates "
                         "are already queued (default 200)")
    args = ap.parse_args(argv)

    chosen = [a.strip() for a in args.areas.split(",") if a.strip()] or list(AREAS)
    unknown = [a for a in chosen if a not in AREAS]
    if unknown:
        print("unknown area(s): %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    # Ask whether there is room BEFORE querying. A full sweep is ~75 requests
    # paced three seconds apart; doing that only to discard the results is
    # four wasted minutes and four wasted minutes of arXiv's patience, on
    # every run of every day once the pool is full.
    if args.append:
        pool = pool_size(args.topics)
        if pool >= args.max_pool:
            print("scouted pool already holds %d untriaged candidates "
                  "(max %d). Nothing to do." % (pool, args.max_pool))
            return 0
        print("scouted pool holds %d of %d; room for %d more."
              % (pool, args.max_pool, args.max_pool - pool))

    known = already_known(args.topics)
    print("topics.md already cites %d arXiv papers; those will be skipped."
          % len(known))

    seen: set[str] = set()
    found: list[tuple[str, Paper]] = []
    fetched = 0

    for area in chosen:
        papers: list[Paper] = []
        for phrase in AREAS[area]:
            for page in range(args.pages):
                batch = fetch(phrase, args.per_area, args.since,
                              start=page * args.per_area)
                papers.extend(batch)
                # A short page means the result set is exhausted; asking for
                # the next burns three seconds against arXiv's rate limit.
                if len(batch) < args.per_area:
                    break
        fetched += len(papers)
        phrases = area_phrases(area)
        kept = off = 0
        for p in papers:
            if p.bare in known or p.bare in seen:
                continue
            if not on_topic(p, phrases):
                off += 1
                continue
            p.assess()
            # A secondary signal alone is vocabulary, not a disagreement.
            if not p.contests or p.score < args.min_score:
                continue
            seen.add(p.bare)
            found.append((area, p))
            kept += 1
        print("  %-18s %3d fetched, %3d off-topic, %3d contest something"
              % (area, len(papers), off, kept))

    found.sort(key=lambda ap_: (-ap_[1].score, ap_[1].published), reverse=False)
    found.sort(key=lambda ap_: ap_[1].score, reverse=True)

    print("")
    print("%d papers fetched, %d candidates at score >= %d."
          % (fetched, len(found), args.min_score))
    if not found:
        print("Nothing met the bar. Lower --min-score or widen --areas.")
        return 1

    if args.limit:
        found = found[:args.limit]
        print("keeping the best %d." % len(found))

    if args.append:
        rendered = [render(area, p) for area, p in found]
        n = append_to_topics(args.topics, rendered, args.max_pool)
        print("appended %d candidate(s) to the end of ## Queue in %s"
              % (n, args.topics))
        return 0 if n else 1

    stamp = _dt.date.today().isoformat()
    body = ["## Scouted candidates (%s)" % stamp,
            "",
            "Found by topic_scout.py from live arXiv metadata: every id, title, date",
            "and quoted sentence below came from the API, not from recall. Papers",
            "already cited in this file were skipped.",
            "",
            "These are NOT queue entries. Each still needs the triage the verdict",
            "vocabulary describes - in particular, whether a second side exists. A",
            "paper contesting something proves one side and implies the other; it does",
            "not prove the other is citable.",
            ""]
    for area, p in found:
        body.append(render(area, p))
        body.append("")

    text = "\n".join(body)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("written to %s" % args.out)
    else:
        print("")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
