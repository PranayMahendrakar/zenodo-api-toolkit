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

check("every DOI on disk is collected",
      run_ci.published_dois(),
      {"10.5281/zenodo.22884949", "10.5281/zenodo.22840565"})

run_ci.DRAFTS = real_drafts

print("")
print("%d passed%s" % (PASS, ", %d FAILED" % FAIL if FAIL else ""))
sys.exit(1 if FAIL else 0)
