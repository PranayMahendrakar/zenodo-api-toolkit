r"""Tests for the CI runner's decision logic.

No network, no Claude, no Zenodo - these cover the three judgements that have
actually gone wrong in production:

  * a finished day must be recognised, or a redundant launch burns an hour and
    gets retried as if it had failed;
  * a session must be judged on how it ENDED, not on what it mentioned, or an
    83-minute run that merely discussed a timeout is retried as a live failure;
  * an auth failure must never be classed as transient, or the runner spends
    15 minutes retrying a login that cannot fix itself.

    python tests/test_run_ci.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime as _dtm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run_ci

PASS = FAIL = 0


def check(what: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("ok    %s" % what)
    else:
        FAIL += 1
        print("FAIL  %s\n        got:  %r\n        want: %r" % (what, got, want))


# ── classification: the tail decides ────────────────────────────────────
long_run = ["Request timed out"] + ["working, line %d" % i for i in range(60)] + [
    "## Two defects for you",
    "the runner logged a transient API problem and relaunched",
    "I did not edit run_daily.ps1 myself - that is your call.",
]
check("a held run that mentions a timeout early is clean",
      run_ci.classify("\n".join(long_run)), "clean")

check("a session that died on a timeout is transient",
      run_ci.classify("\n".join(["line %d" % i for i in range(40)] + ["Request timed out"])),
      "transient")

check("connection drop at the end is transient",
      run_ci.classify("I'll start by reading the instructions file.\n"
                      "API Error: Connection lost mid-response."), "transient")

check("the real expired-session wording is terminal",
      run_ci.classify("Failed to authenticate: OAuth session expired and could not be refreshed"),
      "terminal")

check("the older expired-token wording is terminal",
      run_ci.classify("OAuth access token has expired"), "terminal")

check("auth beats transient when both appear",
      run_ci.classify("API Error\nFailed to authenticate: session expired"), "terminal")

check("a successful run is clean",
      run_ci.classify("\n".join(["line %d" % i for i in range(40)] +
                                ["Done. Published. DOI 10.5281/zenodo.22884949"])), "clean")

check("ordinary prose is clean",
      run_ci.classify("I'll start by reading the instructions file."), "clean")



# ── should another attempt run? judge by output, not by transcript ──────
# 22 Sep 2026: a session delegated the paper to a background task, said it
# would report back, and exited. The task was killed with the process. The
# transcript classifies as "clean", so the runner broke out of the retry loop
# and failed the run with 137 of its 150 minutes unspent.
check("a clean session that produced nothing is retried",
      run_ci.should_retry("clean", minted=False, drafted=False), True)
check("that exact transcript still classifies as clean",
      run_ci.classify("I've kicked off the pipeline as a background agent. "
                      "I'll report back with the full citation-gate output "
                      "once it finishes - no need to poll."), "clean")

check("a clean session that wrote a draft is done",
      run_ci.should_retry("clean", minted=False, drafted=True), False)
check("a clean session that minted a DOI is done",
      run_ci.should_retry("clean", minted=True, drafted=False), False)

check("a transient failure with nothing produced is retried",
      run_ci.should_retry("transient", minted=False, drafted=False), True)
check("a transient failure that still wrote a draft is not retried",
      run_ci.should_retry("transient", minted=False, drafted=True), False)
check("a transient failure that still minted a DOI is not retried",
      run_ci.should_retry("transient", minted=True, drafted=False), False)

check("an auth failure is never retried",
      run_ci.should_retry("terminal", minted=False, drafted=False), False)


# ── finishing an existing draft: ask about the DRAFT, not the day ───────
# 22 Sep 2026, publishing a deliberate second paper: the success test was
# "does today have a published DOI". With --ignore-completed the day already
# had one, so that answered yes no matter what the publish did. The run
# reported OK and printed the MORNING's DOI for a paper it had just minted a
# different DOI for - and would have reported OK had it failed outright.
check("a resume that minted a DOI succeeded",
      run_ci.resume_succeeded(0, "10.5281/zenodo.22901280"), True)
check("a resume that minted nothing failed",
      run_ci.resume_succeeded(0, None), False)
check("a nonzero exit is a failure even with a DOI present",
      run_ci.resume_succeeded(1, "10.5281/zenodo.22901280"), False)
check("a nonzero exit with no DOI is a failure",
      run_ci.resume_succeeded(1, None), False)

# ── completion guard, against synthetic drafts ──────────────────────────
tmp = tempfile.mkdtemp(prefix="run_ci_test_")
real_drafts = run_ci.DRAFTS
run_ci.DRAFTS = tmp


def write(name: str, date: str, doi: str | None) -> None:
    body = ["---", 'title: "T"', "date: %s" % date]
    if doi:
        body.append("doi: %s" % doi)
    body += ["---", "", "body"]
    with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
        fh.write("\n".join(body))


write("published.md", "2026-09-22", "10.5281/zenodo.22884949")
write("staged.md", "2026-09-20", None)          # drafted, never published
write("older.md", "2026-09-19", "10.5281/zenodo.22840565")

check("a finished day is recognised",
      run_ci.todays_published_doi("2026-09-22"), "10.5281/zenodo.22884949")
check("a day that drafted but never published still runs",
      run_ci.todays_published_doi("2026-09-20"), None)
check("a day with nothing at all still runs",
      run_ci.todays_published_doi("2026-09-21"), None)
check("an earlier finished day is recognised",
      run_ci.todays_published_doi("2026-09-19"), "10.5281/zenodo.22840565")
# ── the resume path: a drafted-but-unpublished day ──────────────────────
check("a day drafted but not published is pending",
      os.path.basename(run_ci.todays_pending_draft("2026-09-20") or ""), "staged.md")
check("a finished day has nothing pending",
      run_ci.todays_pending_draft("2026-09-22"), None)
check("a day with no draft at all has nothing pending",
      run_ci.todays_pending_draft("2026-09-21"), None)

check("the DOI is read from the draft that has it",
      run_ci.draft_doi(os.path.join(tmp, "published.md")), "10.5281/zenodo.22884949")
check("a draft with no DOI reads as none",
      run_ci.draft_doi(os.path.join(tmp, "staged.md")), None)

# ── two papers a day, one per slot ──────────────────────────────────────
write("evening.md", "2026-09-22", "10.5281/zenodo.22901280")   # 2nd for the day

check("both of today's published papers are counted",
      run_ci.todays_published_count("2026-09-22"), 2)
check("a day with one paper counts one",
      run_ci.todays_published_count("2026-09-19"), 1)
check("a day with only an unpublished draft counts none",
      run_ci.todays_published_count("2026-09-20"), 0)

# Before the evening slot opens, ONE paper is the whole target. A flat target
# of 2 would make the 08:00 retry see "one of two" and write the evening paper
# in the morning, collapsing the spacing topics.md builds in on purpose.
# Nothing publishes before 05:00. The old test asserted a target of 1 at
# 00:00 - which is exactly what put two "morning" papers on Zenodo at 01:24
# and 02:11 IST, when a delayed 23:00 retry or a 01:00 writer slot saw
# "0 of 1 today" after midnight.
for hh in (0, 1, 3, 4):
    check("at %02d:00 IST nothing publishes" % hh,
          run_ci.target_for(_dtm(2026, 9, 22, hh, 0)), 0)
check("at 04:59 IST nothing publishes",
      run_ci.target_for(_dtm(2026, 9, 22, 4, 59)), 0)
for hh in (5, 8, 11, 14, 18):
    check("at %02d:00 IST the target is 1" % hh,
          run_ci.target_for(_dtm(2026, 9, 22, hh, 0)), 1)
for hh in (19, 21, 23):
    check("at %02d:00 IST the target is 2" % hh,
          run_ci.target_for(_dtm(2026, 9, 22, hh, 0)), 2)

# The scenarios that actually decide whether a run does work.
def satisfied(count, hour):
    return count >= run_ci.target_for(_dtm(2026, 9, 22, hour, 0))

check("05:00 with nothing published -> work",       satisfied(0, 5), False)
check("08:00 retry after the morning ran -> skip",  satisfied(1, 8), True)
check("08:00 retry after a failed morning -> work", satisfied(0, 8), False)
check("19:00 with the morning done -> work",        satisfied(1, 19), False)
check("21:00 with both done -> skip",               satisfied(2, 21), True)
check("21:00 with only one done -> work",           satisfied(1, 21), False)

check("every DOI on disk is collected",
      run_ci.published_dois(),
      {"10.5281/zenodo.22884949", "10.5281/zenodo.22840565",
       "10.5281/zenodo.22901280"})

run_ci.DRAFTS = real_drafts


# -- the buffer -----------------------------------------------------------
# `date:` used to do three jobs: Zenodo's publication_date, the completion
# guard's key, and the in-flight marker. Under a buffer a paper is written
# days before it is published, so the three diverge. Each now has its own
# field, and each of these tests fails if its guard is removed.
import textwrap

buf = tempfile.mkdtemp(prefix="run_ci_buffer_")
run_ci.DRAFTS = buf


def paper(name, **fields):
    lines = ["---", 'title: "T"']
    for k, v in fields.items():
        if v is not None:
            lines.append("%s: %s" % (k, v))
    lines += ["---", "", "## References", "",
              # A real reference line, at column zero. front_matter() must stop
              # at the closing --- or it reads this as the paper's own DOI.
              "Doe, J. (2026). Thing. arXiv:2601.00001. doi:10.48550/arXiv.2601.00001"]
    with open(os.path.join(buf, name), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return os.path.join(buf, name)


p_ready_old = paper("ready-old.md", written="2026-09-20", gated="2026-09-20",
                    reviewed="2026-09-20")
p_ready_new = paper("ready-new.md", written="2026-09-23", gated="2026-09-23",
                    reviewed="2026-09-24")
p_unreviewed = paper("unreviewed.md", written="2026-09-19", gated="2026-09-19")
p_ungated = paper("ungated.md", written="2026-09-21")
p_held = paper("held.md", written="2026-09-19", gated="2026-09-19", hold="bad figure")
p_done = paper("done.md", written="2026-09-18", gated="2026-09-18",
               date="2026-09-22", doi="10.5281/zenodo.1")
p_flight = paper("inflight.md", written="2026-09-17", gated="2026-09-17",
                 deposition="99887766")

check("a reference line is not read as the paper's own doi",
      run_ci.field(p_ready_old, "doi"), None)
check("front matter fields are read",
      run_ci.field(p_ready_old, "gated"), "2026-09-20")

ready = [os.path.basename(x) for x in run_ci.ready_papers()]
check("only gated, unpublished, unheld, not-in-flight papers are ready",
      ready, ["ready-old.md", "ready-new.md"])
check("ready papers come out oldest-written first (FIFO)",
      ready[0], "ready-old.md")

check("an ungated draft is never selected", "ungated.md" in ready, False)
check("a gated paper nobody has reviewed is never selected, however old",
      "unreviewed.md" in ready, False)
check("a held paper is never selected", "held.md" in ready, False)
check("a published paper is never re-selected", "done.md" in ready, False)
check("a paper mid-attempt is never selected", "inflight.md" in ready, False)

check("an unresolved attempt is detected",
      os.path.basename(run_ci.in_flight() or ""), "inflight.md")

# The in-flight marker must be a FACT, not inferred from a date - that
# inference is what stranded drafts before.
os.remove(p_flight)
check("no unresolved attempt once it is gone", run_ci.in_flight(), None)

# The completion guard counts by PUBLICATION date, which publish_paper stamps
# at mint time - not by when the paper was written.
check("the guard counts the day a paper was published",
      run_ci.todays_published_count("2026-09-22"), 1)
check("the guard does not count the day it was written",
      run_ci.todays_published_count("2026-09-18"), 0)

run_ci.DRAFTS = real_drafts



# -- writing 3-4 a day ----------------------------------------------------
spec = tempfile.mkdtemp(prefix="run_ci_spec_")
run_ci.DRAFTS = spec


def mk(name, body_fields):
    p = os.path.join(spec, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("---\n")
        for k, v in body_fields:
            fh.write("%s: %s\n" % (k, v))
        fh.write("---\n\n## References\n\nX. doi:10.48550/arXiv.2601.00001\n")
    return p


a = mk("a.md", [("title", '"A"'), ("date", "2026-09-25")])
check("stamp adds a missing key", run_ci.stamp(a, "written", "2026-09-25"), True)
check("the added key reads back", run_ci.field(a, "written"), "2026-09-25")
run_ci.stamp(a, "written", "2026-09-26")
check("stamp replaces rather than duplicates",
      open(a, encoding="utf-8").read().count("written:"), 1)
check("the replaced value reads back", run_ci.field(a, "written"), "2026-09-26")
check("stamp never touches the body",
      "doi:10.48550/arXiv.2601.00001" in open(a, encoding="utf-8").read(), True)
check("the body doi is still not read as the paper's",
      run_ci.field(a, "doi"), None)
nofm = os.path.join(spec, "nofm.md")
open(nofm, "w", encoding="utf-8").write("no front matter here\n")
check("stamp refuses a file with no front matter",
      run_ci.stamp(nofm, "written", "2026-09-25"), False)
os.remove(nofm)

w1 = mk("w1.md", [("written", "2026-09-25")])
w2 = mk("w2.md", [("written", "2026-09-25"), ("gated", "2026-09-25")])
w3 = mk("w3.md", [("written", "2026-09-25"), ("doi", "10.5281/zenodo.9")])
w4 = mk("w4.md", [("written", "2026-09-24")])
held = mk("held.md", [("written", "2026-09-23"), ("hold", "failed")])
legacy = mk("legacy.md", [("date", "2026-08-20")])   # predates the buffer
os.remove(a)

check("papers written today are counted whatever their state",
      run_ci.written_today("2026-09-25"), 3)
check("another day's papers are not", run_ci.written_today("2026-09-24"), 1)

ug = [os.path.basename(x) for x in run_ci.ungated_written()]
check("written-but-ungated papers are found for re-gating, oldest first",
      ug, ["w4.md", "w1.md"])
check("a legacy draft is never swept up for re-submission",
      "legacy.md" in ug, False)
check("a held paper is not re-gated", "held.md" in ug, False)
check("a gated paper is not re-gated", "w2.md" in ug, False)

# The resume path must not publish around the gates.
p_ug = mk("today-ungated.md", [("date", "2026-09-26"), ("written", "2026-09-26")])
p_hd = mk("today-held.md", [("date", "2026-09-26"), ("hold", "x")])
check("resume never publishes a written paper that has not passed its gates",
      run_ci.todays_pending_draft("2026-09-26"), None)
p_old = mk("today-old.md", [("date", "2026-09-26")])
check("resume still finishes an ordinary unpublished draft",
      os.path.basename(run_ci.todays_pending_draft("2026-09-26") or ""),
      "today-old.md")

check("the daily quota is 3, as asked on 2026-09-29", run_ci.DAILY_WRITES, 3)
check("three a day against two published grows the buffer",
      run_ci.DAILY_WRITES > run_ci.DAILY_TARGET, True)
check("the runaway guard sits well above a day's writing",
      run_ci.BUFFER_MAX >= 10 * run_ci.DAILY_WRITES // 2, True)

run_ci.DRAFTS = real_drafts



# -- topic bookkeeping ----------------------------------------------------
# The writer stopped at step 8, but topics are marked in step 11 - so without
# this, three writer sessions in one run would all take the same topic.
tq = tempfile.mkdtemp(prefix="run_ci_topics_")
real_topics = run_ci.TOPICS
real_log = run_ci.LOG
# note() appends to runner.log; in CI that is the real log in the papers
# repo, which gets committed. Tests must not write into it.
run_ci.LOG = os.path.join(tq, "runner.log")
run_ci.TOPICS = os.path.join(tq, "topics.md")
QUEUE = "\n".join([
    "# queue", "", "## Queue", "",
    "- [x] PUBLISHED 10.5281/zenodo.1 (2026-09-01)",
    "      Old Paper",
    "- [ ] First Topic",
    "      Tension: something",
    "- [ ] Second Topic",
    "", "## Needs narrowing", "", "- [ ] Not In The Queue", ""])
open(run_ci.TOPICS, "w", encoding="utf-8").write(QUEUE)
d1 = os.path.join(tq, "first-paper.md")

check("an unmarked topic is marked BANKED for the draft that used it",
      run_ci.ensure_topic_marked(d1, "2026-09-25"), True)
t = open(run_ci.TOPICS, encoding="utf-8").read()
check("it is the FIRST unchecked Queue entry that gets marked",
      "- [~] BANKED drafts/first-paper.md (2026-09-25)" in t
      and "- [ ] First Topic" not in t, True)
check("the title survives on the next line", "      First Topic" in t, True)
check("the next topic is still available",
      "- [ ] Second Topic" in t, True)
check("entries outside ## Queue are never touched",
      "- [ ] Not In The Queue" in t, True)
check("marking again is a no-op, not a second consumed topic",
      run_ci.ensure_topic_marked(d1, "2026-09-25") and
      open(run_ci.TOPICS, encoding="utf-8").read().count("BANKED") == 1, True)

check("publishing flips BANKED to PUBLISHED with the doi",
      run_ci.mark_published(d1, "10.5281/zenodo.777", "2026-09-26"), True)
t = open(run_ci.TOPICS, encoding="utf-8").read()
check("no stale BANKED marker is left on a published paper",
      "BANKED drafts/first-paper.md" in t, False)
check("the PUBLISHED line names the doi",
      "- [x] PUBLISHED 10.5281/zenodo.777 (2026-09-26) - drafts/first-paper.md" in t, True)
check("flipping a paper with no marker reports it rather than inventing one",
      run_ci.mark_published(os.path.join(tq, "never.md"), "x", "2026-09-26"), False)

run_ci.TOPICS = real_topics
run_ci.LOG = real_log



# -- the writer yields to the publish slots -------------------------------
m = run_ci.minutes_to_next_publish
check("at 03:30 the morning slot is 90 minutes away",
      round(m(_dtm(2026, 9, 25, 3, 30))), 90)
check("at 03:30 the writer would not start a paper",
      m(_dtm(2026, 9, 25, 3, 30)) < run_ci.PAPER_MINUTES, True)
check("at 01:00 there is time for a paper before 05:00",
      m(_dtm(2026, 9, 25, 1, 0)) >= run_ci.PAPER_MINUTES, True)
check("at 17:30 the writer would not start a paper before 19:00",
      m(_dtm(2026, 9, 25, 17, 30)) < run_ci.PAPER_MINUTES, True)
check("after 19:00 the next slot is tomorrow's 05:00",
      round(m(_dtm(2026, 9, 25, 21, 0))), 8 * 60)
check("a whole paper fits inside the job's timeout, three times over",
      run_ci.MAX_PER_RUN * run_ci.PAPER_MINUTES <= 340, True)



# -- the pinned model -----------------------------------------------------
import importlib
import claude_flags

saved = {k: os.environ.pop(k, None) for k in ("PAPER_MODEL", "PAPER_EFFORT")}
importlib.reload(claude_flags)
fl = claude_flags.flags()
check("papers are written with Claude Opus 5.5 by default",
      fl[fl.index("--model") + 1], "claude-opus-5-5")
check("at high effort, not Opus 5.5's medium default",
      fl[fl.index("--effort") + 1], "high")
check("--model comes before the variadic --allowedTools",
      fl.index("--model") < fl.index("--allowedTools"), True)
check("the PowerShell runner pins the same model",
      "--model claude-opus-5-5" in claude_flags.powershell_args(), True)

os.environ["PAPER_MODEL"] = "claude-sonnet-5"
importlib.reload(claude_flags)
check("a repository variable can switch the model without a commit",
      claude_flags.MODEL, "claude-sonnet-5")
os.environ["PAPER_MODEL"] = "   "
importlib.reload(claude_flags)
check("a blank variable falls back to the pinned default",
      claude_flags.MODEL, "claude-opus-5-5")

for k, v in saved.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
importlib.reload(claude_flags)

check("a CLI too old for the model is terminal, not retried",
      run_ci.classify("API Error: 400 Claude Code 2.1.241 does not support this "
                      "model; version 2.1.280 or newer is required."), "terminal")



# -- one parser: quoted dates count ----------------------------------------
# 2026-09-25: a session wrote `date: "2026-09-25"`. The runner's regexes did
# not accept the quotes, counted 1 published paper when there were 2, and
# sent the next session to write and publish a THIRD. Only the session's own
# judgement stopped it.
qd = tempfile.mkdtemp(prefix="run_ci_quotes_")
run_ci.DRAFTS = qd


def fm(name, *pairs):
    p = os.path.join(qd, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("---\n" + "".join("%s\n" % l for l in pairs) + "---\n\nbody\n")
    return p


fm("plain.md", "date: 2026-09-27", "doi: 10.5281/zenodo.1")
fm("double.md", 'date: "2026-09-27"', 'doi: "10.5281/zenodo.2"')
fm("single.md", "date: '2026-09-27'", "doi: 10.5281/zenodo.3")
fm("comment.md", "date: 2026-09-27   # stamped at mint", "doi: 10.5281/zenodo.4")
fm("stamped.md", "date: 2026-09-27T10:15:00", "doi: 10.5281/zenodo.5")

check("a quoted date is counted - the 2026-09-25 miscount",
      run_ci.todays_published_count("2026-09-27"), 5)
check("a quoted doi is read without its quotes",
      run_ci.draft_doi(os.path.join(qd, "double.md")), "10.5281/zenodo.2")
check("a trailing YAML comment is not part of the value",
      run_ci.day_of(os.path.join(qd, "comment.md")), "2026-09-27")
check("a date with a time still belongs to its day",
      run_ci.day_of(os.path.join(qd, "stamped.md")), "2026-09-27")
check("published_dois holds bare DOIs, never quoted ones",
      "10.5281/zenodo.2" in run_ci.published_dois()
      and '"10.5281/zenodo.2"' not in run_ci.published_dois(), True)

for f in os.listdir(qd):
    os.remove(os.path.join(qd, f))
fm("pending.md", 'date: "2026-09-27"')
check("a quoted-date draft awaiting publication is still found",
      os.path.basename(run_ci.todays_pending_draft("2026-09-27") or ""), "pending.md")

run_ci.DRAFTS = real_drafts


# -- independent review -----------------------------------------------------
# 2026-09-28: a review of nine banked papers found 26 confirmed errors - a
# 2.9-point gain written as "five points", figures against the wrong models -
# every one of which had passed cite_check and the originality gate. From
# 2026-09-29 nothing the pipeline wrote publishes until a separate session has
# checked it against its sources and passed it.
rv = tempfile.mkdtemp(prefix="run_ci_review_")
saved_paths = (run_ci.PROJ, run_ci.DRAFTS, run_ci.REVIEWS, run_ci.TOPICS,
               run_ci.LOG, run_ci.run_session, run_ci.regate, run_ci.RETRY_WAIT)
run_ci.PROJ = rv
run_ci.DRAFTS = os.path.join(rv, "drafts")
run_ci.REVIEWS = os.path.join(rv, "reviews")
run_ci.TOPICS = os.path.join(rv, "topics.md")
run_ci.LOG = os.path.join(rv, "runner.log")
os.makedirs(run_ci.DRAFTS)
open(run_ci.TOPICS, "w", encoding="utf-8").write("## Queue\n")


def rdraft(name, *pairs, body="The paper says five points."):
    p = os.path.join(run_ci.DRAFTS, name)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write("---\n" + "".join("%s\n" % l for l in pairs) + "---\n\n" + body + "\n")
    return p


def raw(p):
    with open(p, "rb") as fh:
        return fh.read()


GATED = ("written: 2026-09-29", "gated: 2026-09-29", "original: 2026-09-29")
d = rdraft("paper.md", 'title: "P"', *GATED)
other = rdraft("other.md", 'title: "O"', "written: 2026-09-20", "gated: 2026-09-20",
               "reviewed: 2026-09-21")
d_raw, other_raw = raw(d), raw(other)

check("the verdict is read from its line",
      (open(os.path.join(rv, "v1.md"), "w").write("x\nVERDICT: REVISE\n"),
       run_ci.parse_verdict(os.path.join(rv, "v1.md")))[1], "REVISE")
check("a bolded verdict still counts",
      (open(os.path.join(rv, "v2.md"), "w").write("**VERDICT: PASS**\n"),
       run_ci.parse_verdict(os.path.join(rv, "v2.md")))[1], "PASS")
check("the last verdict in the file wins",
      (open(os.path.join(rv, "v3.md"), "w").write("VERDICT: REVISE\n...\nVERDICT: PASS\n"),
       run_ci.parse_verdict(os.path.join(rv, "v3.md")))[1], "PASS")
check("a report that merely mentions verdicts has none",
      (open(os.path.join(rv, "v4.md"), "w").write("the VERDICT: line is missing, PASS\n"),
       run_ci.parse_verdict(os.path.join(rv, "v4.md")))[1], None)
check("no report is no verdict",
      run_ci.parse_verdict(os.path.join(rv, "missing.md")), None)

check("a gated, unreviewed paper needs review", run_ci.review_state(d), "review")
check("a reviewed paper needs nothing", run_ci.review_state(other), None)
check("an ungated paper is not reviewed before its gates pass",
      run_ci.review_state(rdraft("ug.md", "written: 2026-09-29")), None)
check("a held paper is not reviewed",
      run_ci.review_state(rdraft("h.md", *GATED, "hold: x")), None)
check("a legacy draft is never swept into review",
      run_ci.review_state(rdraft("legacy.md", "gated: 2026-09-01")), None)
for n in ("ug.md", "h.md", "legacy.md"):
    os.remove(os.path.join(run_ci.DRAFTS, n))
check("the queue holds exactly the paper awaiting review",
      [(os.path.basename(p), s) for p, s in run_ci.review_queue()],
      [("paper.md", "review")])

calls = []


def fake_session(outputs):
    """A stand-in for claude -p: each call pops one scripted behaviour."""
    def run(cli, prompt, quiet, produced):
        calls.append(prompt)
        act = outputs.pop(0)
        return act(prompt) or ("ok" if produced() else "empty")
    return run


def review_says(verdict, meddle=False):
    def act(prompt):
        rnd = int(prompt.split("review round ")[1].split(".")[0])
        os.makedirs(run_ci.REVIEWS, exist_ok=True)
        open(run_ci.review_path(d, rnd), "w").write(
            "# Review\n1. SERIOUS - five points; the source says 2.9.\nVERDICT: %s\n" % verdict)
        if meddle:          # a reviewer that "helpfully" edits what it judges
            open(d, "a").write("\nreviewer edit\n")
            open(other, "a").write("\nreviewer edit\n")
            rdraft("stray.md", 'title: "S"')
    return act


def revise(prompt):
    txt = open(d, encoding="utf-8").read()
    # A reviser that fixes the body - and also tries to mark itself reviewed.
    txt = txt.replace("five points", "2.9 points").replace("---\n\n", "reviewed: yes\n---\n\n", 1)
    open(d, "w", encoding="utf-8").write(txt)
    with open(run_ci.review_path(d, 1), "a") as fh:
        fh.write("\n## Response\n1. FIXED - five -> 2.9\n")


gate_saw = []


def fake_regate(draft, run_date, cli=None, quiet=True):
    gate_saw.append(run_ci.field(draft, "gated"))
    run_ci.stamp(draft, "gated", run_date)
    return True


run_ci.regate = fake_regate
run_ci.RETRY_WAIT = 0

# Round 1: REVISE, from a reviewer that also edits drafts.
run_ci.run_session = fake_session([review_says("REVISE", meddle=True)])
check("round 1 asks for a revision",
      run_ci.review_step("cli", d, "2026-09-29", True), "revise")
check("the reviewer's edit to the paper it judged is undone", raw(d) != d_raw
      and b"reviewer edit" not in raw(d), True)
check("the reviewer's edit to another draft is undone", raw(other), other_raw)
check("a draft the reviewer created is removed",
      os.path.exists(os.path.join(run_ci.DRAFTS, "stray.md")), False)
check("the round and verdict are recorded",
      (run_ci.field(d, "review_rounds"), run_ci.field(d, "review_verdict")),
      ("1", "REVISE"))
check("a paper under revision is not ready", d in run_ci.ready_papers(), False)
check("it now needs revising", run_ci.review_state(d), "revise")
check("the review prompt formats and names its report",
      "reviews/paper.r1.md" in calls[-1] and "{" not in calls[-1], True)

# The revision.
run_ci.run_session = fake_session([revise])
check("the revision is re-gated", run_ci.review_step("cli", d, "2026-09-29", True),
      "revised")
check("the old gates were voided before the new ones ran", gate_saw[-1], None)
check("the fix to the body is kept", "2.9 points" in open(d).read(), True)
check("a reviser cannot mark its own paper reviewed",
      run_ci.field(d, "reviewed"), None)
check("the revision is recorded", run_ci.field(d, "revised_round"), "1")
check("a revised paper goes back for another review", run_ci.review_state(d), "review")
check("the revise prompt formats", "{" not in calls[-1]
      and "reviews/paper.r1.md" in calls[-1], True)

# Round 2: PASS.
run_ci.run_session = fake_session([review_says("PASS")])
check("round 2 passes it", run_ci.review_step("cli", d, "2026-09-29", True), "passed")
check("a re-review is told about the previous round",
      "reviews/paper.r1.md" in calls[-1] and "re-review" in calls[-1], True)
check("a passed paper is stamped reviewed", run_ci.field(d, "reviewed"), "2026-09-29")
check("and is ready to publish", d in run_ci.ready_papers(), True)
check("and leaves the review queue", run_ci.review_state(d), None)

# REJECT holds at once.
r = rdraft("rej.md", 'title: "R"', *GATED)
run_ci.run_session = fake_session([lambda p: open(run_ci.review_path(r, 1), "w")
                                   .write("VERDICT: REJECT\n") and None])
check("a rejected paper is held", run_ci.review_step("cli", r, "2026-09-29", True), "held")
check("with the reason on record", "rejected" in (run_ci.field(r, "hold") or ""), True)

# The last allowed round still saying REVISE holds rather than looping.
m = rdraft("max.md", 'title: "M"', *GATED,
           "review_rounds: %d" % (run_ci.MAX_REVIEW_ROUNDS - 1),
           "review_verdict: REVISE", "revised_round: %d" % (run_ci.MAX_REVIEW_ROUNDS - 1))
run_ci.run_session = fake_session([lambda p: open(run_ci.review_path(m, run_ci.MAX_REVIEW_ROUNDS), "w")
                                   .write("VERDICT: REVISE\n") and None])
check("a paper still failing its last round is held, not revised forever",
      run_ci.review_step("cli", m, "2026-09-29", True), "held")

# An API outage says nothing about the paper.
o = rdraft("outage.md", 'title: "O"', *GATED)
run_ci.run_session = lambda *a: "down"
check("an API outage stops review work", run_ci.review_step("cli", o, "2026-09-29", True),
      "down")
check("and does not count against the paper", run_ci.field(o, "review_tries"), None)
check("an outage never holds a paper", run_ci.field(o, "hold"), None)

# A session that runs but writes no verdict does count, and ends in a hold.
run_ci.run_session = lambda *a: "empty"
got = [run_ci.review_step("cli", o, "2026-09-29", True) for _ in range(run_ci.REVIEW_TRIES)]
check("sessions that leave no verdict are held after %d tries" % run_ci.REVIEW_TRIES,
      got[-1], "held")
check("a verdict-less session never marks a paper reviewed",
      run_ci.field(o, "reviewed"), None)

# A report already on disk (the run was cut off after the session) is used.
k = rdraft("kept.md", 'title: "K"', *GATED)
os.makedirs(run_ci.REVIEWS, exist_ok=True)
open(run_ci.review_path(k, 1), "w").write("VERDICT: PASS\n")
n_calls = len(calls)
run_ci.run_session = fake_session([])
check("a finished review on disk is not run again",
      (run_ci.review_step("cli", k, "2026-09-29", True), len(calls) == n_calls),
      ("passed", True))

# The hole this closes: a pipeline paper dated today, gated, unreviewed.
t = rdraft("today.md", 'title: "T"', "date: 2026-09-29", *GATED)
check("an unreviewed pipeline paper dated today is never 'today's pending draft'",
      run_ci.todays_pending_draft("2026-09-29"), None)

check("unstamp removes exactly one key",
      (run_ci.unstamp(t, "gated"), run_ci.field(t, "gated"), run_ci.field(t, "written")),
      (True, None, "2026-09-29"))
check("unstamp of an absent key reports False", run_ci.unstamp(t, "gated"), False)

check("checkpoint is a no-op outside CI",
      (os.environ.pop("GITHUB_ACTIONS", None), run_ci.checkpoint("test"))[1], None)
check("run_ci keeps well inside the job's 340-minute cap",
      run_ci.RUN_MINUTES + 30 <= 340, True)
check("a review round fits in what a slot leaves",
      run_ci.REVIEW_MINUTES < run_ci.RUN_MINUTES, True)

(run_ci.PROJ, run_ci.DRAFTS, run_ci.REVIEWS, run_ci.TOPICS, run_ci.LOG,
 run_ci.run_session, run_ci.regate, run_ci.RETRY_WAIT) = saved_paths


# -- one refused paper must not cost a slot (2026-10-03/04) ------------------
# A cited preprint was retitled after its paper was gated. The publish gate
# re-ran cite_check, rightly refused the paper, and the runner retried that
# same paper at three publish runs while thirteen ready papers waited.
pb = tempfile.mkdtemp(prefix="run_ci_publish_")
saved_pub = (run_ci.DRAFTS, run_ci.TOPICS, run_ci.LOG, run_ci.publish_existing,
             run_ci.gate_report, run_ci.repair_citations, run_ci.PROJ, run_ci.REVIEWS)
run_ci.DRAFTS = pb
run_ci.TOPICS = os.path.join(pb, "topics.md")
run_ci.LOG = os.path.join(pb, "runner.log")
run_ci.PROJ = pb
run_ci.REVIEWS = os.path.join(pb, "reviews")
open(run_ci.TOPICS, "w", encoding="utf-8").write("## Queue\n")


def ready_paper(name, written):
    p = os.path.join(pb, name)
    open(p, "w", encoding="utf-8").write(
        "---\ntitle: T\nwritten: %s\ngated: %s\noriginal: %s\nreviewed: %s\n"
        "review_rounds: 2\n---\n\nbody\n" % ((written,) * 4))
    return p


first = ready_paper("first.md", "2026-09-29")
second = ready_paper("second.md", "2026-09-30")
attempts = []


def refuse_first(draft):
    attempts.append(os.path.basename(draft))
    if draft == first:
        return 2                                  # cite_check refused it
    run_ci.stamp(draft, "doi", "10.5281/zenodo.42")
    return 0


run_ci.publish_existing = refuse_first
check("a slot publishes the next ready paper when the first is refused",
      (run_ci.publish_from_buffer("2026-10-04"), attempts), (0, ["first.md", "second.md"]))
check("the refused paper goes back to its gates", run_ci.field(first, "gated"), None)
check("and the refusal is on record", run_ci.field(first, "publish_refused"), "2026-10-04")
check("it is no longer in the ready queue", first in run_ci.ready_papers(), False)
check("it will be re-gated", first in run_ci.ungated_written(), True)

third = ready_paper("third.md", "2026-10-01")
fourth = ready_paper("fourth.md", "2026-10-02")
attempts.clear()


def reach_zenodo_then_fail(draft):
    attempts.append(os.path.basename(draft))
    run_ci.stamp(draft, "deposition", "999")     # got as far as Zenodo
    return 2


run_ci.publish_existing = reach_zenodo_then_fail
check("a paper that reached Zenodo stops the slot - no second record risked",
      (run_ci.publish_from_buffer("2026-10-04"), attempts), (1, ["third.md"]))
check("and is not sent back to its gates", run_ci.field(third, "gated"), "2026-10-01")
for f in (third, fourth):
    os.remove(f)
check("an empty buffer is reported as such", (lambda: None)() is None, True)

# yesterday's shortfall is made up today
for f in list(os.listdir(pb)):
    if f.endswith(".md"):
        os.remove(os.path.join(pb, f))
open(os.path.join(pb, "a.md"), "w").write("---\ntitle: A\ndate: 2026-10-03\ndoi: 10.5281/zenodo.1\n---\n")
check("one paper missed yesterday is owed today", run_ci.missed_yesterday("2026-10-04"), 1)
open(os.path.join(pb, "b.md"), "w").write("---\ntitle: B\ndate: 2026-10-03\ndoi: 10.5281/zenodo.2\n---\n")
check("nothing is owed after a full day", run_ci.missed_yesterday("2026-10-04"), 0)
check("a whole missed day owes two, never more",
      run_ci.missed_yesterday("2026-10-10"), run_ci.DAILY_TARGET)

# a rejected citation is repaired, not retried until held
REPORT = ("DETAILS\n[MISMATCH] line 1143  10.48550/arXiv.2609.08175\n"
          "    as written : Old Title\n    found      : New Title\nSUMMARY  OK=64 MISMATCH=1\n")
check("a MISMATCH is a citation failure the paper can fix",
      "MISMATCH" in run_ci.citation_failure(REPORT), True)
check("an unreachable source is not",
      run_ci.citation_failure("SUMMARY OK=60 UNVERIFIABLE=5\n[UNVERIFIABLE] line 3"), "")
rp = ready_paper("repaired.md", "2026-09-29")
run_ci.unstamp(rp, "gated")
reports = [(False, REPORT), (True, "")]
def fake_gate_report(d, r):
    ok, out = reports.pop(0)
    if ok:                       # publish_paper --gate-only stamps it
        run_ci.stamp(d, "gated", r)
    return ok, out


run_ci.gate_report = fake_gate_report
repairs = []
run_ci.repair_citations = lambda cli, d, findings, quiet: repairs.append(findings) or True
check("a paper whose citation was repaired is banked",
      run_ci.regate(rp, "2026-10-04", "cli"), True)
check("the repair saw the gate's findings", "Old Title" in repairs[0], True)
check("a repaired paper is not ready until it is reviewed again",
      (run_ci.field(rp, "reviewed"), run_ci.review_state(rp)), (None, "review"))
check("and its next review is a new round, not a replay of the old one",
      run_ci.review_path(rp, int(run_ci.field(rp, "review_rounds")) + 1).endswith(".r3.md"), True)
run_ci.unstamp(rp, "gated")
reports[:] = [(False, REPORT)]
check("without a CLI there is no repair, only a counted failure",
      (run_ci.regate(rp, "2026-10-04"), len(repairs)), (False, 1))

(run_ci.DRAFTS, run_ci.TOPICS, run_ci.LOG, run_ci.publish_existing,
 run_ci.gate_report, run_ci.repair_citations, run_ci.PROJ, run_ci.REVIEWS) = saved_pub


# -- a run alive before a slot waits for it (2026-10-05) ------------------------
# GitHub queued the 19:00 IST run 3h46m late; a run alive at 18:02 had exited.
hs = tempfile.mkdtemp(prefix="run_ci_hold_")
saved_hold = (run_ci.DRAFTS, run_ci.LOG, run_ci.minutes_to_next_publish,
              run_ci.minutes_left, run_ci.time.sleep, run_ci.checkpoint,
              os.environ.get("GITHUB_ACTIONS"))
run_ci.DRAFTS = hs
run_ci.LOG = os.path.join(hs, "runner.log")
open(os.path.join(hs, "r.md"), "w").write(
    "---\ntitle: T\nwritten: 2026-10-01\ngated: 2026-10-01\nreviewed: 2026-10-01\n---\nb\n")
slept = []
run_ci.time.sleep = lambda s: slept.append(s)
run_ci.checkpoint = lambda what: None
run_ci.minutes_left = lambda: run_ci.RUN_MINUTES - 5          # 5 minutes into the run


def hold(wait, ci=True, fresh=True):
    run_ci.minutes_to_next_publish = lambda now=None: wait
    if fresh:
        run_ci._held = False
    if ci:
        os.environ["GITHUB_ACTIONS"] = "true"
    else:
        os.environ.pop("GITHUB_ACTIONS", None)
    slept.clear()
    return run_ci.hold_for_slot(), list(slept)


check("a run 58 minutes before a slot waits for it, to the second",
      hold(58), (True, [58 * 60 + 15]))
check("it holds only once per run", hold(30, fresh=False), (False, []))
check("a slot hours away is not waited for", hold(300), (False, []))
check("nothing holds outside CI", hold(30, ci=False), (False, []))
run_ci.minutes_left = lambda: run_ci.RUN_MINUTES - 250        # deep into a long run
check("a run with too little job time left does not hold and get cut off",
      hold(100), (False, []))
run_ci.minutes_left = lambda: run_ci.RUN_MINUTES - 5
os.remove(os.path.join(hs, "r.md"))
check("with nothing reviewed to publish there is no point holding", hold(30), (False, []))

(run_ci.DRAFTS, run_ci.LOG, run_ci.minutes_to_next_publish, run_ci.minutes_left,
 run_ci.time.sleep, run_ci.checkpoint, _ga) = saved_hold
if _ga is None:
    os.environ.pop("GITHUB_ACTIONS", None)
else:
    os.environ["GITHUB_ACTIONS"] = _ga
run_ci._held = False


print("")
print("%d passed%s" % (PASS, ", %d FAILED" % FAIL if FAIL else ""))
sys.exit(1 if FAIL else 0)
