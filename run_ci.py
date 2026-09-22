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
killed unfinished. There is nobody to report back to. Work through the steps in
the foreground and finish only once the paper is actually on disk.
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
killed unfinished. There is nobody to report back to. Work through the steps in
the foreground and finish only once the paper is actually on disk.
"""


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
    """First 60 lines - enough for the front matter, cheap on a 10k-word file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(next(fh, "") for _ in range(60))
    except OSError:
        return ""


def published_dois() -> set[str]:
    out = set()
    for f in drafts_md():
        m = re.search(r"(?m)^doi:\s*(\S+)", front_matter(f))
        if m:
            out.add(m.group(1))
    return out


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
    proc = subprocess.run([sys.executable, "publish_paper.py", rel, "--publish", "--yes"],
                          cwd=PROJ)
    return proc.returncode


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage-only", action="store_true",
                    help="draft and gate, but publish nothing")
    ap.add_argument("--check", action="store_true",
                    help="report what would happen; run nothing")
    ap.add_argument("--quiet", action="store_true",
                    help="keep the session transcript out of stdout; it still "
                         "goes to runner.log. Use this when stdout is a public "
                         "CI log and the paper is not public yet.")
    args = ap.parse_args(argv)

    note("----- run starting%s -----" % (" (stage-only)" if args.stage_only else ""))

    already = todays_published_doi()

    # --check reports state and stops. It runs before the completion guard on
    # purpose: on a finished day the guard would exit first, and a diagnostic
    # that cannot tell you anything on the days you most want to ask is no
    # diagnostic at all.
    if args.check:
        note("check: cli              = %s" % (find_cli() or "NOT FOUND"))
        note("check: zenodo token     = %s" % ("set" if os.environ.get("ZENODO_TOKEN")
                                               or os.environ.get("ZENODO_ACCESS_TOKEN") else "MISSING"))
        note("check: claude credential= %s" % ("oauth" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                                               else "api key" if os.environ.get("ANTHROPIC_API_KEY")
                                               else "MISSING"))
        note("check: drafts           = %d (%d carry a DOI)"
             % (len(drafts_md()), len(published_dois())))
        note("check: today published  = %s" % (already or "no"))
        note("check: today pending    = %s"
             % (os.path.basename(todays_pending_draft() or "") or "no"))
        note("check: mode             = %s" % ("stage-only" if args.stage_only else "publish"))
        if already and not args.stage_only:
            would = "skip (finished day)"
        elif todays_pending_draft() and not args.stage_only:
            would = "publish today's existing draft (no new paper)"
        else:
            would = "write a new paper"
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
    if already and not args.stage_only:
        note("Today's paper is already published: %s" % already)
        note("        https://doi.org/%s" % already)
        note("        Nothing to do - a second launch on a finished day is a no-op.")
        note("----- run finished -----")
        return 0

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

    # Retry until published, cheaply. A day that drafted but did not publish
    # needs its last step run, not another paper. Skipped in stage-only mode,
    # where not publishing is the whole point.
    pending = todays_pending_draft()
    if pending and not args.stage_only:
        rc = publish_existing(pending)
        after = todays_published_doi()
        if after:
            note("OK: published %s - https://doi.org/%s" % (after, after))
            note("----- run finished -----")
            return 0
        note("FAILED: could not publish today's existing draft (exit %d)." % rc)
        note("        The gates above say why. Nothing was written twice.")
        note("----- run finished -----")
        return 1

    prompt = PROMPT_STAGE if args.stage_only else PROMPT_PUBLISH
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
            if not args.quiet:
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
