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

ALLOWED_TOOLS = [
    "Read", "Glob", "Grep",
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "WebFetch", "WebSearch",
    "TodoWrite", "Task",
    "Bash(python:*)", "Bash(py:*)",
]

PERMISSION_MODE = "acceptEdits"


def flags():
    """Flag list to splice into a claude -p invocation."""
    return ["--permission-mode", PERMISSION_MODE,
            "--allowedTools"] + ALLOWED_TOOLS


def powershell_args():
    """Same flags rendered for run_daily.ps1."""
    quoted = " ".join("'%s'" % t for t in ALLOWED_TOOLS)
    return "--permission-mode %s --allowedTools %s" % (PERMISSION_MODE, quoted)


if __name__ == "__main__":
    print(" ".join(flags()))
