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
    """One front-matter scalar, or None. Blank counts as absent."""
    m = re.search(r"(?m)^%s:\s*(\S.*?)\s*$" % re.escape(name), front_matter(path))
    return m.group(1) if m else None


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

    `gated:` is the whole point: it is written only after the offline gates
    actually ran and passed, so selection never rests on a session's report
    about its own work. A paper on hold, already published, or mid-attempt is
    not ready.
    """
    out = []
    for f in drafts_md():
        if field(f, "doi") or field(f, "hold") or field(f, "deposition"):
            continue
        if field(f, "gated"):
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
    out = set()
    for f in drafts_md():
        m = re.search(r"(?m)^doi:\s*(\S+)", front_matter(f))
        if m:
            out.add(m.group(1))
    return out


BUFFER_TARGET = 4         # default N for a manual --fill-buffer
MAX_PER_RUN = 3           # papers one writer run may draft
DAILY_WRITES = 4          # papers to write per day - "3 to 4 daily"
BUFFER_MAX = 40           # runaway guard: ~20 days of cover at 2/day
GATE_TRIES = 3            # gate attempts before a written paper is held
MORNING_HOUR = 5          # IST; nothing publishes before this
PAPER_MINUTES = 100       # longest a paper takes, with margin

DAILY_TARGET = 2          # papers per day
EVENING_HOUR = 19         # IST; the hour the second slot opens


def todays_published_count(today=None):
    """How many of today's drafts already carry a DOI."""
    today = today or _dt.date.today().isoformat()
    n = 0
    for f in drafts_md():
        head = front_matter(f)
        dated = re.search(r"(?m)^date:\s*" + re.escape(today) + r"\b", head)
        if dated and re.search(r"(?m)^doi:\s*\S+", head):
            n += 1
    return n


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
    """The DOI recorded in one draft's front matter, if it has one."""
    m = re.search(r"(?m)^doi:\s*(\S+)", front_matter(path))
    return m.group(1) if m else None


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
        head = front_matter(f)
        if re.search(r"(?m)^date:\s*" + re.escape(today) + r"\b", head):
            m = re.search(r"(?m)^doi:\s*(\S+)", head)
            if m:
                return m.group(1)
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
        # regate(), not straight to publish: resuming it here would skip the
        # buffer's proof step and retry the same failing gates every slot.
        if field(f, "hold") or (field(f, "written") and not field(f, "gated")):
            continue
        head = front_matter(f)
        if re.search(r"(?m)^date:\s*" + re.escape(today) + r"\b", head):
            if not re.search(r"(?m)^doi:\s*\S+", head):
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


def regate(draft: str, run_date: str) -> bool:
    """Retry the gates on a paper that is already written.

    The attempt is counted BEFORE the gates run, so a crash mid-gate still
    counts. After GATE_TRIES failures the paper is held with a reason rather
    than retried forever; until then a failure is treated as transient -
    usually arXiv or Crossref unreachable - because nothing written should
    be discarded on one bad morning.
    """
    tries = int(field(draft, "gate_tries") or 0) + 1
    stamp(draft, "gate_tries", str(tries))
    name = os.path.basename(draft)
    if gate(draft, run_date):
        note("banked %s (gate attempt %d)" % (name, tries))
        return True
    if tries >= GATE_TRIES:
        stamp(draft, "hold", "failed its gates %d times, last on %s" % (tries, run_date))
        note("HELD %s after %d failed gate attempts - it needs a human." % (name, tries))
    else:
        note("%s failed its gates (attempt %d of %d); will retry next slot."
             % (name, tries, GATE_TRIES))
    return False


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
        note("check: buffer           = %d banked, %d awaiting gates%s"
             % (banked, awaiting,
                ("  [IN FLIGHT: %s]" % os.path.basename(in_flight()))
                if in_flight() else ""))
        note("check: written today    = %d of %d" % (wrote_today, DAILY_WRITES))
        note("check: today pending    = %s"
             % (os.path.basename(todays_pending_draft() or "") or "no"))
        note("check: mode             = %s" % ("stage-only" if args.stage_only else "publish"))
        # Report what the run will actually do. A satisfied slot no longer
        # just exits - it writes ahead until today's quota is met.
        if in_flight() and not args.stage_only:
            would = "BLOCK - an earlier publish attempt is unresolved"
        elif satisfied and not args.stage_only:
            if (wrote_today >= DAILY_WRITES and not awaiting) or banked >= BUFFER_MAX:
                would = "nothing (published enough for this hour; quota met)"
            elif minutes_to_next_publish() < PAPER_MINUTES and not awaiting:
                would = "nothing (a publish slot opens too soon to start a paper)"
            else:
                would = "write ahead into the buffer (publishes nothing)"
        elif banked and not args.stage_only:
            would = "publish the oldest banked paper"
        elif awaiting and not args.stage_only:
            would = "re-gate a written paper, then publish it if it passes"
        elif todays_pending_draft() and not args.stage_only:
            would = "publish today's existing draft (no new paper)"
        else:
            would = "write a new paper inline, then publish it"
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
        if (wrote_today >= DAILY_WRITES and not pending) or banked >= BUFFER_MAX:
            note("        Nothing to do - this slot is finished. Written today %d"
                 % wrote_today)
            note("        of %d, banked %d." % (DAILY_WRITES, banked))
            note("----- run finished -----")
            return 0
        note("        Nothing to publish, so writing ahead: %d of %d written"
             % (wrote_today, DAILY_WRITES))
        note("        today, %d banked, %d awaiting their gates."
             % (banked, pending))
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
        want = args.fill_buffer
        wrote = gated = 0

        def more_wanted() -> bool:
            if len(ready_papers()) >= want:
                return False
            return not (quota and written_today(run_date) >= DAILY_WRITES)

        # Nothing already written is thrown away. Finish gating earlier
        # papers before drafting new ones: re-gating costs minutes, writing
        # costs an hour and a topic.
        for draft in ungated_written():
            if regate(draft, run_date):
                gated += 1

        # Bounded per run so the job cannot be killed mid-paper. A paper is
        # 30-90 minutes; three fits inside the job's timeout with room.
        while wrote < MAX_PER_RUN and more_wanted():
            # Never start a paper that could still be drafting when a publish
            # slot opens: one concurrency group means the publish would wait
            # behind it, and a second pending slot would cancel the first.
            left = minutes_to_next_publish()
            if left < PAPER_MINUTES:
                note("not starting another paper: the next publish slot opens in"
                     " %d minutes." % left)
                break
            note("writing: %d of %d written today, %d banked."
                 % (written_today(run_date), DAILY_WRITES, len(ready_papers())))
            before = set(drafts_md())
            _f, _m, _d = write_one(cli, PROMPT_WRITE, args.quiet)
            fresh = [d for d in drafts_md() if d not in before]
            if not fresh:
                note("FAILED: the session produced no draft; stopping rather")
                note("        than looping.")
                break
            wrote += 1
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
            if regate(draft, run_date):
                gated += 1

        if wrote >= MAX_PER_RUN and more_wanted():
            note("stopped at the %d-paper cap for one run; the next slot"
                 " continues." % MAX_PER_RUN)
        note("writer finished: %d written, %d banked, %d written today,"
             " buffer now %d." % (wrote, gated, written_today(run_date),
                                  len(ready_papers())))
        note("----- run finished -----")
        return 0 if (gated or wrote or not more_wanted()) else 1

    # ---- publish from the buffer -----------------------------------------
    # The point of the buffer: the slow, failure-prone half (writing) is no
    # longer inside the irreversible half (publishing).
    if not args.stage_only:
        ready = ready_papers()
        if not ready:
            # A written paper that has not passed its gates yet is worth far
            # more than a new one written from scratch now. Try those first.
            for draft in ungated_written():
                if regate(draft, run_date):
                    break
            ready = ready_papers()
        if ready:
            paper = ready[0]
            note("publishing from the buffer: %s (written %s, gated %s)"
                 % (os.path.basename(paper), field(paper, "written") or "?",
                    field(paper, "gated") or "?"))
            rc = publish_existing(paper)
            minted = draft_doi(paper)
            if resume_succeeded(rc, minted):
                note("OK: published %s - https://doi.org/%s" % (minted, minted))
                mark_published(paper, minted, run_date)
                note("        buffer now holds %d." % len(ready_papers()))
                note("----- run finished -----")
                return 0
            note("FAILED: could not publish %s (exit %d)."
                 % (os.path.basename(paper), rc))
            note("        The gates above say why. Nothing was written twice.")
            note("----- run finished -----")
            return 1
        note("buffer is empty - writing this one inline, as before.")

    # Transition path, and the resume path it replaces: a draft written today
    # that never published. Kept so nothing already written is ever wasted.
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

    prompt = PROMPT_STAGE if args.stage_only else PROMPT_PUBLISH
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
        newest = max(drafts_md(), key=os.path.getmtime)
        note("FAILED: a draft was written but no DOI was minted - %s"
             % os.path.basename(newest))
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
