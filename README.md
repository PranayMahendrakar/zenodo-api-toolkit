# Zenodo API Toolkit

Python tooling for publishing to [Zenodo](https://zenodo.org) through its REST
API, plus the citation checker and unattended runner built around it.

Targets production (`https://zenodo.org`) by default; set
`ZENODO_BASE=https://sandbox.zenodo.org` to work against sandbox. Sandbox and
production have separate accounts and separate tokens.

```bash
export ZENODO_TOKEN="..."    # zenodo.org/account/settings/applications/tokens/new/
python check_token.py        # verify it works
```

Scopes needed: `deposit:write`, plus `deposit:actions` for anything that
publishes.

## Citation checking

`cite_check.py` is the piece most likely to be useful on its own. It extracts
every reference from a Markdown document and resolves each one against arXiv,
DataCite and Crossref, then compares the title it gets back against the title
the document claims.

```bash
python cite_check.py paper.md
python cite_check.py paper.md --verbose
```

It is built to fail loudly rather than quietly:

| Outcome | Meaning |
|---|---|
| `OK` | the identifier resolves and the title matches |
| `NOT-FOUND` | the identifier resolves to nothing — very likely fabricated |
| `MISMATCH` | it resolves, but to a different paper than the one cited |
| `UNVERIFIABLE` | no source could be reached, so nothing was checked |

`UNVERIFIABLE` is a failure, not a pass. A checker that cannot reach its
sources has not verified anything, and saying so is the entire point. When
arXiv is unreachable the lookup falls back to the paper's DataCite DOI
(`10.48550/arXiv.<id>`) — a second route to the same check, not a way around
it: the title still has to match and a fabricated identifier still fails.

Crossref offers a "polite pool" with better rate limits to callers who send a
contact address. Set `CROSSREF_MAILTO` to opt in; without it the lookups use
the anonymous pool and work the same, just with tighter limits.

## Publishing

| Script | Writes to Zenodo? | What it does |
|---|---|---|
| `check_token.py` | no | verify the token, list depositions |
| `zenodo_search.py` | no | search records |
| `zenodo_stats.py` | no | views and downloads per record |
| `zenodo_backup.py` | no | download every record you own |
| `zenodo_upload.py` | yes | create a deposition and upload files |
| `publish_paper.py` | yes | the full path: metadata, upload, mint a DOI |
| `zenodo_version.py` | yes | new version of an existing record |
| `update_record_meta.py` | yes | edit metadata on a published record |
| `delete_draft.py` | yes | delete an *unpublished* draft |

`publish_paper.py` will not publish past its own guards. It refuses on a
duplicate title, on a failed citation check, and when it cannot confirm either
— an unreachable deposition listing is treated as "unknown", never as "fine".
**A published DOI cannot be withdrawn**, so it prints exactly what will become
permanent and waits for confirmation unless given `--yes`.

`paper_defaults.py` holds the standing record values (author, ORCID, licence,
venue). Precedence is: CLI flag → the paper's front matter → these defaults.

## Unattended runs

`run_ci.py` drives one full drafting run and is the piece that runs on a
schedule. It exists because the same three judgements kept going wrong:

- **a finished day is a no-op** — a redundant launch costs a tenth of a second
  instead of an hour;
- **a session is judged by what it left behind**, not by how its transcript
  read. A run that ends politely having produced nothing is not a success, and
  retrying after a draft exists would start a *second* paper rather than
  finish the first;
- **authentication failures are never retried** — no amount of waiting fixes a
  login.

```bash
python run_ci.py                # draft, gate, publish
python run_ci.py --stage-only   # draft and gate, publish nothing
python run_ci.py --check        # report what it would do, run nothing
python run_ci.py --quiet        # keep the transcript out of stdout
```

`--stage-only` is for the first run in a new environment: a DOI cannot be
withdrawn, so the first paper a machine produces should be read by a human
before it becomes permanent. `--quiet` keeps the draft out of stdout when
stdout is a public CI log; it still goes to `runner.log`.

The runner expects a working directory containing `DAILY_RUN.md` (the
procedure it follows), `topics.md`, and a `drafts/` directory. Those are
content, not tooling, and live wherever you keep yours — see
`.github/workflows/daily-paper.yml` for how the scheduled run assembles the
two halves.

## Requirements

Python 3.9+, plus `requests`, `pymupdf` and `markdown`. PDF rendering goes
through PyMuPDF's Story engine, so there is no LaTeX or pandoc dependency.

## Licence

MIT.
