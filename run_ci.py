r"""The daily paper run, for CI.

A port of run_daily.ps1 to Python so the scheduler logic lives in one portable
place instead of a PowerShell copy and a bash copy that drift apart. Every
guard that file earned the hard way is reproduced here:

  * a finished day is a no-op - the drafts already record whether today's paper
    is out, so a redundant launch costs 0.1s instead of an hour;
  * a session is judged on how it ENDED, not on what it mentioned, because a
    transcript that discusses a timeout is not a transcript that died of one;
  * a transient failure is retried only when the attempt produced nothing at
    all, since retrying after a draft exists starts a second paper and
    retrying after a DOI exists risks a second permanent record;
  * auth failures are never retried.

    python run_ci.py                # draft, gate, publish (what 05:00 does)
    python run_ci.py --stage-only   # draft and gate, publish nothing
    python run_ci.py --check        # report what it would do, run nothing

--stage-only exists for the first run in a new environment. A DOI cannot be
withdrawn, so the first paper a machine produces should be inspected before it
becomes permanent.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import time

import claude_flags
from md2pdf import fm_get

PROJ = os.path.dirname(os.path.abspath(__file__))
DRAFTS = os.path.join(PROJ, "drafts")
LOG = os.path.join(PROJ, "runner.log")

MAX_ATTEMPTS = 3
RETRY_WAIT = 300          # seconds; these outages are usually brief
TAIL_LINES = 20           # how much of the transcript decides the verdict

# Patterns, not literals. The literal "OAuth access token has expired" once
# failed to match "OAuth session expired and could not be refreshed", and the
# runner blamed the topic queue for an expired login.
TERMINAL_PATTERNS = [
    r"failed to authenticate",
    r"authentication[_ ]error",
    r"oauth.*(expired|refresh)",
    r"please run /login",
    r"invalid api key",
    r"not (logged in|authenticated)",
    r"session expired",
    # The CLI refusing the pinned model. Starts "API Error", so without these
    # it classified as transient and burned three attempts and fifteen
    # minutes retrying something no amount of waiting fixes.
    r"does not support this model",
    r"or newer is required",
]
TRANSIENT_PATTERNS = [
    r"api error",
    r"connection lost",
    r"rate[_ ]limit",
    r"overloaded",
    r"response stopped arriving",
    r"(request|read|connection) timed out",
    r"50[0234] ",
]

PROMPT_PUBLISH = """\
Follow the instructions in DAILY_RUN.md exactly. You are running unattended.
Publish the paper as step 9 describes, using --publish --yes, but only if every
gate passes. Never pass --skip-cite-check or --force-duplicate. If you have real
doubt about the paper, stage it as a draft instead and say why in the log.
Do the work yourself, now, in this session. Do NOT delegate it to a background
agent or a background task, and do NOT end your turn early to report that
something is still running: this is a one-shot non-interactive invocation, so
the process exits when your turn ends and anything left in the background is
killed unfinished. There is nobody to report back to.

Do not rush in the other direction either. You have 150 minutes and a paper
takes 30 to 90 of them. Step 4's literature sweep is most of that and is what
the paper is made of: the two papers written before this instruction existed
came in at 6,200 words on 13 references in under fifteen minutes, against
12,400 words on 45 references before. See the DEPTH and APPARATUS rules in
step 5 - they are floors, not targets, and a thin paper should be staged with
a reason rather than published.

Run scripts with `python`, which is the allowed interpreter. If some command
is refused, that refusal is about the exact command, not about the session:
on 2026-09-24 a run tried `python3`, was refused, concluded that all code
execution was blocked, and skipped cite_check, publish_paper and the figure
for the whole paper. `python cite_check.py ...` would have worked. Try
`python` before concluding anything is unavailable.

Before drafting, check the topic against the author's existing papers: the
title and the "## Abstract" section of every file in drafts/ (grep them - do
not read every paper in full). If this topic's central question is
substantially one the author has already written - the same question in other
words, or a narrower slice of a paper he already has - do not write it. Mark it
`- [!] overlaps drafts/<slug>.md` in topics.md with one line saying why, and
take the next topic. At several papers a day on neighbouring themes, a near
repeat is the likeliest way for this body of work to get weaker, and no gate
downstream can see it: the originality check catches copied words, not a
copied question.

Touch only the paper you are writing: its draft, its figure, daily_log.md
and topics.md. Do not edit any other draft or any of the pipeline's own
files. If you find something wrong outside your paper - a count that looks
off, a record that looks inconsistent - write it plainly into daily_log.md,
and if it bears on whether publishing is safe, do not publish. Declining is
always safe; editing another paper's front matter can make the next run
publish twice.
"""

PROMPT_STAGE = """\
Follow the instructions in DAILY_RUN.md exactly, with ONE deliberate exception:
do NOT publish. Stop after step 8. Do not run publish_paper.py with --publish,
and do not mint a DOI under any circumstances. Leave the finished draft and its
PDF on disk and report the citation-gate output. This is a first run in a new
environment and the output is being inspected before anything becomes permanent.
Never pass --skip-cite-check or --force-duplicate.
Do the work yourself, now, in this session. Do NOT delegate it to a background
agent or a background task, and do NOT end your turn early to report that
something is still running: this is a one-shot non-interactive invocation, so
the process exits when your turn ends and anything left in the background is
killed unfinished. There is nobody to report back to.

Do not rush in the other direction either. You have 150 minutes and a paper
takes 30 to 90 of them. Step 4's literature sweep is most of that and is what
the paper is made of: the two papers written before this instruction existed
came in at 6,200 words on 13 references in under fifteen minutes, against
12,400 words on 45 references before. See the DEPTH and APPARATUS rules in
step 5 - they are floors, not targets, and a thin paper should be staged with
a reason rather than published.

Run scripts with `python`, which is the allowed interpreter. If some command
is refused, that refusal is about the exact command, not about the session:
on 2026-09-24 a run tried `python3`, was refused, concluded that all code
execution was blocked, and skipped cite_check, publish_paper and the figure
for the whole paper. `python cite_check.py ...` would have worked. Try
`python` before concluding anything is unavailable.

Before drafting, check the topic against the author's existing papers: the
title and the "## Abstract" section of every file in drafts/ (grep them - do
not read every paper in full). If this topic's central question is
substantially one the author has already written - the same question in other
words, or a narrower slice of a paper he already has - do not write it. Mark it
`- [!] overlaps drafts/<slug>.md` in topics.md with one line saying why, and
take the next topic. At several papers a day on neighbouring themes, a near
repeat is the likeliest way for this body of work to get weaker, and no gate
downstream can see it: the originality check catches copied words, not a
copied question.

Touch only the paper you are writing: its draft, its figure, daily_log.md
and topics.md. Do not edit any other draft or any of the pipeline's own
files. If you find something wrong outside your paper - a count that looks
off, a record that looks inconsistent - write it plainly into daily_log.md,
and if it bears on whether publishing is safe, do not publish. Declining is
always safe; editing another paper's front matter can make the next run
publish twice.
"""


PROMPT_WRITE = """\
Follow the instructions in DAILY_RUN.md for steps 1 to 8 exactly, then steps 10
and 11. SKIP STEP 9 ENTIRELY: do not run publish_paper.py in any mode. This paper
is being written ahead into a buffer; the runner proves it publishable with
every gate after you finish, and publishes it on a later slot.

Step 11 is not optional here - it is what stops the next session taking the
same topic. Replace the FIRST line of the topic entry you used with exactly:
    - [~] BANKED drafts/<slug>.md (<today's date>)
where <slug>.md is the draft file you wrote, and keep the entry's title as the
next, indented line. Never mark it PUBLISHED: it is not published.

Never pass --skip-cite-check or --force-duplicate.
""" + PROMPT_STAGE[PROMPT_STAGE.index("Do the work yourself"):]


SESSION_RULES = """
Do the work yourself, now, in this session. Do NOT delegate it to a background
agent or a background task, and do NOT end your turn early to report that
something is still running: this is a one-shot non-interactive invocation, so
the process exits when your turn ends and anything left in the background is
killed unfinished. There is nobody to report back to.

Run scripts with `python`, which is the allowed interpreter. If a command is
refused, the refusal is about that exact command, not the session: try
`python` before concluding that code cannot run.
"""

PROMPT_REVIEW = """\
You are the independent reviewer of one paper in this repository:
    drafts/{slug}.md
This is review round {round}. You did not write this paper and you are not here
to polish its prose. You are here to find what would embarrass its author once
it carries a permanent DOI, and to prove each finding against the source before
you report it. After you, nobody reads this paper before Zenodo does.

Do NOT edit drafts/{slug}.md or any other draft. Your only output is the report
file reviews/{slug}.r{round}.md. The runner restores any draft you change.

Read the paper in full first. Then work through three lenses, in this order.

1. FIDELITY - the lens that matters most. The last review of nine papers found
   26 confirmed errors of exactly this kind, all of which had passed every
   automated gate: a 2.9-point gain written as "five points", a similarity
   figure attributed to the wrong pair of models, values listed against the
   wrong systems, a source table's contradicting row left out. For EVERY number
   and every specific claim the paper attributes to a source - in the prose,
   the table and the figure - open the source and check it: the abstract page
   (https://arxiv.org/abs/<id>) and, when the number is not in the abstract,
   the full text (https://arxiv.org/html/<id>, or the PDF). For a reference
   with only a DOI, use doi.org or api.crossref.org. Check that the number is
   right, that it measures what the paper says it measures, that it belongs to
   the system and setting the paper names, and that the paper does not state
   the result more strongly or more generally than the source does. A number
   you could not find in the source is a finding, not a pass.

2. SUBSTANCE. Does the argument follow from the evidence cited? Look for claims
   beyond what any cited result supports, "we show" or "our results" for work
   nobody here ran, counter-evidence the paper cites and then ignores, internal
   contradictions, a table or figure whose values disagree with the prose, and
   an algorithm block that does not state the decision the paper analyses.

3. NOVELTY. Compare the paper's central question with the title and the
   "## Abstract" section of every other file in drafts/ (grep them; do not read
   every paper in full). Is it substantially a question the author has already
   written - the same question in other words, or a narrow slice of an
   existing paper? If so, name that paper.

Then be your own sceptic. For every finding you mean to mark SERIOUS, go back to
the source once more and confirm it, quoting the source's exact words. Drop
anything you cannot substantiate: a wrong finding costs the author a correct
sentence.

SERIOUS - a misreported number or attribution; a claim the source does not
          support; an overclaim that changes what the paper asserts; a value in
          a table or figure that no cited source reports; an internal
          contradiction on a point the argument needs.
MINOR   - anything else worth fixing: wording, emphasis, a missing caveat that
          does not change a conclusion.

PASS    - no SERIOUS finding survives your own check. The paper can publish.
REVISE  - SERIOUS findings exist, and each can be fixed by correcting or
          removing the claims involved without changing the paper's thesis.
REJECT  - the thesis does not survive the literature, the central question
          duplicates one of the author's existing papers, or the evidence is
          substantially fabricated. REJECT holds the paper for the author.
{previous}
Write reviews/{slug}.r{round}.md in this shape:

    # Review round {round}: drafts/{slug}.md
    ## Findings
    1. SERIOUS - <section, table or figure> - The paper says: "<its exact
       words>". The source (<citation>) says: "<its exact words>".
       Fix: <the correction>.
    2. MINOR - ...
    ## Checked
    <one line: how many attributed claims you checked, against how many
     sources>
    VERDICT: PASS

The last line of the file must be exactly one of `VERDICT: PASS`,
`VERDICT: REVISE` or `VERDICT: REJECT`. The runner reads that line and nothing
else, so a report without it counts as no review at all.

Take the time this needs. A review that opens a handful of sources is not a
review; the fidelity lens alone means opening most of the references.
""" + SESSION_RULES

PROMPT_REREVIEW = """
This is a re-review. The previous round's report is reviews/{slug}.r{prev}.md,
and the reviser's response is appended to it under "## Response". First check
every SERIOUS finding from that round: it stands unless it was either corrected
properly or rebutted with the source's own words. Then check each passage the
response says was changed, because a correction can introduce a new error.
Re-auditing the rest of the paper from scratch is not required; report a new
SERIOUS finding elsewhere only if you come across one.
"""

PROMPT_REVISE = """\
You are revising one paper in response to an independent review:
    drafts/{slug}.md
    reviews/{slug}.r{round}.md   (the review; its verdict was REVISE)

Work through every finding in the review, SERIOUS ones first. Before acting on
a finding, check it against the source yourself - reviewers are sometimes
wrong. Open the source and read what it actually says.
  * If the finding is right, correct the paper: fix the number, the
    attribution or the scope, or remove the claim if the source cannot carry
    it. Say the corrected thing in your own words; a source's exact words go in
    quotation marks with the citation.
  * If the finding is wrong, leave the paper as it is and rebut the finding
    with the source's exact words.
Make no change the review did not ask for, beyond what a correction needs to
read properly. Keep the paper's structure, its table, its algorithm block and
its figure. If a corrected number appears in the figure, regenerate the figure
with matplotlib the same way it was made.

Then append to reviews/{slug}.r{round}.md:

    ## Response
    1. FIXED - "<old words>" -> "<new words>"
    2. REBUTTED - the source says: "<its exact words>"
    ...

one line per finding, in the review's order, and run from the repository root:
    python cite_check.py drafts/{slug}.md --delay 3.0
    python originality_check.py drafts/{slug}.md
Fix anything either one reports. The runner re-runs every gate afterwards.

Touch only drafts/{slug}.md, its figure, and reviews/{slug}.r{round}.md. Do not
change the front matter - the runner restores it. Never pass --skip-cite-check
or --force-duplicate, and do not run publish_paper.py in any mode.
""" + SESSION_RULES


def note(msg: str) -> None:
    line = "%s  %s" % (_dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), msg)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def should_retry(verdict: str, minted: bool, drafted: bool) -> bool:
    """Is another attempt worth making?

    The rule that keeps getting this wrong: judge the attempt by what it LEFT
    BEHIND, not by how its transcript read.

      * terminal (auth) - no. Retrying cannot fix a login.
      * a DOI or a draft exists - no. Relaunching would start a SECOND paper
        rather than finish the first, and a DOI cannot be withdrawn.
      * nothing exists - yes, whatever the transcript said. A session that
        ends politely having done nothing classifies as "clean", and clean is
        not the same as done.
    """
    if verdict == "terminal":
        return False
    return not (minted or drafted)


def drafts_md() -> list[str]:
    if not os.path.isdir(DRAFTS):
        return []
    return [os.path.join(DRAFTS, f) for f in os.listdir(DRAFTS) if f.endswith(".md")]


def front_matter(path: str) -> str:
    """The YAML front matter, ending at its closing `---`.

    Bounded by the delimiter rather than by a line count. A fixed 60-line
    window was safe only while the front matter was short: reference lines in
    the body begin `doi:10.48550/arXiv....` at column zero, so a window that
    overshoots the closing `---` can read a citation as the paper's own DOI.
    Adding written:/gated:/deposition: pushes the block longer, which is
    exactly when that would have started happening.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return ""
            out = [first]
            for line in fh:
                out.append(line)
                if line.strip() == "---":
                    break
            return "".join(out)
    except OSError:
        return ""


def field(path: str, name: str) -> str | None:
    """One front-matter scalar, read the way YAML reads it - or None.

    Through md2pdf.fm_get, the parser publish_paper uses, not a pattern of
    this module's own. On 2026-09-25 a session wrote `date: "2026-09-25"` -
    valid YAML - and this module's regexes rejected the quotes, so it counted
    one paper published that day when there were two and sent the next
    session to write and publish a third. Two parsers reading one file is how
    they came to disagree; now there is one. Blank counts as absent.
    """
    return fm_get(front_matter(path), name) or None


def day_of(path: str) -> str:
    """A draft's date: as YYYY-MM-DD, or '' - tolerant of a time suffix."""
    return (field(path, "date") or "")[:10]


def stamp(path: str, key: str, value: str) -> bool:
    """Set one front-matter key in place, adding it before the closing ---.

    Never touches the body. Returns False if the file has no front matter,
    so a caller can refuse to report success on an unrecorded fact.
    """
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return False
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    if not lines or lines[0].strip() != "---":
        return False
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return False
    entry = "%s: %s" % (key, value)
    for i in range(1, end):
        if lines[i].startswith(key + ":"):
            lines[i] = entry
            break
    else:
        lines.insert(end, entry)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(eol.join(lines))
    return True


def written_today(day: str | None = None) -> int:
    """Papers drafted on `day`, whatever has happened to them since."""
    day = day or _dt.date.today().isoformat()
    return sum(1 for f in drafts_md() if field(f, "written") == day)


def ungated_written() -> list[str]:
    """Papers the pipeline wrote that have not yet passed their gates.

    Only drafts carrying `written:` - legacy drafts predate the buffer and
    must never be swept up and re-submitted. One of them is already live on
    Zenodo without a doi: in its front matter.
    """
    out = []
    for f in drafts_md():
        if not field(f, "written"):
            continue
        if field(f, "gated") or field(f, "doi") or field(f, "hold") \
                or field(f, "deposition"):
            continue
        out.append(f)
    out.sort(key=lambda p: (field(p, "written") or "", os.path.basename(p)))
    return out


def ready_papers() -> list[str]:
    """Buffered papers proven publishable, oldest first.

    `gated:` is written only after the offline gates actually ran and passed,
    and `reviewed:` only after an independent review returned PASS - so
    selection never rests on a session's report about its own work. A paper
    on hold, already published, or mid-attempt is not ready.
    """
    out = []
    for f in drafts_md():
        if field(f, "doi") or field(f, "hold") or field(f, "deposition"):
            continue
        if field(f, "gated") and field(f, "reviewed"):
            out.append(f)
    out.sort(key=lambda p: (field(p, "written") or "", os.path.basename(p)))
    return out


def in_flight() -> str | None:
    """A paper whose publish attempt reached Zenodo with the outcome unknown.

    Any date, not today's. `deposition:` is written before the irreversible
    step and cleared only by success, so this is a fact rather than an
    inference from a date. While one exists nothing else may be published:
    minting a second DOI for the same paper cannot be undone.
    """
    for f in drafts_md():
        if field(f, "deposition") and not field(f, "doi"):
            return f
    return None


def published_dois() -> set[str]:

    return {d for d in (field(f, "doi") for f in drafts_md()) if d}


BUFFER_TARGET = 4         # default N for a manual --fill-buffer
MAX_PER_RUN = 2           # papers one writer run may draft - see below
# Two, not three. Nothing a run writes is saved until its final push, and on
# 2026-09-25 a cancelled run showed that step is SKIPPED on cancellation even
# with if: always(). Hitting timeout-minutes is a cancellation, so a run that
# timed out on its third paper would lose the first two with it. Two papers
# is ~200 minutes against a 340-minute cap, and a day has enough idle slots
# to reach DAILY_WRITES regardless.
DAILY_WRITES = 3          # papers to write per day; 2 publish, so the buffer grows
BUFFER_MAX = 40           # runaway guard: ~20 days of cover at 2/day
GATE_TRIES = 3            # gate attempts before a written paper is held
MORNING_HOUR = 5          # IST; nothing publishes before this
PAPER_MINUTES = 100       # longest a paper takes, with margin

# Independent review. Every gate above is mechanical: cite_check proves a
# reference exists, originality_check that the words are not lifted. Neither
# can see a real citation reported wrongly - and the 28 Sep review of nine
# banked papers found 26 such errors that had passed every gate. So a paper is
# not ready until a separate session, with no stake in the draft, has checked
# its claims against their sources and passed it.
REVIEWS = os.path.join(PROJ, "reviews")
REVIEW_MINUTES = 60       # one review round, with margin
REVISE_MINUTES = 60       # one revision plus its re-gate, with margin
MAX_REVIEW_ROUNDS = 3     # review, revise, review, revise, review - then hold
REVIEW_TRIES = 3          # sessions that end without a verdict before a hold
# run_ci's own share of the job's 340-minute cap: setup, the slot wait and the
# final push need the rest. Nothing is started that cannot finish inside it,
# because a job that hits its timeout is cancelled and a cancelled job skips
# the push that saves its work.
RUN_MINUTES = 270
_STARTED = time.monotonic()


def minutes_left() -> float:
    return RUN_MINUTES - (time.monotonic() - _STARTED) / 60

DAILY_TARGET = 2          # papers per day
EVENING_HOUR = 19         # IST; the hour the second slot opens


def todays_published_count(today=None):
    """How many of today's drafts already carry a DOI.
    """
    today = today or _dt.date.today().isoformat()
    return sum(1 for f in drafts_md() if day_of(f) == today and field(f, "doi"))


def target_for(now: _dt.datetime | None = None) -> int:
    """How many papers should exist by this point in the day.

    One after the morning slot, two after the evening slot - NOT simply
    DAILY_TARGET all day. A flat target would make the morning's retry at
    08:00 see "one of two done" and immediately write the evening paper,
    collapsing the spacing between two papers that topics.md builds in on
    purpose.

    Zero before the morning slot. Without that, any run after midnight -
    a 23:00 retry GitHub delayed past 00:00, or a writer cron at 01:00 -
    saw "0 of 1 today" and published the new day's paper in the small hours:
    Zenodo shows 01:24 and 02:11 IST for two "morning" papers.
    """
    now = now or _dt.datetime.now()
    if now.hour < MORNING_HOUR:
        return 0
    return DAILY_TARGET if now.hour >= EVENING_HOUR else 1


def minutes_to_next_publish(now: _dt.datetime | None = None) -> float:
    """Minutes until the next publish slot opens (05:00 or 19:00 IST)."""
    now = now or _dt.datetime.now()
    for h in (MORNING_HOUR, EVENING_HOUR):
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t > now:
            return (t - now).total_seconds() / 60
    t = (now + _dt.timedelta(days=1)).replace(hour=MORNING_HOUR, minute=0,
                                              second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def draft_doi(path: str) -> str | None:
    """The DOI recorded in one draft's front matter, if it has one.
    """
    return field(path, "doi")


def resume_succeeded(rc: int, minted: str | None) -> bool:
    """Did finishing an existing draft actually mint ITS DOI?

    The question deliberately concerns the draft, not the day. "Does today
    have a published paper" is what made this wrong: with --ignore-completed
    the day already has one, so that question answers yes even when the
    publish just failed - reporting success, and naming the wrong DOI.
    """
    return rc == 0 and bool(minted)


def todays_published_doi(today: str | None = None) -> str | None:
    """Today's paper, if it is already out.

    Read from the drafts rather than a stamp file: publish_paper.py writes
    `doi:` into the front matter of the draft it published, and that front
    matter carries the date. The repository already knows.
    """
    today = today or _dt.date.today().isoformat()
    for f in drafts_md():
        if day_of(f) == today:
            doi = field(f, "doi")
            if doi:
                return doi
    return None


def todays_pending_draft(today: str | None = None) -> str | None:
    """Today's draft, written but not published.

    This is what makes a later attempt cheap. If the morning run drafted a
    paper and then died at the publish step - Zenodo down, arXiv rate-limited,
    the session dropped - the expensive part is already on disk. A retry should
    finish that draft, not spend another 90 minutes writing a different paper
    on the next queue topic.
    """
    today = today or _dt.date.today().isoformat()
    for f in drafts_md():
        # A held paper needs a human. A paper the pipeline wrote goes through
        # the buffer - gates, then review - and never through here: resuming
        # it would publish around both. Writers are told to fill in `date:`,
        # so a paper drafted today looks exactly like "today's pending draft".
        if field(f, "hold") or field(f, "written"):
            continue
        if day_of(f) == today and not field(f, "doi"):
            return f
    return None


def publish_existing(draft: str) -> int:
    """Hand an already-written draft to publish_paper.py.

    No Claude session: the paper exists, only the last step is missing.
    publish_paper.py re-runs every gate itself - citations, duplicate title,
    placeholder text - so this is not a shortcut past them.
    """
    note("today's draft is already written but unpublished:")
    note("        %s" % os.path.basename(draft))
    note("        finishing it rather than writing a second paper")
    rel = os.path.join("drafts", os.path.basename(draft))
    env = dict(os.environ, ZENODO_RUN_DATE=_dt.date.today().isoformat())
    proc = subprocess.run([sys.executable, "publish_paper.py", rel, "--publish", "--yes"],
                          cwd=PROJ, env=env)
    return proc.returncode


def gate_report(draft: str, run_date: str) -> tuple[bool, str]:
    """gate(), also returning what the gates printed - streamed as it runs."""
    env = dict(os.environ, ZENODO_RUN_DATE=run_date)
    rel = os.path.join("drafts", os.path.basename(draft))
    proc = subprocess.Popen([sys.executable, "publish_paper.py", rel, "--gate-only"],
                            cwd=PROJ, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    chunks = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        chunks.append(line)
    proc.wait()
    return proc.returncode == 0, "".join(chunks)


def citation_failure(report: str) -> str:
    """The cite_check findings in a gate report that a fix to the paper can
    cure - a reference that resolves to a different title, or not at all -
    as opposed to a source that was merely unreachable. "" if none."""
    lines = [l for l in report.splitlines() if re.match(r"\s*\[(MISMATCH|NOT-FOUND)\]", l)]
    if not lines:
        return ""
    start = report.find("DETAILS")
    end = report.find("SUMMARY", start)
    return report[start:end].strip() if start >= 0 and end > start else "\n".join(lines)


def gate(draft: str, run_date: str) -> bool:
    """Run every DOI-guarding gate against a draft; stamp `gated:` if they pass.

    Delegated to publish_paper.py --gate-only rather than reimplemented, so
    the buffer is gated by exactly the code that publishes - not by a second,
    drifting copy of the same rules. Returns False loudly; a paper that fails
    here simply stays unready and the writer moves to the next one.
    """
    env = dict(os.environ, ZENODO_RUN_DATE=run_date)
    rel = os.path.join("drafts", os.path.basename(draft))
    proc = subprocess.run([sys.executable, "publish_paper.py", rel, "--gate-only"],
                          cwd=PROJ, env=env)
    return proc.returncode == 0


TOPICS = os.path.join(PROJ, "topics.md")


def banked_marker(draft: str) -> str:
    return "- [~] BANKED drafts/%s" % os.path.basename(draft)


def ensure_topic_marked(draft: str, day: str) -> bool:
    """Make sure the topic this draft used is no longer unchecked.

    The writer session is told to mark it in step 11. If it did not, mark the
    first unchecked entry in ## Queue - step 1's own rule for which topic a
    session takes - so the next session cannot draft the same one again.
    """
    try:
        with open(TOPICS, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return False
    if banked_marker(draft) in text:
        return True
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    try:
        q = next(i for i, l in enumerate(lines) if l.strip() == "## Queue")
    except StopIteration:
        return False
    end = next((i for i in range(q + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    for i in range(q, end):
        if lines[i].startswith("- [ ] "):
            title = lines[i][len("- [ ] "):]
            lines[i] = "%s (%s)" % (banked_marker(draft), day)
            lines.insert(i + 1, "      " + title)
            with open(TOPICS, "w", encoding="utf-8", newline="") as fh:
                fh.write(eol.join(lines))
            note("the session did not mark its topic; marked the first unchecked")
            note("        entry BANKED for %s so it is not drafted twice."
                 % os.path.basename(draft))
            return True
    return False


def mark_published(draft: str, doi: str, day: str) -> bool:
    """Flip a BANKED marker to PUBLISHED once the paper is out."""
    try:
        with open(TOPICS, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return False
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    mark = banked_marker(draft)
    for i, l in enumerate(lines):
        if l.startswith(mark):
            lines[i] = "- [x] PUBLISHED %s (%s) - drafts/%s" % (
                doi, day, os.path.basename(draft))
            with open(TOPICS, "w", encoding="utf-8", newline="") as fh:
                fh.write(eol.join(lines))
            return True
    note("note: no BANKED marker for %s in topics.md to flip to PUBLISHED."
         % os.path.basename(draft))
    return False


def regate(draft: str, run_date: str, cli: str | None = None, quiet: bool = True) -> bool:
    """Retry the gates on a paper that is already written.

    The attempt is counted BEFORE the gates run, so a crash mid-gate still
    counts. After GATE_TRIES failures the paper is held with a reason rather
    than retried forever; until then a failure is treated as transient -
    usually arXiv or Crossref unreachable - because nothing written should
    be discarded on one bad morning.

    A citation the gate rejects as a different title, or as not found, is not
    transient and does not cure itself: on 2026-10-03 a cited preprint was
    retitled after the paper was gated, and the paper failed three publish
    slots in a row. Given a CLI, such a failure gets one repair session,
    which updates the reference and re-checks every claim made of it against
    the source as it now stands. A repaired paper has changed, so its review
    is reopened.
    """
    tries = int(field(draft, "gate_tries") or 0) + 1
    stamp(draft, "gate_tries", str(tries))
    name = os.path.basename(draft)
    ok, report = gate_report(draft, run_date)
    if ok:
        note("banked %s (gate attempt %d)" % (name, tries))
        return True
    findings = citation_failure(report)
    repairs = int(field(draft, "citation_repairs") or 0)
    if findings and cli and repairs < CITATION_REPAIRS:
        stamp(draft, "citation_repairs", str(repairs + 1))
        if repair_citations(cli, draft, findings, quiet) and gate_report(draft, run_date)[0]:
            reopen_review(draft)
            note("banked %s after repairing its citations; it goes back for review."
                 % name)
            return True
    if tries >= GATE_TRIES:
        stamp(draft, "hold", "failed its gates %d times, last on %s" % (tries, run_date))
        note("HELD %s after %d failed gate attempts - it needs a human." % (name, tries))
    else:
        note("%s failed its gates (attempt %d of %d); will retry next slot."
             % (name, tries, GATE_TRIES))
    return False


CITATION_REPAIRS = 2      # repair sessions a paper may have for its citations

PROMPT_FIX_CITATIONS = """\
The citation gate rejected a paper in this repository:
    drafts/{slug}.md

Its report:

{findings}

For every reference marked MISMATCH or NOT-FOUND, open the record yourself:
https://arxiv.org/abs/<id> and the full text (https://arxiv.org/html/<id>), or
doi.org / api.crossref.org for a DOI.
  * Same identifier, same authors, new title or metadata - the work was
    revised. Update the reference to the current record, then check EVERY
    claim the paper attributes to that work against the CURRENT version's
    text. Correct a claim the current version states differently; remove a
    claim it no longer supports. Your own words; a source's exact words go in
    quotation marks with the citation.
  * The identifier names a different work from the one meant - find the
    intended work, cite it correctly and check the claims against it. If it
    cannot be found, remove the reference and every claim that rests on it.

Then run, from the repository root, until both pass:
    python cite_check.py drafts/{slug}.md --delay 3.0
    python originality_check.py drafts/{slug}.md

Append to reviews/{slug}.repairs.md what you changed and why, quoting the
source for each corrected claim. Touch only drafts/{slug}.md, its figure if a
plotted value changed, and that file. Do not change the front matter. Never
pass --skip-cite-check or --force-duplicate, and do not run publish_paper.py.
""" + SESSION_RULES


def repair_citations(cli: str, draft: str, findings: str, quiet: bool) -> bool:
    """One session to fix the citations the gate rejected. True if it ran
    and changed the paper."""
    name = os.path.basename(draft)
    slug = os.path.splitext(name)[0]
    note("repairing the citations of %s:" % name)
    for line in findings.splitlines()[:6]:
        note("        %s" % line.strip()[:110])
    saved_fm = front_matter_lines(draft)
    with open(draft, "rb") as fh:
        before = fh.read()
    snap = snapshot()

    def changed() -> bool:
        with open(draft, "rb") as fh:
            return fh.read() != before

    os.makedirs(REVIEWS, exist_ok=True)
    outcome = run_session(cli, PROMPT_FIX_CITATIONS.format(slug=slug, findings=findings),
                          quiet, changed)
    for p in restore(snap, allow=(draft,)):
        note("        the repair changed %s; undone" % os.path.basename(p))
    if saved_fm and put_front_matter(draft, saved_fm):
        note("        the repair changed the front matter; restored it")
    if outcome != "ok":
        note("        the repair session did not complete (%s)." % outcome)
        return False
    return True


def reopen_review(draft: str) -> None:
    """A paper changed after it passed review goes back for one more round."""
    if not field(draft, "reviewed"):
        return
    rounds = int(field(draft, "review_rounds") or 0)
    unstamp(draft, "reviewed")
    stamp(draft, "review_rounds", str(rounds))
    stamp(draft, "review_verdict", "REVISE")
    stamp(draft, "revised_round", str(rounds))
    stamp(draft, "review_tries", "0")


def missed_yesterday(today: str | None = None) -> int:
    """Papers yesterday's two slots should have published and did not.

    Each is made up today, one per publish run, so a failed slot costs a
    delay rather than a paper. Only yesterday counts: a longer outage is not
    turned into a flood of papers on one day.
    """
    today = today or _dt.date.today().isoformat()
    yesterday = (_dt.date.fromisoformat(today) - _dt.timedelta(days=1)).isoformat()
    return max(0, DAILY_TARGET - todays_published_count(yesterday))


PUBLISH_TRIES = 3         # ready papers a slot may try before it gives up


def publish_from_buffer(run_date: str) -> int | None:
    """Publish the oldest ready paper; if it is refused before anything
    reaches Zenodo, send it back to its gates and try the next.

    One refused paper used to fail the slot: on 2026-10-03 and 04 the same
    paper - a cited preprint retitled after it was gated - was retried and
    refused at three publish runs while thirteen ready papers waited behind
    it. Returns 0 published, 1 failed, None if nothing was ready.
    """
    ready = ready_papers()
    if not ready:
        return None
    for paper in ready[:PUBLISH_TRIES]:
        note("publishing from the buffer: %s (written %s, gated %s, reviewed %s)"
             % (os.path.basename(paper), field(paper, "written") or "?",
                field(paper, "gated") or "?", field(paper, "reviewed") or "?"))
        rc = publish_existing(paper)
        minted = draft_doi(paper)
        if resume_succeeded(rc, minted):
            note("OK: published %s - https://doi.org/%s" % (minted, minted))
            mark_published(paper, minted, run_date)
            note("        buffer now holds %d." % len(ready_papers()))
            return 0
        if field(paper, "deposition") or in_flight():
            note("FAILED: %s reached Zenodo and its outcome is unknown (exit %d)."
                 % (os.path.basename(paper), rc))
            note("        Publishing nothing else until it is resolved.")
            return 1
        # Refused before anything reached Zenodo. Its gates passed when it
        # was banked, so something changed since - most often a cited source.
        # Back to its gates, where a rejected citation is repaired, and on to
        # the next paper now.
        unstamp(paper, "gated")
        unstamp(paper, "original")
        stamp(paper, "gate_tries", "0")
        stamp(paper, "publish_refused", run_date)
        note("        %s was refused (exit %d) before reaching Zenodo; it goes"
             % (os.path.basename(paper), rc))
        note("        back to its gates, and the next ready paper is tried.")
    note("FAILED: %d ready paper(s) were refused this slot. The gates above say why."
         % min(len(ready), PUBLISH_TRIES))
    return 1


def classify(output: str) -> str:
    """terminal | transient | clean, judged on the tail only."""
    tail = "\n".join(output.splitlines()[-TAIL_LINES:])
    for pat in TERMINAL_PATTERNS:
        if re.search(pat, tail, re.I):
            return "terminal"
    for pat in TRANSIENT_PATTERNS:
        if re.search(pat, tail, re.I):
            return "transient"
    return "clean"


def find_cli() -> str | None:
    """PATH first, then the per-user install the Windows installer uses.

    On a CI runner `claude` is on PATH; on a workstation it is usually only in
    ~/.local/bin, which is why looking at PATH alone reported NOT FOUND there.
    """
    explicit = os.environ.get("CLAUDE_CLI")
    if explicit and os.path.exists(explicit):
        return explicit

    found = shutil.which("claude")
    if found:
        return found

    home = os.path.expanduser("~")
    for name in ("claude.exe", "claude"):
        cand = os.path.join(home, ".local", "bin", name)
        if os.path.exists(cand):
            return cand
    return None


def write_one(cli: str, prompt: str, quiet: bool = False):
    """Run ONE drafting session, with the retry rules, and report what it left.

    Extracted so the buffer writer can run it repeatedly without a second copy
    of the judging logic. Every rule here was earned the hard way and there
    must only ever be one of it:

      * an auth failure is terminal and never retried;
      * an attempt is judged by what it LEFT BEHIND, not by how its transcript
        read - a session can end politely having done nothing;
      * a retry never runs on top of a draft or a DOI, because that starts a
        second paper rather than finishing the first.

    Returns (failed, minted, drafted).
    """
    flags = claude_flags.flags()
    dois_before = published_dois()
    drafts_before = len(drafts_md())



    failed = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            note("retrying: attempt %d of %d" % (attempt, MAX_ATTEMPTS))

        # Stream as it arrives; a run this long should not be a black box.
        chunks: list[str] = []
        proc = subprocess.Popen([cli, "-p", prompt] + flags, cwd=PROJ,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            # On a public repo the workflow log is world-readable, and this
            # stream is the paper itself. Capture it either way - runner.log
            # is committed to the private repo - but only echo it when stdout
            # is somewhere the draft is allowed to be.
            if not quiet:
                sys.stdout.write(line)
                sys.stdout.flush()
            chunks.append(line)
        proc.wait()
        out = "".join(chunks)

        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(out)

        verdict = classify(out)
        if verdict == "terminal":
            note("FAILED: authentication problem - not retrying, this needs you.")
            note("        Re-run `claude setup-token` and update the secret.")
            failed = True
            break

        # What the attempt LEFT BEHIND decides, not how its transcript read.
        # "clean" only means the session ended with no error string in it, and
        # a session can end perfectly politely having done nothing: on 22 Sep
        # 2026 one handed the job to a background task, announced it would
        # report back, and exited - killing the task. That transcript is
        # "clean", and this loop used to break on it and call the whole run a
        # failure with most of the budget unspent.
        minted = published_dois() - dois_before
        drafted = len(drafts_md()) > drafts_before

        if not should_retry(verdict, bool(minted), drafted):
            # Real work exists. Never relaunch on top of it - a second run
            # would start a second paper rather than finish this one.
            if verdict != "clean":
                note("transient API problem, but the attempt left work behind")
                note("        (%s), so not retrying."
                     % ("a DOI was minted" if minted else "a draft was written"))
            break

        note("transient API problem" if verdict != "clean" else
             "the session ended cleanly but produced nothing - it did not work")
        if attempt == MAX_ATTEMPTS:
            note("FAILED: %d attempts produced no paper." % MAX_ATTEMPTS)
            failed = True
            break
        note("        nothing was produced, so waiting %ds and trying again" % RETRY_WAIT)
        time.sleep(RETRY_WAIT)

    return failed, published_dois() - dois_before, len(drafts_md()) > drafts_before


# -- independent review ----------------------------------------------------

VERDICT_RE = re.compile(r"(?m)^[ \t>*_`]*VERDICT:\s*\**\s*(PASS|REVISE|REJECT)\b")


def unstamp(path: str, key: str) -> bool:
    """Remove one front-matter key. True if it was there."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return False
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    if not lines or lines[0].strip() != "---":
        return False
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return False
    keep = [l for l in lines[1:end] if not l.startswith(key + ":")]
    if len(keep) == end - 1:
        return False
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(eol.join([lines[0]] + keep + lines[end:]))
    return True


def front_matter_lines(path: str) -> list[str] | None:
    """The front matter as raw lines, delimiters included - for restoring."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    return None if end is None else lines[:end + 1]


def put_front_matter(path: str, saved: list[str]) -> bool:
    """Put saved front matter back over whatever a session left. True if it
    had changed. The body is the session's; the front matter is the runner's,
    because it carries the stamps that decide what gets published."""
    now = front_matter_lines(path)
    if now is None or now == saved:
        return False
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(eol.join(saved + lines[len(now):]))
    return True


def review_path(draft: str, rnd: int) -> str:
    slug = os.path.splitext(os.path.basename(draft))[0]
    return os.path.join(REVIEWS, "%s.r%d.md" % (slug, rnd))


def parse_verdict(path: str) -> str | None:
    """The report's verdict, or None. The last VERDICT line counts."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            found = VERDICT_RE.findall(fh.read())
    except OSError:
        return None
    return found[-1] if found else None


def review_state(f: str) -> str | None:
    """What a buffered paper needs next: 'review', 'revise', or None.

    Only pipeline papers (`written:`), only once their gates have passed, and
    never one that is reviewed, held, published or mid-attempt. A revision
    removes `gated:`, so a revised paper waits here until its gates pass again.
    """
    if not field(f, "written") or field(f, "reviewed"):
        return None
    if field(f, "doi") or field(f, "hold") or field(f, "deposition"):
        return None
    if not field(f, "gated"):
        return None
    rounds = int(field(f, "review_rounds") or 0)
    if (rounds and field(f, "review_verdict") == "REVISE"
            and int(field(f, "revised_round") or 0) < rounds):
        return "revise"
    return "review"


def review_queue() -> list[tuple[str, str]]:
    """(draft, what it needs) for every paper awaiting review work, oldest first."""
    out = [(f, s) for f in drafts_md() for s in [review_state(f)] if s]
    out.sort(key=lambda t: (field(t[0], "written") or "", os.path.basename(t[0])))
    return out


def snapshot() -> dict[str, bytes]:
    """Every draft and the topic queue, as bytes, before a session runs."""
    out = {}
    for p in drafts_md() + [TOPICS]:
        try:
            with open(p, "rb") as fh:
                out[p] = fh.read()
        except OSError:
            pass
    return out


def restore(snap: dict[str, bytes], allow: tuple[str, ...] = ()) -> list[str]:
    """Undo whatever a session did to files it was not allowed to touch.

    Put back every snapshotted file it changed or deleted, and remove any
    draft it created. A reviewer that edits the paper it is judging is no
    longer an independent check, and a session that edits ANOTHER draft's
    front matter can make the next run publish twice.
    """
    fixed = []
    for p, data in snap.items():
        if p in allow:
            continue
        try:
            with open(p, "rb") as fh:
                same = fh.read() == data
        except OSError:
            same = False
        if not same:
            with open(p, "wb") as fh:
                fh.write(data)
            fixed.append(p)
    for p in drafts_md():
        if p not in snap and p not in allow:
            os.remove(p)            # created seconds ago by the session itself
            fixed.append(p)
    return fixed


def run_session(cli: str, prompt: str, quiet: bool, produced) -> str:
    """Run one review or revision session with write_one's retry rules.

    'ok' when produced() says the session left its output; 'down' when the
    CLI failed on authentication or on API errors every time, which says
    nothing about the paper and must not count against it; 'empty' when
    sessions ran cleanly and left nothing, which does.
    """
    flags = claude_flags.flags()
    verdict = "clean"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            note("retrying: attempt %d of %d" % (attempt, MAX_ATTEMPTS))
        chunks: list[str] = []
        proc = subprocess.Popen([cli, "-p", prompt] + flags, cwd=PROJ,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            if not quiet:
                sys.stdout.write(line)
                sys.stdout.flush()
            chunks.append(line)
        proc.wait()
        out = "".join(chunks)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(out)
        verdict = classify(out)
        if verdict == "terminal":
            note("FAILED: authentication problem - not retrying, this needs you.")
            return "down"
        if produced():
            return "ok"
        if attempt < MAX_ATTEMPTS:
            note("the session left no output (%s); waiting %ds"
                 % ("transient API problem" if verdict != "clean" else "it ended cleanly",
                    RETRY_WAIT))
            time.sleep(RETRY_WAIT)
    return "empty" if verdict == "clean" else "down"


def review_step(cli: str, draft: str, run_date: str, quiet: bool) -> str:
    """Take ONE paper one step through review: a review round, or the
    revision the last round asked for. Each step is a single session, so the
    work resumes cleanly on the next slot wherever it stops.

    Returns passed | revise | revised | held | failed | down.
    """
    name = os.path.basename(draft)
    rounds = int(field(draft, "review_rounds") or 0)
    os.makedirs(REVIEWS, exist_ok=True)

    if review_state(draft) == "revise":
        report = review_path(draft, rounds)
        rel_report = os.path.relpath(report, PROJ).replace(os.sep, "/")
        note("revising %s after review round %d (%s)" % (name, rounds, rel_report))
        saved_fm = front_matter_lines(draft)
        with open(draft, "rb") as fh:
            before = fh.read()
        snap = snapshot()

        def revised() -> bool:
            with open(draft, "rb") as fh:
                changed = fh.read() != before
            try:
                with open(report, encoding="utf-8", errors="replace") as fh:
                    answered = "## Response" in fh.read()
            except OSError:
                answered = False
            return changed or answered

        slug = os.path.splitext(name)[0]
        outcome = run_session(cli, PROMPT_REVISE.format(slug=slug, round=rounds),
                              quiet, revised)
        for p in restore(snap, allow=(draft,)):
            note("        the reviser changed %s; undone" % os.path.basename(p))
        if saved_fm and put_front_matter(draft, saved_fm):
            note("        the reviser changed the front matter; restored it")
        if outcome != "ok":
            return _review_failed(draft, outcome, "revision")
        stamp(draft, "revised_round", str(rounds))
        # The old gates proved the OLD text. Their stamps come off before
        # anything else, so this paper can never be read as gated - and so
        # publishable - on the strength of checks made before it changed.
        unstamp(draft, "gated")
        unstamp(draft, "original")
        stamp(draft, "gate_tries", "0")
        stamp(draft, "review_tries", "0")
        if regate(draft, run_date, cli, quiet):
            return "revised"
        return "failed"

    rnd = rounds + 1
    report = review_path(draft, rnd)
    rel_report = os.path.relpath(report, PROJ).replace(os.sep, "/")
    if parse_verdict(report):
        # The session finished in a run that was cut off before its stamps.
        note("review round %d of %s is already on disk; using it" % (rnd, name))
    else:
        note("reviewing %s: round %d of at most %d" % (name, rnd, MAX_REVIEW_ROUNDS))
        slug = os.path.splitext(name)[0]
        previous = PROMPT_REREVIEW.format(slug=slug, prev=rounds) if rounds else ""
        snap = snapshot()
        outcome = run_session(
            cli, PROMPT_REVIEW.format(slug=slug, round=rnd, previous=previous),
            quiet, lambda: parse_verdict(report) is not None)
        # Nothing may change during a review - the paper least of all.
        for p in restore(snap):
            note("        the reviewer changed %s; undone" % os.path.basename(p))
        if outcome != "ok":
            return _review_failed(draft, outcome, "review")

    verdict = parse_verdict(report)
    stamp(draft, "review_rounds", str(rnd))
    stamp(draft, "review_verdict", verdict)
    stamp(draft, "review_tries", "0")
    if verdict == "PASS":
        stamp(draft, "reviewed", run_date)
        note("PASSED review round %d: %s is ready to publish." % (rnd, name))
        return "passed"
    if verdict == "REJECT":
        stamp(draft, "hold", "rejected by review round %d - see %s" % (rnd, rel_report))
        note("HELD %s: review round %d rejected it (%s)." % (name, rnd, rel_report))
        return "held"
    if rnd >= MAX_REVIEW_ROUNDS:
        stamp(draft, "hold", "still REVISE after %d review rounds - see %s"
              % (rnd, rel_report))
        note("HELD %s: still REVISE after %d rounds (%s)." % (name, rnd, rel_report))
        return "held"
    note("review round %d asks for revisions to %s (%s)." % (rnd, name, rel_report))
    return "revise"


def _review_failed(draft: str, outcome: str, what: str) -> str:
    name = os.path.basename(draft)
    if outcome == "down":
        note("the %s session for %s could not run (API or login); it will be"
             " retried next slot and does not count against the paper." % (what, name))
        return "down"
    tries = int(field(draft, "review_tries") or 0) + 1
    stamp(draft, "review_tries", str(tries))
    if tries >= REVIEW_TRIES:
        stamp(draft, "hold", "%s sessions left no output %d times" % (what, tries))
        note("HELD %s: %d %s sessions produced nothing." % (name, tries, what))
        return "held"
    note("the %s session for %s left no output (%d of %d); retrying next slot."
         % (what, name, tries, REVIEW_TRIES))
    return "failed"


def checkpoint(what: str) -> None:
    """Commit and push the run's work so far, in CI only.

    Everything a run does used to be saved by one push at the very end, and
    GitHub skips that step when a job is cancelled - which is what hitting
    timeout-minutes is. A run now reviews and writes for hours, so each
    finished step is pushed as it completes, and a cancellation costs at most
    the step in progress. Best effort: the final push step still runs.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=zenodo-daily-paper[bot]",
             "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
             *args], cwd=PROJ, capture_output=True, text=True)

    # A missing pathspec makes `git add` add nothing at all, so name only
    # paths that exist.
    paths = [p for p in ("drafts", "reviews", "topics.md", "daily_log.md")
             if os.path.exists(os.path.join(PROJ, p))]
    git("add", "-A", *paths)
    git("add", "-f", "runner.log")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", "daily paper: %s" % what)
    for _ in range(3):
        if git("push", "-q").returncode == 0:
            note("saved: %s" % what)
            return
        if git("-c", "rebase.autoStash=true", "pull", "--rebase", "-q",
               "origin", "main").returncode != 0:
            git("rebase", "--abort")
            break
    note("could not push the checkpoint (%s); the final push step retries." % what)


def room_for(minutes: float, yield_to_slot: bool) -> bool:
    """Is there time to start a step that can take `minutes`?"""
    if minutes_left() < minutes:
        return False
    return not (yield_to_slot and minutes_to_next_publish() < minutes)


def review_pending(run_date: str, cli: str, quiet: bool, yield_to_slot: bool,
                   only: str | None = None) -> str:
    """Advance papers through review, oldest first, while time allows.

    Returns 'down' if the API is failing (stop all session work this run),
    else 'ok'. Every step either advances a paper's state or counts a try
    toward a hold, so this cannot spin; the step cap is a second guard.
    """
    stalled: set[str] = set()
    for _ in range(4 * MAX_REVIEW_ROUNDS):
        # A paper whose session just failed waits for the next slot rather
        # than burning three more attempts back to back.
        queue = [(d, s) for d, s in review_queue()
                 if (only is None or d == only) and d not in stalled]
        if not queue:
            return "ok"
        draft, state = queue[0]
        need = REVIEW_MINUTES if state == "review" else REVISE_MINUTES
        if not room_for(need, yield_to_slot):
            note("not starting a %s of %s: too little time before %s."
                 % (state, os.path.basename(draft),
                    "the next publish slot" if yield_to_slot
                    and minutes_to_next_publish() < need else "this job's timeout"))
            return "ok"
        result = review_step(cli, draft, run_date, quiet)
        checkpoint("%s %s" % (state, os.path.basename(draft)))
        if result == "down":
            return "down"
        if result == "failed":
            stalled.add(draft)
    return "ok"


def work(cli: str, run_date: str, quiet: bool, want: int, quota: bool,
         yield_to_slot: bool) -> tuple[int, bool, bool]:
    """Gate, review, then write - in that order, while time allows.

    Nothing already written is thrown away: re-gating costs minutes and
    reviewing an hour, while writing costs an hour AND a topic, so earlier
    papers are finished before new ones are started. Every finished step is
    checkpointed. Returns (papers written, anything progressed, API down).
    """
    wrote = 0
    progressed = False

    def in_pipeline() -> int:
        return len(ready_papers()) + len(review_queue()) + len(ungated_written())

    def more_wanted() -> bool:
        # `want` counts papers on their way as well as ready ones: a slot
        # that needs one paper must not start a second while the first is
        # still in review.
        if in_pipeline() >= want or len(ready_papers()) >= BUFFER_MAX:
            return False
        return not (quota and written_today(run_date) >= DAILY_WRITES)

    for draft in ungated_written():
        if regate(draft, run_date, cli, quiet):
            progressed = True
    checkpoint("gates")

    if review_pending(run_date, cli, quiet, yield_to_slot) == "down":
        return wrote, progressed, True

    # Bounded per run so the job cannot be killed mid-paper.
    while wrote < MAX_PER_RUN and more_wanted():
        # Never start a paper that could still be drafting when a publish
        # slot opens: one concurrency group means the publish would wait
        # behind it, and a second pending slot would cancel the first.
        if not room_for(PAPER_MINUTES, yield_to_slot):
            note("not starting another paper: %s." % (
                "the next publish slot opens in %d minutes" % minutes_to_next_publish()
                if yield_to_slot and minutes_to_next_publish() < PAPER_MINUTES
                else "too little of this job's time is left"))
            break
        note("writing: %d of %d written today, %d ready."
             % (written_today(run_date), DAILY_WRITES, len(ready_papers())))
        before = set(drafts_md())
        failed, _m, _d = write_one(cli, PROMPT_WRITE, quiet)
        fresh = [d for d in drafts_md() if d not in before]
        if not fresh:
            note("FAILED: the session produced no draft; stopping rather")
            note("        than looping.")
            return wrote, progressed, failed
        wrote += 1
        progressed = True
        draft = fresh[0]
        # Recorded before gating, so the paper counts toward today and
        # sorts correctly in the buffer whatever the gates decide.
        if not stamp(draft, "written", run_date):
            note("FAILED: could not stamp written: into %s" % draft)
            break
        # Before anything else: this topic must not be drafted again.
        if not ensure_topic_marked(draft, run_date):
            note("FAILED: could not mark the topic for %s as used; stopping"
                 % os.path.basename(draft))
            note("        rather than risk drafting the same topic twice.")
            break
        regate(draft, run_date, cli, quiet)
        checkpoint("wrote %s" % os.path.basename(draft))
        # Review it now if there is time; otherwise the next slot does.
        if review_pending(run_date, cli, quiet, yield_to_slot, only=draft) == "down":
            return wrote, progressed, True

    if wrote >= MAX_PER_RUN and more_wanted():
        note("stopped at the %d-paper cap for one run; the next slot"
             " continues." % MAX_PER_RUN)
    return wrote, progressed, False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage-only", action="store_true",
                    help="draft and gate, but publish nothing")
    ap.add_argument("--check", action="store_true",
                    help="report what would happen; run nothing")
    ap.add_argument("--ignore-completed", action="store_true",
                    help="publish even though today already has a paper. A "
                         "deliberate, human-only override of the completion "
                         "guard; the citation gate and the duplicate check "
                         "are NOT affected and still have to pass.")
    ap.add_argument("--fill-buffer", type=int, nargs="?", const=BUFFER_TARGET,
                    default=None, metavar="N",
                    help="write papers until N proven-publishable ones are "
                         "banked (default %d), then stop. Publishes nothing."
                         % BUFFER_TARGET)
    ap.add_argument("--quiet", action="store_true",
                    help="keep the session transcript out of stdout; it still "
                         "goes to runner.log. Use this when stdout is a public "
                         "CI log and the paper is not public yet.")
    args = ap.parse_args(argv)
    # Every child - the drafting session, publish_paper, the gates - stamps
    # the date THIS run started on. A run that crosses midnight still files
    # its paper under the day the slot belonged to.
    os.environ.setdefault("ZENODO_RUN_DATE", _dt.date.today().isoformat())

    note("----- run starting%s -----" % (" (stage-only)" if args.stage_only else ""))

    already = todays_published_doi()
    done_today = todays_published_count()
    target = target_for()
    owed = missed_yesterday() if target else 0
    target += owed
    # "Enough for now", not "any at all": with two slots a day the question is
    # whether this slot's paper exists, not whether the day has one.
    satisfied = done_today >= target

    # --check reports state and stops. It runs before the completion guard on
    # purpose: on a finished day the guard would exit first, and a diagnostic
    # that cannot tell you anything on the days you most want to ask is no
    # diagnostic at all.
    if args.check:
        note("check: cli              = %s" % (find_cli() or "NOT FOUND"))
        note("check: model            = %s at effort %s"
             % (claude_flags.MODEL, claude_flags.EFFORT))
        note("check: zenodo token     = %s" % ("set" if os.environ.get("ZENODO_TOKEN")
                                               or os.environ.get("ZENODO_ACCESS_TOKEN") else "MISSING"))
        note("check: claude credential= %s" % ("oauth" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                                               else "api key" if os.environ.get("ANTHROPIC_API_KEY")
                                               else "MISSING"))
        note("check: drafts           = %d (%d carry a DOI)"
             % (len(drafts_md()), len(published_dois())))
        note("check: today published  = %d of %d wanted by now%s"
             % (done_today, target, (" (latest %s)" % already) if already else ""))
        wrote_today = written_today(_dt.date.today().isoformat())
        banked = len(ready_papers())
        awaiting = len(ungated_written())
        queue = review_queue()
        to_review = sum(1 for _, s in queue if s == "review")
        note("check: buffer           = %d ready (gated and reviewed), %d awaiting"
             " gates%s" % (banked, awaiting,
                           ("  [IN FLIGHT: %s]" % os.path.basename(in_flight()))
                           if in_flight() else ""))
        note("check: review queue     = %d to review, %d to revise"
             % (to_review, len(queue) - to_review))
        note("check: written today    = %d of %d" % (wrote_today, DAILY_WRITES))
        note("check: today pending    = %s"
             % (os.path.basename(todays_pending_draft() or "") or "no"))
        note("check: mode             = %s" % ("stage-only" if args.stage_only else "publish"))
        # Report what the run will actually do. A satisfied slot does not just
        # exit - it reviews and writes ahead.
        writing = wrote_today < DAILY_WRITES and banked < BUFFER_MAX
        if in_flight() and not args.stage_only:
            would = "BLOCK - an earlier publish attempt is unresolved"
        elif satisfied and not args.stage_only:
            if not (queue or awaiting or writing):
                would = "nothing (published enough for this hour; quota met)"
            elif minutes_to_next_publish() < min(REVIEW_MINUTES, PAPER_MINUTES) \
                    and not awaiting:
                would = "nothing (a publish slot opens too soon to start a session)"
            else:
                would = "review and write ahead into the buffer (publishes nothing)"
        elif banked and not args.stage_only:
            would = "publish the oldest ready paper"
        elif (queue or awaiting) and not args.stage_only:
            would = "finish gating and reviewing a buffered paper, then publish it if it passes"
        elif todays_pending_draft() and not args.stage_only:
            would = "publish today's existing draft (no new paper)"
        elif args.stage_only:
            would = "write a new paper and stage it"
        else:
            would = "write and review a paper, then publish it if it passes"
        note("check: would            = %s" % would)

        # A diagnostic that prints MISSING and then reports success is the
        # same mistake as a gate that cannot run and calls itself passed. In
        # CI this step exists to catch an unset secret BEFORE an hour of work
        # starts, so a missing prerequisite has to fail here.
        missing = [n for n, ok in (
            ("claude CLI", bool(find_cli())),
            ("ZENODO_TOKEN", bool(os.environ.get("ZENODO_TOKEN")
                                  or os.environ.get("ZENODO_ACCESS_TOKEN"))),
            ("a Claude credential", bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                                         or os.environ.get("ANTHROPIC_API_KEY"))),
        ) if not ok]
        if missing:
            note("check: FAILED - missing %s" % ", ".join(missing))
            note("----- run finished -----")
            return 1
        note("----- run finished -----")
        return 0

    # A finished day is a no-op. Cheapest question to answer, so answer it
    # before starting any work at all.
    #
    # --ignore-completed is the one way past this, and it is deliberately
    # narrow: it can only arrive from a human running workflow_dispatch, never
    # from the schedule, and it overrides ONLY the "one paper a day" rule. The
    # citation gate and the duplicate-title check are untouched and still have
    # to pass before anything is minted.
    if satisfied and args.ignore_completed and not args.stage_only:
        note("Today already has %d paper(s), which meets the target of %d for now."
             % (done_today, target))
        note("        --ignore-completed was passed, so continuing anyway and")
        note("        publishing an EXTRA paper for today. The citation gate and")
        note("        duplicate check still apply.")
    elif satisfied and not args.stage_only:
        note("Today already has %d paper(s) of the %d wanted by this hour."
             % (done_today, target))
        if already:
            note("        latest: https://doi.org/%s" % already)
        # This slot has nothing to publish. Rather than exit, use the time to
        # write ahead - the buffer is what stops a future writing failure from
        # becoming a missed publication.
        #
        # Decided by NEED, not by which cron fired. Keying the writer off
        # github.event.schedule failed on 2026-09-25: both writer crons fired,
        # both ran the publish path, and GitHub's 60-70 minute delays made the
        # cron identity unverifiable from the timings. Need is observable;
        # which-slot-am-I is not.
        day = _dt.date.today().isoformat()
        wrote_today = written_today(day)
        banked = len(ready_papers())
        pending = len(ungated_written())
        queued = len(review_queue())
        if not (queued or pending) and (wrote_today >= DAILY_WRITES
                                        or banked >= BUFFER_MAX):
            note("        Nothing to do - this slot is finished. Written today %d"
                 % wrote_today)
            note("        of %d, %d ready, none awaiting review." % (DAILY_WRITES, banked))
            note("----- run finished -----")
            return 0
        note("        Nothing to publish, so reviewing and writing ahead: %d of %d"
             % (wrote_today, DAILY_WRITES))
        note("        written today, %d ready, %d awaiting gates, %d awaiting review."
             % (banked, pending, queued))
        args.fill_buffer = BUFFER_MAX
        args.quota = True

    token = os.environ.get("ZENODO_TOKEN") or os.environ.get("ZENODO_ACCESS_TOKEN")
    if not token:
        note("ABORT: ZENODO_TOKEN is not set.")
        return 1
    os.environ.setdefault("ZENODO_TOKEN", token)

    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        note("ABORT: no Claude credential. Set CLAUDE_CODE_OAUTH_TOKEN (from")
        note("       `claude setup-token`) or ANTHROPIC_API_KEY.")
        return 1

    cli = find_cli()
    if not cli:
        note("ABORT: claude CLI not found on PATH.")
        return 1

    run_date = _dt.date.today().isoformat()

    # ---- nothing proceeds while an attempt is unresolved -----------------
    # `deposition:` without `doi:` means a previous attempt reached the part
    # of Zenodo that cannot be undone and we do not know how it ended. Writing
    # is fine; publishing anything is not, because the unresolved paper may
    # already be live and a second attempt would mint a second permanent
    # record for the same work.
    stuck = in_flight()
    if stuck and not args.stage_only and args.fill_buffer is None:
        dep = field(stuck, "deposition")
        note("BLOCKED: %s carries deposition %s and no doi."
             % (os.path.basename(stuck), dep))
        note("        An attempt reached Zenodo and its outcome is unknown, so")
        note("        publishing anything now risks a second permanent record.")
        note("        Check it, then either record the doi: in the front matter")
        note("        or remove the draft with: python delete_draft.py %s" % dep)
        note("----- run finished -----")
        return 1

    # ---- writer mode: bank papers, publish nothing ------------------------
    if args.fill_buffer is not None:
        quota = getattr(args, "quota", False)
        wrote, progressed, down = work(cli, run_date, args.quiet, args.fill_buffer,
                                       quota, yield_to_slot=True)
        note("writer finished: %d written, %d written today, %d ready, %d"
             " awaiting review." % (wrote, written_today(run_date),
                                    len(ready_papers()), len(review_queue())))
        note("----- run finished -----")
        return 1 if down else 0

    # ---- publish from the buffer -----------------------------------------
    # The point of the buffer: the slow, failure-prone half (writing and
    # reviewing) is no longer inside the irreversible half (publishing).
    if not args.stage_only:
        ready = ready_papers()
        if not ready:
            # Nothing reviewed is waiting. Finish what is already written -
            # gates, then review - before writing anything new, and write
            # only if nothing at all is in the pipeline. The slot is due, so
            # there is no later slot to yield to.
            note("no reviewed paper is ready - finishing buffered work first.")
            work(cli, run_date, args.quiet, 1, quota=False, yield_to_slot=False)
            ready = ready_papers()
            if not ready and not todays_pending_draft():
                note("FAILED: no paper passed review in time for this slot. The")
                note("        work so far is saved; the next slot continues it.")
                note("----- run finished -----")
                return 1
        if ready:
            if owed:
                note("making up %d paper(s) yesterday's slots did not publish." % owed)
            result = publish_from_buffer(run_date)
            checkpoint("publish")
            note("----- run finished -----")
            return 1 if result is None else result
        note("no pipeline paper is ready; publishing a hand-made draft dated today.")

    # A draft dated today that the pipeline did not write - one a person made
    # and left to publish. Pipeline papers never come through here: they
    # publish from the buffer, reviewed, or not at all.
    pending = todays_pending_draft()
    if pending and not args.stage_only:
        rc = publish_existing(pending)
        minted = draft_doi(pending)
        if resume_succeeded(rc, minted):
            note("OK: published %s - https://doi.org/%s" % (minted, minted))
            note("----- run finished -----")
            return 0
        note("FAILED: could not publish today's existing draft (exit %d)." % rc)
        note("        The gates above say why. Nothing was written twice.")
        note("----- run finished -----")
        return 1

    # Only --stage-only reaches this point: every publishing path above has
    # returned. Writing a paper and publishing it in one unreviewed session
    # is exactly what the review exists to prevent, so PROMPT_PUBLISH is no
    # longer used by a scheduled run.
    if not args.stage_only:
        note("FAILED: nothing to publish.")
        note("----- run finished -----")
        return 1
    prompt = PROMPT_STAGE
    dois_before = published_dois()
    drafts_before = len(drafts_md())
    seen_before = set(drafts_md())
    failed, _minted, _drafted = write_one(cli, prompt, args.quiet)
    for d in drafts_md():
        if d not in seen_before and not field(d, "written"):
            stamp(d, "written", _dt.date.today().isoformat())

    if failed:
        note("FAILED: no paper produced.")
        note("----- run finished -----")
        return 1

    new_dois = published_dois() - dois_before
    new_drafts = len(drafts_md()) - drafts_before

    if args.stage_only:
        # Success here is a draft, not a DOI - publishing was forbidden.
        if new_dois:
            note("UNEXPECTED: --stage-only was passed but a DOI was minted: %s"
                 % ", ".join(sorted(new_dois)))
            note("----- run finished -----")
            return 1
        if new_drafts > 0:
            note("OK (stage-only): %d new draft(s), nothing published." % new_drafts)
            note("        Inspect the PDF, then publish with:")
            note("        python publish_paper.py drafts/<slug>.md --publish --yes")
            note("----- run finished -----")
            return 0
        note("FAILED: stage-only run produced no draft.")
        note("----- run finished -----")
        return 1

    if new_dois:
        for d in sorted(new_dois):
            note("OK: published %s - https://doi.org/%s" % (d, d))
        note("----- run finished -----")
        return 0

    if new_drafts > 0:
        # The draft THIS run wrote - not the most recently modified file. On
        # 2026-09-25 a session edited an older paper after writing its own,
        # and this line named the edited paper instead.
        fresh = [d for d in drafts_md() if d not in seen_before]
        newest = fresh[0] if fresh else max(drafts_md(), key=os.path.getmtime)
        note("FAILED: a draft was written but no DOI was minted - %s"
             % os.path.basename(newest))
        if field(newest, "gated"):
            note("        It passed every gate and is banked, so the next slot")
            note("        publishes it from the buffer. Nothing written is lost.")
        note("        Either the run stopped early or it deliberately staged the")
        note("        paper. Read the session output above, then finish it with:")
        note("        python publish_paper.py drafts/%s --publish --yes"
             % os.path.basename(newest))
    else:
        note("FAILED: no new draft and no DOI (drafts still %d)." % len(drafts_md()))
        note("        The run produced nothing. Read the session output above -")
        note("        an auth or network error there is the usual cause.")

    note("----- run finished -----")
    return 1


if __name__ == "__main__":
    sys.exit(main())
