r"""Standing record values for this author's papers.

Precedence when publish_paper.py builds a Zenodo record:

    CLI flag  >  the paper's own front matter  >  these defaults  >  nothing

These are deliberate standing choices, not silent guesses, and the confirmation
block labels anything sourced here as "(from paper_defaults.py)" so it is still
visible before the DOI is minted. Change a value here and every future paper
picks it up; override one paper by putting the key in its front matter.

Set a value to "" to fall back to publish_paper.py's conservative behaviour
(publication_type "preprint", no journal, copyright = author).
"""

DEFAULTS = {
    "author": "Mahendrakar, Pranay",
    "copyright": "Pranay Mahendrakar",
    "orcid": "0009-0003-7224-029X",
    "affiliation": "SONYTECH",
    "license": "CC-BY-4.0",

    # The author publishes through his own journal, Life of Research.
    # publication_type "article" is what makes Zenodo show "Journal article"
    # rather than "Preprint"; journal_title is what names the venue.
    "publication_type": "article",
    "journal_title": "Life of Research",

    # Left empty on purpose: inventing a volume, issue or page range would be
    # asserting something untrue about a venue that does not paginate.
    "journal_volume": "",
    "journal_issue": "",
    "journal_pages": "",

    "version": "1.0",
    "upload_type": "publication",
    "access_right": "open",
}


def get(key):
    """The standing default for a key, or "" if there isn't one."""
    return DEFAULTS.get(key, "") or ""


# ---------------------------------------------------------------------------
# Unattended publishing
# ---------------------------------------------------------------------------
# True  -> the 05:00 job publishes the paper it wrote, no confirmation.
# False -> it stages an unpublished draft and stops.
#
# When True, these gates still run and still stop the publish if they fail:
#   * every citation must resolve AND match its title (cite_check.py)
#   * no scaffold placeholder text may survive into any record field
#   * no deposition on the account may already carry this title
#   * the topic must have passed the writability test
#
# What no gate can check is whether the paper is worth publishing. A minted
# DOI cannot be withdrawn. Flip this to False to go back to review-first.
AUTO_PUBLISH = True


if __name__ == "__main__":
    width = max(len(k) for k in DEFAULTS)
    for k in sorted(DEFAULTS):
        print("%-*s : %s" % (width, k, DEFAULTS[k] or "(none)"))
