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


p_ready_old = paper("ready-old.md", written="2026-09-20", gated="2026-09-20")
p_ready_new = paper("ready-new.md", written="2026-09-23", gated="2026-09-23")
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

check("the daily quota is 3 to 4, as asked", run_ci.DAILY_WRITES in (3, 4), True)
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


print("")
print("%d passed%s" % (PASS, ", %d FAILED" % FAIL if FAIL else ""))
sys.exit(1 if FAIL else 0)
