r"""The CLI flags an unattended pipeline run needs.

Headless `claude -p` starts with restrictive permissions: no file writes, no
network. The pipeline needs both - it writes drafts and logs, and it verifies
every citation against arXiv and Crossref. Without these flags the run refuses
to draft (correctly, since DAILY_RUN.md forbids citing from memory) and exits.

Kept in one place so run_daily.ps1, go.py and run_now.py cannot drift apart.

Scope, deliberately: file tools are allowed because the job writes drafts,
daily_log.md and topics.md. Bash is limited to python, so the run can invoke
cite_check.py / publish_paper.py but not arbitrary shell commands. Widen it
only if a run reports a denied tool - go.log records what was refused.
"""

import os

ALLOWED_TOOLS = [
    "Read", "Glob", "Grep",
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "WebFetch", "WebSearch",
    "TodoWrite", "Task",
    # python3 as well as python. On Linux the model reaches for python3 by
    # instinct; without it the call is refused, and on 2026-09-24 the run read
    # one refusal as "this session blocks all code execution" and gave up on
    # cite_check, publish_paper and matplotlib for the whole paper. The
    # interpreter it happened to name should not decide whether the gates run.
    "Bash(python:*)", "Bash(py:*)", "Bash(python3:*)",
]

PERMISSION_MODE = "acceptEdits"

# The model that writes the papers - pinned, not inherited.
#
# Unpinned, `claude -p` used whatever default the plan had, and CI installs
# the newest Claude Code on every run, so the model writing papers under the
# author's name could change after an update with nobody noticing. Chosen by
# the author on 2026-09-25. Override without a commit through the repository
# variable PAPER_MODEL (GitHub: Settings -> Secrets and variables -> Variables).
# Not CLAUDE_*: that namespace is Claude Code's own - it exports CLAUDE_EFFORT
# itself - so a variable there would silently inherit the CLI's setting.
MODEL = os.environ.get("PAPER_MODEL", "").strip() or "claude-opus-5-5"

# Opus 5.5 defaults to medium effort, one level below Opus 5. Research writing
# is intelligence-sensitive, so ask for high explicitly rather than inherit.
EFFORT = os.environ.get("PAPER_EFFORT", "").strip() or "high"


def flags():
    """Flag list to splice into a claude -p invocation."""
    # --model and --effort go BEFORE --allowedTools, which is variadic and
    # would otherwise be the first place a mis-ordered flag gets swallowed.
    return ["--model", MODEL, "--effort", EFFORT,
            "--permission-mode", PERMISSION_MODE,
            "--allowedTools"] + ALLOWED_TOOLS


def powershell_args():
    """Same flags rendered for run_daily.ps1."""
    quoted = " ".join("'%s'" % t for t in ALLOWED_TOOLS)
    return "--model %s --effort %s --permission-mode %s --allowedTools %s" % (
        MODEL, EFFORT, PERMISSION_MODE, quoted)


if __name__ == "__main__":
    print(" ".join(flags()))
