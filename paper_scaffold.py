r"""
Generate a structured working draft skeleton so you never start from a blank page.

Each genre produces a different Markdown skeleton: YAML front-matter, one section
per skeleton entry, and an HTML comment under every heading stating that section's
word budget, its rhetorical job, and the questions the author has to answer there.
Those comments are working notes and are meant to be deleted before publication.

    python paper_scaffold.py "Topic title here" --genre agenda --out drafts/my-paper.md
    python paper_scaffold.py "Topic" --genre survey --authors "Doe, Jane" --keywords "a, b"
    python paper_scaffold.py --list
    python paper_scaffold.py --status drafts/my-paper.md
    python paper_scaffold.py --check  drafts/my-paper.md

Genres: agenda | survey | position | analysis | empirical

--check is the pre-flight. It reads a draft's front matter and prints every field
that will end up on the permanent Zenodo record, with its value and an
OK / DEFAULT / MISSING / INVALID status, so nothing reaches a minted DOI without
the author having seen it first. It exits 1 when

    - a required field (title, author, date, license, copyright) is absent;
    - a required field is still carrying this generator's placeholder, which is
      a decision nobody made and is what the silent default looked like;
    - publication_type is not a value Zenodo accepts;
    - the Abstract still holds a scaffold note or the bare body stub, because
      publish_paper.py copies that section into the record description as it
      stands.

Run it before publish_paper.py.

Every field that lands on a permanent public record is declared in the front
matter of the draft itself. There are no silent defaults here: publication_type,
copyright, version, orcid, affiliation and the journal_* fields are written into
the generated skeleton so they are a conscious decision on day one rather than
whatever the publishing script happened to hardcode on the day the DOI was minted.

WORD BUDGETS ARE TUNABLE CONSTANTS. Everything numeric lives at the top of this
file in GENRE_TARGETS (total prose target per genre) and in the per-section
"weight" values inside SKELETONS. Weights are relative, so changing a genre's
target rescales every section in it automatically; changing one section's weight
re-slices that genre only. Sections marked "fixed" (abstracts) do not scale.
The defaults here are derived from the section-proportion tables in
PAPER_STRUCTURE.md, scaled down from the source papers' real lengths to a
working default of 6,000-12,000 words. They are opinions, not laws -- override
any single draft with --target without editing the file.

--status re-reads a generated draft and prints a progress table. It counts real
prose only: HTML comments, fenced code blocks, headings and the reference list
are all excluded, so the numbers cannot be inflated by the scaffolding itself.

The empirical genre emits a warning block, because that genre cannot be written
from a topic alone -- it requires data you actually collected. Reporting results
you did not obtain is fabrication, not drafting.

No network access. The only file written is the draft named by --out, and an
existing file is never overwritten without --force.
"""
import os
import re
import sys
import argparse
import datetime
import unicodedata

# --------------------------------------------------------------------------
# TUNABLE CONSTANTS
# --------------------------------------------------------------------------

# Total prose word target per genre. Excludes the reference list, which is not
# prose and is not budgeted anywhere in this tool.
GENRE_TARGETS = {
    "agenda":    6000,
    "survey":   12000,
    "position":  7000,
    "analysis":  8000,
    "empirical": 8000,
}

# Layout of the printed --status table.
COL_SECTION = 40
BAR_WIDTH = 24
ROW_BAR_WIDTH = 10

DEFAULT_LICENSE = "CC-BY-4.0"
DEFAULT_VERSION = "1.0"

# Zenodo's closed vocabulary for upload_type "publication". A publication_type
# outside this list is rejected by the API, so --check treats it as fatal.
ZENODO_PUBLICATION_TYPES = [
    "annotationcollection", "article", "book", "conferencepaper",
    "datamanagementplan", "deliverable", "milestone", "other", "patent",
    "preprint", "proposal", "report", "section", "softwaredocumentation",
    "taxonomictreatment", "technicalnote", "thesis", "workingpaper",
]

# The journal_* fields only mean anything to Zenodo when publication_type is
# "article". Set anywhere else they are silently ignored on the record.
JOURNAL_FIELDS = ["journal_title", "journal_volume", "journal_issue", "journal_pages"]

# publication_type per genre.
#
# These used to be "conservative" - workingpaper / report / preprint, on the
# reasoning that "article" claims a journal and so should never be a default.
# That reasoning does not apply here: the author publishes through his own
# journal, Life of Research, so "article" plus journal_title is the accurate
# description of where these papers appear, not an unsupported claim. The
# values come from paper_defaults.py so the scaffold, the renderer and the
# uploader cannot disagree about what a paper is.
#
# Genre still varies the SKELETON - sections, word budgets, what each section
# must answer. It just no longer varies the venue.
try:
    import paper_defaults as _pd
    _STANDING_TYPE = _pd.get("publication_type") or "article"
except Exception:
    _STANDING_TYPE = "article"

GENRE_PUBLICATION_TYPE = {
    "agenda":    _STANDING_TYPE,
    "position":  _STANDING_TYPE,
    "analysis":  _STANDING_TYPE,
    "survey":    _STANDING_TYPE,
    "empirical": _STANDING_TYPE,
}

# Used when the genre is one this file does not know about - a hand-written
# draft can carry any genre string. It is only ever a suggestion printed to
# the author, never written to a file.
UNKNOWN_GENRE_PUBLICATION_TYPE = _STANDING_TYPE


def publication_type_for(genre):
    """The declared default for a genre this generator does know about.

    Deliberately not a .get() with a fallback: a genre added to SKELETONS and
    forgotten here must fail loudly at generation time, not quietly stamp some
    unrelated type onto a permanent record.
    """
    if genre not in GENRE_PUBLICATION_TYPE:
        die("genre '%s' has no entry in GENRE_PUBLICATION_TYPE. Add one rather "
            "than letting the publication_type default to something nobody "
            "chose -- it goes on a permanent record." % genre)
    return GENRE_PUBLICATION_TYPE[genre]


# Placeholder strings the generator writes. --check reports any field still
# carrying one of these as DEFAULT rather than OK: present, but never decided.
ORCID_PLACEHOLDER = "0000-0000-0000-0000"
AUTHOR_PLACEHOLDER = "LASTNAME, Firstname"
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

# Fields that must be present before a DOI is minted. Anything here missing
# makes --check exit 1.
REQUIRED_FIELDS = ["title", "author", "date", "license", "copyright"]

# What --check prints, in order, as (field, required).
#   title, author, date, license   -> straight onto the record
#   copyright                      -> the record AND the frozen PDF
#   publication_type               -> Zenodo's closed vocabulary, above
#   version, orcid, affiliation    -> record metadata, editable after publish
#   journal_*                      -> only meaningful when the type is article
#   keywords, ai_assistance        -> record keywords and the disclosure line
#   genre                          -> scaffold bookkeeping, never sent to Zenodo
CHECK_FIELDS = [
    ("title",            True),
    ("author",           True),
    ("date",             True),
    ("license",          True),
    ("copyright",        True),
    ("publication_type", False),
    ("version",          False),
    ("orcid",            False),
    ("affiliation",      False),
    ("journal_title",    False),
    ("journal_volume",   False),
    ("journal_issue",    False),
    ("journal_pages",    False),
    ("keywords",         False),
    ("ai_assistance",    False),
    ("genre",            False),
]

# The permanent-record warning pasted under the front matter of every draft.
# NB: no literal "-->" may appear anywhere inside this block.
PERMANENT_RECORD_NOTE = """<!--
  PERMANENT RECORD FIELDS -- working note, delete before publication.

  The front matter above is not decoration. Every field in the block marked
  "permanent record" is copied onto a public Zenodo record and stamped with a
  DOI. Decide each one now, while it is still free to change.

  FROZEN AT PUBLISH TIME, FOREVER:
    - the PDF file itself. A published file can never be replaced, only
      superseded by a whole new version with a new DOI. Anything that must
      appear in the document -- the copyright line, the author affiliation,
      the version number -- has to be in the Markdown BEFORE it is rendered.
    - the DOI, and the fact that the record exists. Records cannot be deleted.

  EDITABLE AFTER PUBLICATION (edit, update, publish again):
    - title, description, creators, keywords, license, publication_type,
      journal_title / volume / issue / pages, version.
    Editable is not the same as harmless: the wrong value is public, indexed
    and cited from the moment the DOI is minted.

  BEFORE PUBLISHING, RUN THE PRE-FLIGHT:
    python paper_scaffold.py --check <this file>

  It prints every field above with an OK / DEFAULT / MISSING status. A DEFAULT
  is a decision you have not made yet, and on a required field it fails the
  check exactly as a missing value does.

  publication_type must be one of Zenodo's values:
    article, report, workingpaper, preprint, technicalnote, conferencepaper,
    thesis, book, section, patent, deliverable, milestone, proposal,
    softwaredocumentation, taxonomictreatment, datamanagementplan,
    annotationcollection, other.
  The journal_* fields are only meaningful when it is "article".
-->"""

# The default disclosure string. Normal scholarly practice, and Zenodo metadata
# can carry it in the description or in a custom field. The author is expected to
# edit it to match what actually happened.
DEFAULT_AI_ASSISTANCE = (
    "EDIT THIS. Draft scaffolding and copy-editing used an AI assistant. "
    "All claims, citations, quotations and numbers were checked by the authors "
    "against the primary sources. No result, measurement or citation in this "
    "paper was produced by a language model."
)

# --------------------------------------------------------------------------
# SKELETONS
#
# Each section carries:
#   title    -- the heading that goes into the draft
#   fixed    -- an absolute word budget that does NOT scale with the target, or
#   weight   -- a relative share of whatever the target leaves after fixed
#   job      -- the rhetorical job of the section, in one or two sentences
#   ask      -- 2-4 questions the author must answer inside that section
#
# The "weight" numbers are the measured section lengths of the model paper named
# in "model", so the internal proportions survive rescaling to any target.
# --------------------------------------------------------------------------

SKELETONS = {

    "agenda": {
        "label": "Research agenda / taxonomy",
        "model": "Concrete Problems in AI Safety (~16,870 words)",
        "needs": "Literature, plus original conceptual work: a stipulated term, "
                 "a partition of the problem space, and concrete proposed "
                 "experiments. No data required.",
        "sections": [
            {
                "title": "Abstract",
                "fixed": 160,
                "job": "Five sentences. One of them must carry the whole taxonomy "
                       "-- the number of causes and the number of problems -- so a "
                       "reader who stops here still leaves with the tree.",
                "ask": [
                    "What is the one generative question the whole taxonomy falls out of?",
                    "Can you state the partition (N causes, M problems) in a single sentence?",
                    "Does any sentence here promise results? It must not -- this genre has none.",
                ],
            },
            {
                "title": "Introduction",
                "weight": 555,
                "job": "Position the paper twice: against adjacent-but-different "
                       "agendas, and against the adjacent-but-discredited one. Do "
                       "NOT explain the taxonomy here; that is the next section's job.",
                "ask": [
                    "Which neighbouring agenda will readers confuse this with, and what is the difference in one sentence?",
                    "Which discredited or dismissed version of this topic must you separate yourself from?",
                    "What is the numbered roadmap of the rest of the paper?",
                ],
            },
            {
                "title": "Overview of the problem space",
                "weight": 1200,
                "job": "Derive the taxonomy from a single generative question, "
                       "install a running example that mirrors the problem sections "
                       "one-for-one, and close with an explicit scope-and-depth "
                       "disclaimer saying what you left out and why.",
                "ask": [
                    "What is the running example, and does it produce one bullet per problem section?",
                    "Why this partition and not an obvious alternative one?",
                    "What is deliberately out of scope, and why is that defensible?",
                    "Are your sections deliberately uneven in length? Explain the unevenness here.",
                ],
            },
            {
                "title": "Problem 1: <name the problem>",
                "weight": 2010,
                "job": "Template, used identically for every problem section: "
                       "scenario -> why the obvious fix fails -> bulleted candidate "
                       "approaches, each one self-attacked -> honest summary -> a "
                       "'Potential experiments' block someone else could start on Monday.",
                "ask": [
                    "What concrete scenario makes this problem visible to a sceptic?",
                    "What is the obvious fix, and exactly why is it insufficient?",
                    "For each candidate approach, what is its strongest objection? State it yourself.",
                    "What is the smallest experiment that would make progress here?",
                ],
            },
            {
                "title": "Problem 2: <name the problem>",
                "weight": 2615,
                "job": "Same template. This is typically the longest section because "
                       "it is the credibility anchor -- the one where prior work is "
                       "richest and you must show you have read all of it.",
                "ask": [
                    "What concrete scenario makes this problem visible?",
                    "Which literature is mature here, and does your coverage prove you know it?",
                    "Which candidate approach do you actually believe in, and what would falsify it?",
                    "What is the smallest experiment that would make progress here?",
                ],
            },
            {
                "title": "Problem 3: <name the problem>",
                "weight": 1575,
                "job": "Same template. A short problem section is fine when prior "
                       "work is mature -- but say that is why it is short.",
                "ask": [
                    "What concrete scenario makes this problem visible?",
                    "Is this section short because the problem is easy, or because it is neglected? Say which.",
                    "What is the smallest experiment that would make progress here?",
                ],
            },
            {
                "title": "Related efforts",
                "weight": 595,
                "job": "Placed at the END, and organized by community rather than by "
                       "topic. One bullet per community: what it achieved, and its "
                       "specific blind spot.",
                "ask": [
                    "Which research communities are already working on parts of this?",
                    "For each: what is their best result, and what do they systematically miss?",
                    "Why is your partition an addition rather than a rebranding?",
                ],
            },
            {
                "title": "Conclusion",
                "weight": 250,
                "job": "Two paragraphs, no new content. Restate the stipulated "
                       "definition and the count of problems. Nothing else.",
                "ask": [
                    "Does this introduce any claim not already argued above? Delete it if so.",
                    "Is the definition stated here word-identical to the one in the overview?",
                ],
            },
        ],
    },

    "survey": {
        "label": "Systematic survey",
        "model": "A Survey of Large Language Models (~90,600 words)",
        "needs": "Literature at scale, plus a defensible and stated selection "
                 "criterion. Optional small original experiments, which must be "
                 "framed as incomplete if they are.",
        "sections": [
            {
                "title": "Abstract",
                "fixed": 200,
                "job": "Name the aspects the survey will cover, in the exact order "
                       "they appear as sections. That list is a contract you must "
                       "honour literally, and repeat unchanged in the introduction "
                       "and the conclusion.",
                "ask": [
                    "What are the N aspects, in order? Write them once and never reorder or rename them.",
                    "What is the evidence that this subfield has outgrown any single reader?",
                    "Does the abstract state the cutoff date of your literature search?",
                ],
            },
            {
                "title": "Introduction",
                "weight": 2400,
                "job": "Establish territory, find the niche, occupy it. This is "
                       "where the survey's only argumentative figure belongs -- "
                       "typically the growth curve that proves the field is "
                       "unsurveyable by hand.",
                "ask": [
                    "What figure proves the field needs a survey? (Publication counts over time is the standard move.)",
                    "Which existing surveys exist, and what specifically do they not cover?",
                    "What will a reader be able to do after reading this that they could not before?",
                ],
            },
            {
                "title": "Scope and selection criteria",
                "weight": 1800,
                "job": "The section most surveys omit and every reviewer now asks "
                       "for. State the databases searched, the query strings, the "
                       "date range, inclusion and exclusion rules, and the resulting "
                       "counts. Then admit the selection bias that remains.",
                "ask": [
                    "What exact queries, on what databases, over what date range?",
                    "How many records were found, screened, and included? Give all three numbers.",
                    "What kind of work does your criterion systematically exclude?",
                    "Do you cite your own prior work disproportionately? Check, and disclose if so.",
                ],
            },
            {
                "title": "Background and terminology",
                "weight": 4000,
                "job": "The shared vocabulary the rest of the survey presupposes. "
                       "Every term you will use contrastively later must be pinned "
                       "down here, once.",
                "ask": [
                    "Which terms are used inconsistently across the literature, and which usage do you adopt?",
                    "What is the minimum a reader needs before section 4 makes sense?",
                    "Is there a taxonomy figure that shows how the pillars relate?",
                ],
            },
            {
                "title": "Resources: datasets, tools and benchmarks",
                "weight": 2800,
                "job": "Catalogue tables. This is usually the most-cited part of a "
                       "survey because it is the part practitioners actually reuse.",
                "ask": [
                    "What are the canonical resources, and what is each one's licence and access status?",
                    "Is there a maintained companion repository? Link it, and stamp a version and date.",
                    "Does every table row point at something a reader can actually obtain?",
                ],
            },
            {
                "title": "Pillar 1: <first promised aspect>",
                "weight": 3000,
                "job": "Use the same internal template for every pillar: numbered "
                       "subsections, bolded run-in headings, one pipeline figure, one "
                       "configuration table, and a closing 'Summary and discussion'.",
                "ask": [
                    "What is the organizing axis inside this pillar, and is it the same kind of axis as the other pillars use?",
                    "Which three works are indispensable here, and did you read them in full?",
                    "What does the closing summary say that is not just a list?",
                ],
            },
            {
                "title": "Pillar 2: <second promised aspect>",
                "weight": 3000,
                "job": "Same template as pillar 1. Consistency of internal structure "
                       "is what makes a long survey navigable.",
                "ask": [
                    "Does this pillar's subsection structure mirror pillar 1's?",
                    "What is the disagreement in this literature, and where do you come down?",
                    "What does the closing summary say that is not just a list?",
                ],
            },
            {
                "title": "Pillar 3: <third promised aspect>",
                "weight": 3000,
                "job": "Same template. If a pillar is much thinner than the others, "
                       "say whether that reflects the literature or your coverage.",
                "ask": [
                    "Is this pillar thin because the area is young, or because you covered it less? Say which.",
                    "What claim here is contested, and by whom?",
                    "What does the closing summary say that is not just a list?",
                ],
            },
            {
                "title": "Pillar 4: <fourth promised aspect>",
                "weight": 3000,
                "job": "Same template. This is the last of the promised aspects -- "
                       "check the abstract's list ends exactly here.",
                "ask": [
                    "Does the set of pillars match the abstract's list exactly, in order and in name?",
                    "Which findings across pillars actually conflict with each other?",
                    "What does the closing summary say that is not just a list?",
                ],
            },
            {
                "title": "Applications",
                "weight": 2400,
                "job": "Widen the audience beyond the subfield. Each application "
                       "subsection should end in a 'remaining issues' note rather "
                       "than a success story.",
                "ask": [
                    "Which application domains have adopted this, and with what documented result?",
                    "What is the remaining issue in each domain?",
                    "Are you reporting adoption claims from vendors? Mark them as such.",
                ],
            },
            {
                "title": "Open problems and future directions",
                "weight": 2600,
                "job": "Also the designed overflow container: late-arriving material "
                       "that does not fit the pillar frame lands here rather than "
                       "distorting a core section.",
                "ask": [
                    "Which open problems are named with a citation showing someone has hit the wall?",
                    "What arrived too late to fit the frame, and does it belong here rather than in a pillar?",
                    "Which of these problems would you personally work on next, and why?",
                ],
            },
            {
                "title": "Conclusion",
                "weight": 1600,
                "job": "Restate the scope AND the exclusions. Then forward-looking "
                       "paragraphs, each naming one unsolved problem, each with a "
                       "citation. No new claims.",
                "ask": [
                    "Does the conclusion repeat the abstract's aspect list unchanged?",
                    "Have you restated what the survey does NOT cover?",
                    "Does every forward-looking paragraph name a real, cited open problem?",
                ],
            },
            {
                "title": "Coda: provenance, limitations and update log",
                "weight": 900,
                "job": "Cheap and high-value: how the survey was actually written, "
                       "where it leans on grey literature or on your own judgement, "
                       "an invitation to send corrections, and a dated changelog if "
                       "you intend to revise.",
                "ask": [
                    "Which subsections rest on blog posts, APIs or your own reading rather than peer-reviewed work?",
                    "If you release a v2, can a reader of v1 tell what changed without diffing PDFs?",
                    "Who reviewed this before submission, and what did they change?",
                ],
            },
        ],
    },

    "position": {
        "label": "Position report",
        "model": "On the Opportunities and Risks of Foundation Models (~122,094 words)",
        "needs": "Literature plus a normative argument, and usually a coalition of "
                 "authors. No experiments.",
        "sections": [
            {
                "title": "Abstract",
                "fixed": 200,
                "job": "Name the thing, state the normative claim, and be explicit "
                       "that this is an argument rather than a finding.",
                "ask": [
                    "What is the one sentence someone would quote when disagreeing with you?",
                    "Is it unambiguous that this paper reports no experiments?",
                    "Does the author list itself form part of the argument? If so, say so here.",
                ],
            },
            {
                "title": "Naming the thing",
                "weight": 2100,
                "job": "Coin or adopt the vocabulary, earn it with a compressed "
                       "history in a few named beats, then explicitly reject the "
                       "alternative names and say why each one fails.",
                "ask": [
                    "What is the term, and what exactly does it include and exclude?",
                    "Which alternative names did you consider, and what does each one wrongly imply?",
                    "What is the shortest history that makes the term feel necessary rather than invented?",
                ],
            },
            {
                "title": "Widening the unit of analysis",
                "weight": 1030,
                "job": "Convert a narrow technical topic into a pipeline or system "
                       "with stages, so that the societal claims later in the paper "
                       "have a specific place to attach.",
                "ask": [
                    "What are the stages, and who acts at each one?",
                    "Which stage do current debates ignore?",
                    "Does every later claim in this paper attach to a named stage?",
                ],
            },
            {
                "title": "The normative argument",
                "weight": 1800,
                "job": "The controversial part, deliberately quarantined into one "
                       "clearly-labelled section so the rest of the report can be "
                       "read as description.",
                "ask": [
                    "What ought to happen, who ought to do it, and by when?",
                    "What is the strongest counter-position, stated in its own best terms?",
                    "What evidence would change your mind? Say it explicitly.",
                ],
            },
            {
                "title": "Scope, method and incompleteness",
                "weight": 1400,
                "job": "How this document was produced, who wrote which part, what "
                       "it does not cover, and -- if multi-author -- the statement "
                       "that not all authors hold all the views expressed.",
                "ask": [
                    "How were topics chosen, and by whom?",
                    "Do all authors endorse every claim? If not, say so in one unhedged sentence.",
                    "What is missing from this report that a fair critic would expect?",
                ],
            },
            {
                "title": "Part I: <first substantive area>",
                "weight": 2500,
                "job": "Substantive area, argued from literature. Place the most "
                       "technical part first so later, softer chapters inherit its "
                       "authority.",
                "ask": [
                    "What is the claim of this part, in one sentence, at the top?",
                    "Which citations do the real work here, and have you read them in full?",
                    "Where does this part overreach? Mark the boundary yourself.",
                ],
            },
            {
                "title": "Part II: <second substantive area>",
                "weight": 2500,
                "job": "Substantive area. Each part should be independently readable "
                       "and should cross-reference the pipeline stages named earlier.",
                "ask": [
                    "Which pipeline stage does this part attach to?",
                    "What would someone who works in this area day-to-day say you got wrong?",
                    "Is any empirical-sounding claim here actually unmeasured? Hedge it or cut it.",
                ],
            },
            {
                "title": "Part III: <third substantive area>",
                "weight": 2000,
                "job": "Substantive area. If one part is far shorter than the others, "
                       "the length difference is itself a claim about importance -- "
                       "make sure it is the claim you want to make.",
                "ask": [
                    "Is this part short because the topic is small, or because you know less about it?",
                    "Whose interests are affected here, and are any of them absent from your author list?",
                    "What is the concrete recommendation, if any?",
                ],
            },
            {
                "title": "Conclusion",
                "weight": 250,
                "job": "Deliberately tiny. Do not summarize a long report -- re-frame "
                       "it, and defend the methodological gamble you took by writing "
                       "it this way.",
                "ask": [
                    "Why was this document worth writing in this form?",
                    "Have you resisted the urge to summarize? A summary here is wasted words.",
                ],
            },
            {
                "title": "Disclosure: funding, conflicts and disagreement",
                "weight": 400,
                "job": "A separate, clearly-headed section naming funders, "
                       "institutional interests, and the external reviewers who "
                       "pushed back.",
                "ask": [
                    "Who funded this, and does any funder have an interest in the conclusion?",
                    "Which external reviewers commented, and did you name them with permission?",
                    "Is there a dissenting view among the authors that a reader should know about?",
                ],
            },
        ],
    },

    "analysis": {
        "label": "Mechanistic analysis / argument paper",
        "model": "In-context Learning and Induction Heads (~21,700 words)",
        "needs": "Original observational and interventional experiments on an "
                 "EXISTING system. No new artifact required, but the analyses "
                 "reported must be analyses you actually ran.",
        "sections": [
            {
                "title": "Abstract",
                "fixed": 230,
                "job": "Down-grade your own claim explicitly, then partition where "
                       "the evidence is strong from where it is weak. Naming your "
                       "evidence class ('indirect', 'circumstantial') in the abstract "
                       "buys you the right to argue hard later.",
                "ask": [
                    "What is the causal claim, stated in one sentence?",
                    "What class of evidence do you have -- correlational, interventional, circumstantial? Name it.",
                    "For which sub-claim is your evidence weakest? Say that here, not only in the limitations.",
                ],
            },
            {
                "title": "Introduction",
                "weight": 1370,
                "job": "Territory, gap, thesis, and then a numbered list of the "
                       "independent arguments to come. Warn about the main confound "
                       "before a reader finds it themselves.",
                "ask": [
                    "What is the phenomenon, and why is the standard explanation unsatisfying?",
                    "What are your N independent lines of evidence? Number them here.",
                    "What is the confound a hostile reader will raise first?",
                ],
            },
            {
                "title": "Key concepts and instruments",
                "weight": 1890,
                "job": "Turn every piece of jargon into a measurable instrument "
                       "BEFORE using it. A term you cannot measure cannot carry an "
                       "argument.",
                "ask": [
                    "For each key term: what is the operational definition and the measurement procedure?",
                    "What does your instrument fail to distinguish?",
                    "Would an independent implementer get the same numbers from your definition alone?",
                ],
            },
            {
                "title": "Framing: the shape of the evidence",
                "weight": 610,
                "job": "Show the summed evidence matrix before the evidence itself, "
                       "and re-paste the numbered argument list verbatim so the "
                       "reader can navigate. Label it as repeated.",
                "ask": [
                    "Which arguments cover which systems or conditions? Draw that as a matrix.",
                    "Where are the empty cells, and do you say so?",
                ],
            },
            {
                "title": "Argument 1: <state the claim as the heading>",
                "weight": 1900,
                "job": "The heading IS the claim sentence, never a topic label. "
                       "Present the evidence, then close the section by attacking it "
                       "yourself.",
                "ask": [
                    "What exactly was measured, on what, how many times?",
                    "What alternative explanation survives this evidence?",
                    "What result would have falsified this argument, and did you look for it?",
                ],
            },
            {
                "title": "Argument 2: <state the claim as the heading>",
                "weight": 1460,
                "job": "Independent line of evidence -- it must not rest on argument "
                       "1's assumptions, or it is not independent.",
                "ask": [
                    "Is this genuinely independent of argument 1, or does it share an assumption?",
                    "What is the effect size, and is it large enough to matter?",
                    "How does this section attack itself before a reviewer does?",
                ],
            },
            {
                "title": "Argument 3: <state the claim as the heading>",
                "weight": 1230,
                "job": "Independent line of evidence. Interventional evidence belongs "
                       "as late as it is strong -- if you have an ablation that moves "
                       "the needle, this is where it earns its place.",
                "ask": [
                    "Is there an intervention, not just an observation? If not, say the claim stays correlational.",
                    "What is the control condition?",
                    "How does this section attack itself before a reviewer does?",
                ],
            },
            {
                "title": "Systems, data and methods",
                "weight": 980,
                "job": "The evidence dump, deferred until after the arguments so the "
                       "narrative never stalls. Table of systems, sizes, seeds, run "
                       "counts, compute.",
                "ask": [
                    "Exactly how many systems, runs, seeds and interventions? Give the counts.",
                    "What would someone need to reproduce this, and is all of it here?",
                    "Which measurements were discarded, and on what pre-stated rule?",
                ],
            },
            {
                "title": "Unexplained curiosities",
                "weight": 690,
                "job": "Publish the residue. Partial investigations that went "
                       "nowhere, and bare open questions. This section is worth more "
                       "to your credibility than another confirmatory result.",
                "ask": [
                    "What did you observe that your hypothesis does not explain?",
                    "Which investigation did you abandon, and at what point?",
                    "What would you do next if you had another six months?",
                ],
            },
            {
                "title": "Discussion",
                "weight": 350,
                "job": "Short. Cash the motivation you opened with, and report at "
                       "least one clean negative result.",
                "ask": [
                    "What follows for the motivating problem, concretely?",
                    "What did NOT work? Report one negative result plainly.",
                    "What is the honest ceiling on how far this generalizes?",
                ],
            },
            {
                "title": "Related work",
                "weight": 1970,
                "job": "Placed AFTER the results, and carrying the bulk of the "
                       "paper's citations. Split explicitly into 'consistent with' "
                       "and 'in tension with' -- the second list is the one readers "
                       "trust you for.",
                "ask": [
                    "Which prior findings agree with yours?",
                    "Which prior findings appear to contradict yours, and how do you account for that?",
                    "Have you cited the work that would most embarrass you to have missed?",
                ],
            },
        ],
    },

    "empirical": {
        "label": "Empirical / new artifact or measurement",
        "model": "Attention Is All You Need + Training Compute-Optimal LLMs",
        "needs": "ORIGINAL DATA from experiments you actually ran. Measured numbers "
                 "from runs that happened. This genre cannot be written from a topic "
                 "alone.",
        "sections": [
            {
                "title": "Abstract",
                "fixed": 200,
                "job": "Context -> thesis -> result along the axes you claim -> at "
                       "most two headline numbers -> the generalization claim. No "
                       "citations. Every number here must appear identically in the "
                       "results section.",
                "ask": [
                    "What are the two headline numbers, and have you checked them against the results table character by character?",
                    "Is the thesis falsifiable as written?",
                    "Does the generalization claim exceed what you measured? Trim it if so.",
                ],
            },
            {
                "title": "Introduction",
                "weight": 850,
                "job": "Make the paper feel inevitable. Turn a widely-shared practice "
                       "or assumption into a visible error, then say what you did "
                       "about it. Not a contributions list dressed as prose.",
                "ask": [
                    "What does everyone currently do, and why is it wrong or costly?",
                    "What is the single sentence a reader should remember?",
                    "Do your enumerated contributions each name a specific artifact, number or method?",
                ],
            },
            {
                "title": "Background and related work",
                "weight": 918,
                "job": "Organize prior work by which constraint each family fails to "
                       "remove, not chronologically. If a rival got a different "
                       "answer, diagnose why here so the method section can stay pure.",
                "ask": [
                    "What is the axis that sorts prior work into families?",
                    "Which prior result conflicts with yours, and what is your forensic account of the difference?",
                    "What is your scoped novelty claim, stated so a reviewer could check it?",
                ],
            },
            {
                "title": "Method",
                "weight": 1960,
                "job": "The reproducibility core. Every constant inline. Top-down: "
                       "the whole picture, then the parts, then the mechanism, then "
                       "the small details.",
                "ask": [
                    "Could a competent stranger reimplement this from this section alone?",
                    "Is every hyperparameter, dimension and constant stated, or does one live only in your code?",
                    "Which design choices were made for principled reasons and which were arbitrary? Say which.",
                ],
            },
            {
                "title": "Experimental setup",
                "weight": 600,
                "job": "Housekeeping so the cost and performance claims can be "
                       "audited. Hardware, data, splits, seeds, wall-clock, budget. "
                       "No interpretation here.",
                "ask": [
                    "What data, from where, under what licence, split how?",
                    "How many seeds, and are you reporting variance? Single-run comparisons are now a standard reviewer objection.",
                    "What did this cost in compute and time?",
                ],
            },
            {
                "title": "Results",
                "weight": 1900,
                "job": "Report what you measured. Restate the prediction before "
                       "revealing the outcome, so a null result is still a result. "
                       "Every number here must come from a run that actually happened.",
                "ask": [
                    "Is every number in this section traceable to a specific log, run or output file?",
                    "What was predicted before you looked, and did it hold?",
                    "Are effect sizes reported with uncertainty, or only point estimates?",
                    "Does any table cell contain a number you have not personally verified?",
                ],
            },
            {
                "title": "Ablations and analysis",
                "weight": 900,
                "job": "Attack your own result. Ablate against yourself, and include "
                       "at least one transfer or out-of-distribution check to "
                       "pre-empt the 'one-trick' objection.",
                "ask": [
                    "Which component, removed, hurts most -- and did you actually run that ablation?",
                    "Where does the method fail? Show the failure case.",
                    "What negative result did you obtain, and is it reported?",
                ],
            },
            {
                "title": "Limitations",
                "weight": 400,
                "job": "Unhedged. Name the specific conditions under which your "
                       "conclusion does not hold. A limitations section that only "
                       "lists 'future work' is not a limitations section.",
                "ask": [
                    "Under what conditions would your headline claim be false?",
                    "What did you not measure that a reader might assume you did?",
                    "Is any limitation here phrased as an opportunity? Rewrite it as a limitation.",
                ],
            },
            {
                "title": "Conclusion",
                "weight": 300,
                "job": "Restate the novelty flatly, restate the headline numbers "
                       "once, convert every unaddressed limitation into named future "
                       "work. No new claims.",
                "ask": [
                    "Do the numbers here match the abstract and the results table exactly?",
                    "Is every future-work item traceable to a limitation you admitted?",
                ],
            },
            {
                "title": "Reproducibility and artifact availability",
                "weight": 350,
                "job": "Where the code, data and configs are, under what licence, at "
                       "what version or DOI. State what cannot be released and why.",
                "ask": [
                    "Is there a permanent identifier (DOI, tagged release) rather than a bare URL?",
                    "What cannot be shared, and what is the reason -- licence, privacy, size?",
                    "Can the headline table be regenerated by one documented command?",
                ],
            },
        ],
    },
}

EMPIRICAL_WARNING = """> **STOP -- READ BEFORE WRITING A SINGLE NUMBER IN THIS DRAFT**
>
> This is the EMPIRICAL genre. It requires original data or experiments that you
> have actually run. It cannot be written from a topic alone.
>
> A paper that reports accuracies, scores, ablation results, p-values, sample
> sizes, runtimes or any other measurement that was not actually measured is
> **fabrication**. Not a draft, not a placeholder, not a stylistic exercise.
> There is no framing -- "illustrative numbers", "for the template", "we will
> replace these later" -- that makes a fabricated result acceptable to circulate,
> and no legitimate reason to generate one.
>
> The same applies to citations. A reference attached to a claim you have not
> verified the source actually makes is a fabricated citation, even when the
> paper it names is real.
>
> **If you do not have data yet**, pick a genre that does not require it, and
> that is worth writing on its own terms:
>
> - `--genre agenda`   a taxonomy and a list of startable experiments
> - `--genre position` a normative argument
> - `--genre survey`   the bibliography itself is the contribution
> - `--genre analysis` argument from analyses of an existing system
>
> Or write a protocol / registered-report-style paper stating what you *will* run.
>
> Delete this block once you have real results in hand.
"""

# --------------------------------------------------------------------------
# ASCII helpers -- the console here is cp1252
# --------------------------------------------------------------------------

TRANSLIT = {
    0x2018: "'", 0x2019: "'", 0x201a: "'", 0x201b: "'",
    0x201c: '"', 0x201d: '"', 0x201e: '"', 0x2033: '"',
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2015: "-",
    0x2026: "...", 0x00a0: " ", 0x202f: " ", 0x2009: " ", 0x00ad: "",
    0x2022: "*", 0x00b7: "*", 0x2192: "->", 0x2190: "<-", 0x00d7: "x",
}


def ascii_only(text):
    """Best-effort ASCII. Transliterate what we can, strip accents, drop the rest."""
    if text is None:
        return ""
    text = str(text).translate(TRANSLIT)
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        stripped = "".join(c for c in decomposed if ord(c) < 128)
        out.append(stripped if stripped else "?")
    return "".join(out)


def emit(text=""):
    """Print without ever dying on the console codec."""
    text = ascii_only(text)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")


def die(message):
    sys.stderr.write("error: " + ascii_only(message) + "\n")
    raise SystemExit(2)


# --------------------------------------------------------------------------
# Budget arithmetic
# --------------------------------------------------------------------------

def budgets_for(genre, target=None):
    """Resolve every section's word budget for a genre.

    Sections with a "fixed" budget keep it. The rest split whatever the target
    leaves, in proportion to their weights, using largest-remainder rounding so
    the budgets sum to exactly the target.
    """
    skeleton = SKELETONS[genre]
    total = GENRE_TARGETS[genre] if target is None else int(target)
    sections = skeleton["sections"]

    fixed_total = sum(s["fixed"] for s in sections if "fixed" in s)
    weight_total = sum(s.get("weight", 0) for s in sections)
    remainder = total - fixed_total
    if weight_total <= 0:
        die("genre '%s' has no weighted sections" % genre)
    if remainder < len(sections):
        die("target %d is too small for genre '%s' (fixed sections alone need %d)"
            % (total, genre, fixed_total))

    # Largest-remainder apportionment of `remainder` across the weighted sections.
    exact, floors = [], []
    for s in sections:
        if "fixed" in s:
            exact.append(None)
            floors.append(s["fixed"])
        else:
            share = remainder * s["weight"] / float(weight_total)
            exact.append(share)
            floors.append(int(share))

    assigned = sum(f for f, e in zip(floors, exact) if e is not None)
    leftover = remainder - assigned
    order = sorted(
        [i for i, e in enumerate(exact) if e is not None],
        key=lambda i: exact[i] - floors[i],
        reverse=True,
    )
    for k in range(leftover):
        floors[order[k % len(order)]] += 1

    return [(s, b) for s, b in zip(sections, floors)]


# --------------------------------------------------------------------------
# Draft generation
# --------------------------------------------------------------------------

SECTION_MARK = "scaffold:section"
PLACEHOLDER = "TODO"   # the body stub written under every heading
BUDGET_RE = re.compile(SECTION_MARK + r"\s+budget=(\d+)")


def yaml_str(value):
    """Double-quoted YAML scalar, escaped."""
    value = ascii_only(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % value


def yaml_plain(value):
    """Unquoted when that is unambiguous, quoted when it is not.

    The downstream readers in this pipeline (publish_paper.py, md2pdf.py) are
    line regexes, not YAML parsers: they hand back the quotes as part of the
    value. Anything that can be written bare is written bare, so a keyword does
    not reach a permanent record wearing quotation marks.
    """
    text = ascii_only(value).strip()
    if not text:
        return '""'
    if re.search(r'[:#\[\]{}&*!|>%@`,\'"]', text) or text[0] in "-?" or text != value.strip():
        return yaml_str(value)
    if text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return yaml_str(value)
    return text


def comment_safe(text):
    """Text destined for the inside of an HTML comment. A literal '-->' would
    close the comment early and dump the working notes into the rendered page."""
    return ascii_only(text).replace("-->", "->")


def wrap(text, width=76, indent=""):
    """Greedy wrap. No textwrap import needed and the behaviour is obvious."""
    words, lines, line = ascii_only(text).split(), [], ""
    for w in words:
        candidate = w if not line else line + " " + w
        if len(candidate) + len(indent) > width and line:
            lines.append(indent + line)
            line = w
        else:
            line = candidate
    if line:
        lines.append(indent + line)
    return lines


def slugify(text):
    text = ascii_only(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "draft"


def build_draft(title, genre, authors, keywords, license_id, target, ai_note, date,
                publication_type=None, copyright_holder=None, version=None):
    skeleton = SKELETONS[genre]
    plan = budgets_for(genre, target)
    total = sum(b for _, b in plan)

    pub_type = publication_type or publication_type_for(genre)
    # The copyright line is prose, not a citation: "(c) 2026 Pranay Mahendrakar",
    # not "(c) 2026 Mahendrakar, Pranay M.". So it comes from paper_defaults
    # rather than being derived from the citation-form author name.
    try:
        import paper_defaults as _d
        _std_holder = _d.get("copyright")
    except Exception:
        _std_holder = ""
    holder = (copyright_holder or _std_holder
              or (authors[0] if authors else AUTHOR_PLACEHOLDER))
    version = version or DEFAULT_VERSION

    out = []

    # ---- YAML front matter ----
    # Every comment here sits on its own line. The readers downstream in this
    # pipeline match front matter with line regexes and would otherwise carry a
    # trailing "# comment" onto the record as part of the value.
    out.append("---")
    out.append("title: %s" % yaml_str(title))
    out.append("# The record creator. publish_paper.py reads this singular key.")
    out.append("author: %s" % yaml_str(authors[0] if authors else AUTHOR_PLACEHOLDER))
    if len(authors) > 1:
        out.append("# Co-authors, for your own reference.")
        out.append("authors:")
        for a in authors:
            out.append("  - %s" % yaml_str(a))
    out.append("date: %s" % yaml_str(date))
    out.append("genre: %s" % genre)
    out.append("target_words: %d" % total)
    out.append("keywords:")
    for k in keywords:
        out.append("  - %s" % yaml_plain(k))
    # A plain ">" folded scalar, not ">-". The chomp indicator changes nothing
    # about the text, but publish_paper.py's block() reader is a line regex that
    # matches "key: >" only: written as ">-" the disclosure parses as empty
    # there and never reaches the record description. Verified, not assumed.
    out.append("ai_assistance: >")
    for line in wrap(ai_note, width=74, indent="  "):
        out.append(line)
    out.append("")
    out.append("# ==== PERMANENT RECORD =================================================")
    out.append("# Everything below is copied onto a public Zenodo record and stamped")
    out.append("# with a DOI. Decide each one now. Pre-flight: --check <this file>")
    out.append("")
    out.append("license: %s" % yaml_plain(license_id))
    out.append("")
    out.append("# EDIT: the copyright holder, exactly as it should appear. It goes on")
    out.append("# the record AND has to be written into the document itself, because a")
    out.append("# published PDF can never be changed afterwards.")
    out.append("copyright: %s" % yaml_str(holder))
    out.append("")
    if pub_type == publication_type_for(genre):
        out.append("# Conservative default for genre '%s'. Change it deliberately."
                   % genre)
    else:
        out.append("# Chosen at generation time with --publication-type.")
    out.append("# One of: article report workingpaper preprint technicalnote")
    out.append("# conferencepaper thesis book section patent deliverable milestone")
    out.append("# proposal softwaredocumentation taxonomictreatment")
    out.append("# datamanagementplan annotationcollection other")
    out.append("publication_type: %s" % yaml_plain(pub_type))
    out.append("")
    out.append("version: %s" % yaml_str(version))
    out.append("")
    # Standing values come from paper_defaults.py. Emitting them filled in,
    # rather than as EDIT placeholders, is the point: an unedited placeholder
    # is how a wrong value reaches a permanent record.
    try:
        import paper_defaults as _d
        std_orcid = _d.get("orcid")
        std_affil = _d.get("affiliation")
        std_journal = _d.get("journal_title")
    except Exception:
        std_orcid = std_affil = std_journal = ""

    if std_orcid:
        out.append("orcid: %s" % std_orcid)
    else:
        out.append("# EDIT: 0000-0000-0000-0000 -- 16 digits, last one may be X.")
        out.append("# Leave empty rather than guessing; a wrong ORCID credits someone else.")
        out.append('orcid: ""')
    out.append("")
    if std_affil:
        out.append('affiliation: "%s"' % std_affil)
    else:
        out.append("# EDIT: institution, or leave empty.")
        out.append('affiliation: ""')
    out.append("")
    out.append("# The journal_* fields are ONLY used when publication_type is")
    out.append("# 'article'. Zenodo ignores them for every other publication_type.")
    if std_journal:
        out.append('journal_title: "%s"' % std_journal)
        out.append("# Volume, issue and pages are left unset on purpose - inventing")
        out.append("# them would assert something untrue about the venue.")
    else:
        out.append("# Uncomment and fill in if, and only if, this is a journal article.")
        out.append("# journal_title: Journal name here")
    out.append("# journal_volume: 1")
    out.append("# journal_issue: 1")
    out.append("# journal_pages: 1-20")
    out.append("# =======================================================================")
    out.append("---")
    out.append("")

    # ---- permanent record warning ----
    out.append(PERMANENT_RECORD_NOTE)
    out.append("")

    # ---- header comment ----
    out.append("<!--")
    out.append("  Scaffold generated by paper_scaffold.py -- genre: %s" % genre)
    out.append("  Model for the section proportions: %s" % comment_safe(skeleton["model"]))
    out.append("")
    for line in wrap(comment_safe("REQUIRED INPUT: " + skeleton["needs"]), width=76, indent="  "):
        out.append(line)
    out.append("")
    # NB: no literal "-->" anywhere inside this block; it would close the comment.
    out.append("  Every HTML comment block below is a working note: the word budget,")
    out.append("  the job that section has to do, and the questions to answer in it.")
    out.append("  DELETE ALL OF THEM BEFORE PUBLICATION.")
    out.append("")
    out.append("  Progress:  python paper_scaffold.py --status <this file>")
    out.append("-->")
    out.append("")

    out.append("# %s" % ascii_only(title))
    out.append("")

    if genre == "empirical":
        out.append(EMPIRICAL_WARNING.rstrip())
        out.append("")

    # ---- sections ----
    for section, budget in plan:
        out.append("## %s" % ascii_only(section["title"]))
        out.append("")
        out.append("<!-- %s budget=%d" % (SECTION_MARK, budget))
        out.append("")
        out.append("     BUDGET: ~%d words (%.0f%% of the %d-word target)"
                   % (budget, 100.0 * budget / total, total))
        out.append("")
        out.append("     JOB:")
        for line in wrap(comment_safe(section["job"]), width=72, indent="     "):
            out.append(line)
        out.append("")
        out.append("     ANSWER THESE:")
        for i, q in enumerate(section["ask"], 1):
            qlines = wrap(comment_safe(q), width=68, indent="")
            out.append("       %d. %s" % (i, qlines[0]))
            for extra in qlines[1:]:
                out.append("          %s" % extra)
        out.append("")
        out.append("     Delete this note before publication.")
        out.append("-->")
        out.append("")
        out.append(PLACEHOLDER)
        out.append("")

    # ---- references ----
    out.append("## References")
    out.append("")
    out.append("<!-- Not budgeted and not counted by --status: a reference list is not prose.")
    out.append("")
    out.append("     Before submission, check mechanically:")
    out.append("       - every entry here is cited at least once in the body")
    out.append("       - every in-text citation key resolves to an entry here")
    out.append("       - every entry is a source you have actually read, and it")
    out.append("         genuinely supports the claim it is attached to")
    out.append("       - quoted material is in quotation marks with a page number")
    out.append("")
    out.append("     A citation you have not verified is a fabricated citation, even")
    out.append("     when the paper it names exists.")
    out.append("-->")
    out.append("")

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Status: parse a generated draft and count real prose
# --------------------------------------------------------------------------

FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
HEADING2_RE = re.compile(r"^##(?!#)\s*(.+?)\s*$")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def blank_out(match):
    """Replace a matched span with the same number of newlines, so line
    structure (and therefore heading detection) survives the deletion."""
    return "\n" * match.group(0).count("\n")


def strip_fences(text):
    """Remove fenced code blocks, keeping line count stable."""
    lines = text.split("\n")
    out, fence = [], None
    for line in lines:
        m = FENCE_RE.match(line)
        if fence is None and m:
            fence = m.group(1)
            out.append("")
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0]:
                fence = None
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def count_words(text):
    """Count prose words. Headings, table rules and markdown punctuation do not
    count; a token counts only if it contains a letter or a digit."""
    n = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped == PLACEHOLDER:
            continue
        if TABLE_RULE_RE.match(line) and "-" in line:
            continue
        for token in stripped.split():
            token = token.strip("#*_`>|~[](){}<>\"'.,;:!?")
            if any(c.isalnum() for c in token):
                n += 1
    return n


def scalar_value(raw):
    """Unquote a YAML scalar and drop any trailing '# comment'.

    Quote-aware, so a '#' inside a quoted title survives. Without this the
    generated front matter -- which explains each permanent-record field in an
    inline comment -- would read back as value-plus-comment.
    """
    raw = raw.strip()
    if raw[:1] in ('"', "'"):
        quote = raw[0]
        out, i, escaped = [], 1, False
        while i < len(raw):
            ch = raw[i]
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                break
            else:
                out.append(ch)
            i += 1
        return "".join(out)
    cut = raw.find("#")
    while cut > 0 and raw[cut - 1] not in (" ", "\t"):
        cut = raw.find("#", cut + 1)
    if cut >= 0:
        raw = raw[:cut]
    return raw.strip()


def parse_front_matter(text, want_lists=False):
    """-> (meta, body), or (meta, lists, body) when want_lists is set.

    Only top-level scalars land in meta; indented lines belong to a block or a
    list. With want_lists, top-level '- item' sequences are collected too.
    """
    meta, lists = {}, {}
    if not text.startswith("---"):
        return (meta, lists, text) if want_lists else (meta, text)
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return (meta, lists, text) if want_lists else (meta, text)

    pending = None          # key whose list items we are collecting
    block_key = None        # key whose folded/literal block we are collecting
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if block_key is not None:
                joined = (meta[block_key] + " " + stripped).strip()
                meta[block_key] = joined
            elif pending is not None and stripped.startswith("- "):
                lists.setdefault(pending, []).append(scalar_value(stripped[2:]))
            continue
        pending = block_key = None
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = scalar_value(value)
        if value in (">", ">-", ">+", "|", "|-", "|+"):
            meta[key] = ""      # a block scalar: the text is on the lines below
            block_key = key
            continue
        meta[key] = value
        if value == "":
            pending = key
    body = "\n".join(lines[end + 1:])
    return (meta, lists, body) if want_lists else (meta, body)


def parse_draft(path):
    """-> (meta, [(title, budget_or_None, words), ...])"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except IOError as exc:
        die("cannot read %s (%s)" % (path, exc))

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    meta, body = parse_front_matter(raw)
    body = strip_fences(body)

    # Split on level-2 headings. Anything before the first one (H1 title,
    # warning block, header comment) is preamble and is not a section.
    chunks, current, buf = [], None, []
    for line in body.split("\n"):
        m = HEADING2_RE.match(line)
        if m:
            if current is not None:
                chunks.append((current, "\n".join(buf)))
            current, buf = m.group(1), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        chunks.append((current, "\n".join(buf)))

    rows = []
    for title, chunk in chunks:
        bm = BUDGET_RE.search(chunk)
        budget = int(bm.group(1)) if bm else None
        prose = COMMENT_RE.sub(blank_out, chunk)
        rows.append((title.strip(), budget, count_words(prose)))

    # Fall back to the skeleton when the scaffold markers were already deleted.
    genre = meta.get("genre", "")
    if genre in SKELETONS:
        lookup = {}
        for section, budget in budgets_for(genre, meta.get("target_words")):
            lookup[section["title"].strip().lower()] = budget
        rows = [
            (t, b if b is not None else lookup.get(t.lower()), w)
            for (t, b, w) in rows
        ]
    return meta, rows


def bar(fraction, width):
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def fit(text, width):
    text = ascii_only(text)
    return text if len(text) <= width else text[: width - 3] + "..."


def print_status(path):
    meta, rows = parse_draft(path)
    if not rows:
        die("no '## ' sections found in %s -- is it a scaffolded draft?" % path)

    title = meta.get("title", os.path.basename(path))
    genre = meta.get("genre", "unknown")

    emit("")
    emit("  %s" % fit(title, 74))
    emit("  file: %s" % path)
    emit("  genre: %s   target_words: %s"
         % (genre, meta.get("target_words", "?")))
    emit("")

    header = ("%-*s %7s %7s %7s %6s  %s"
              % (COL_SECTION, "SECTION", "BUDGET", "ACTUAL", "DELTA", "PCT", "PROGRESS"))
    rule = "-" * len(header)
    emit(header)
    emit(rule)

    total_budget = 0
    total_actual = 0
    unbudgeted = []

    for title_, budget, words in rows:
        name = fit(title_, COL_SECTION)
        if budget is None:
            unbudgeted.append((title_, words))
            emit("%-*s %7s %7d %7s %6s  %s"
                 % (COL_SECTION, name, "-", words, "-", "-", "(not budgeted)"))
            continue
        delta = words - budget
        pct = (100.0 * words / budget) if budget else 0.0
        total_budget += budget
        total_actual += words
        emit("%-*s %7d %7d %+7d %5.0f%%  %s"
             % (COL_SECTION, name, budget, words, delta, pct,
                bar(words / float(budget) if budget else 0, ROW_BAR_WIDTH)))

    emit(rule)
    overall = (100.0 * total_actual / total_budget) if total_budget else 0.0
    emit("%-*s %7d %7d %+7d %5.0f%%"
         % (COL_SECTION, "TOTAL (budgeted sections)", total_budget, total_actual,
            total_actual - total_budget, overall))
    emit("")
    emit("  %s  %.0f%% of %d words" % (bar(total_actual / float(total_budget)
                                           if total_budget else 0, BAR_WIDTH),
                                       overall, total_budget))
    emit("")

    # ---- notes ----
    notes = []
    empty = [t for t, b, w in rows if b is not None and w == 0]
    if empty:
        notes.append("%d section(s) still empty: %s"
                     % (len(empty), ", ".join(fit(t, 28) for t in empty[:4])
                        + (" ..." if len(empty) > 4 else "")))
    over = [(t, w - b) for t, b, w in rows if b is not None and w > b * 1.25]
    if over:
        notes.append("%d section(s) more than 25%% over budget: %s"
                     % (len(over), ", ".join("%s (+%d)" % (fit(t, 24), d)
                                             for t, d in over[:3])))
    for t, w in unbudgeted:
        if t.lower() != "references":
            notes.append("section '%s' has no budget -- added by hand, or renamed "
                         "since generation" % fit(t, 40))

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        remaining = len(BUDGET_RE.findall(raw))
    except IOError:
        remaining = 0
    if remaining:
        notes.append("%d scaffold note(s) still in the file -- delete them before "
                     "publication" % remaining)
    if genre == "empirical":
        notes.append("EMPIRICAL genre: every number in this draft must come from a "
                     "run that actually happened.")

    if notes:
        emit("  Notes")
        for n in notes:
            for i, line in enumerate(wrap(n, width=72, indent="")):
                emit("    %s %s" % ("-" if i == 0 else " ", line))
        emit("")


# --------------------------------------------------------------------------
# Pre-flight: every field that will land on the permanent record
# --------------------------------------------------------------------------

CHECK_COL_FIELD = 17
CHECK_COL_VALUE = 44


def is_placeholder(field, value):
    """True when the value is still something the scaffold wrote, not a choice."""
    value = value.strip()
    if not value:
        return False
    low = value.lower()
    if value in (AUTHOR_PLACEHOLDER, ORCID_PLACEHOLDER):
        return True
    if "edit:" in low or low.startswith("edit ") or low.startswith("edit this"):
        return True
    if low in ("todo", "tbd", "journal name here", "journal title here",
               "institution", "<name>"):
        return True
    return False


def check_draft(path):
    """Print the permanent-record field table. -> exit code (0 ok, 1 not ok)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except IOError as exc:
        die("cannot read %s (%s)" % (path, exc))

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not raw.startswith("---"):
        die("%s has no YAML front matter -- nothing to check" % path)

    meta, lists, body = parse_front_matter(raw, want_lists=True)

    # publish_paper.py reads the singular "author". Accept an "authors:" list
    # as a fallback so a hand-written draft is not failed on a spelling.
    if not meta.get("author"):
        first = (lists.get("authors") or [""])[0]
        # Older scaffolds wrote a mapping per author: "- name: Doe, Jane".
        m = re.match(r"^name\s*:\s*(.+)$", first)
        if m:
            first = scalar_value(m.group(1))
        if first:
            meta["author"] = first

    values = {}
    for field, _required in CHECK_FIELDS:
        if lists.get(field):
            values[field] = ", ".join(lists[field])
        else:
            values[field] = meta.get(field, "").strip()

    rows, missing_required, undecided_required = [], [], []
    for field, required in CHECK_FIELDS:
        value = values[field]
        if not value:
            # "MISSING" is reserved for required fields, so the word keeps its
            # force. An optional field nobody filled in is merely "unset".
            status = "MISSING" if required else "unset"
            if required:
                missing_required.append(field)
        elif is_placeholder(field, value):
            status = "DEFAULT"
            if required:
                undecided_required.append(field)
        elif field == "publication_type" and value not in ZENODO_PUBLICATION_TYPES:
            # Never show OK for a value the API will reject.
            status = "INVALID"
        else:
            status = "OK"
        rows.append((field, value, status, required))

    emit("")
    emit("  Pre-flight: fields that will land on the permanent record")
    emit("  file: %s" % path)
    emit("")
    header = ("  %-*s  %-*s  %-8s %s"
              % (CHECK_COL_FIELD, "FIELD", CHECK_COL_VALUE, "VALUE",
                 "STATUS", "REQ"))
    rule = "  " + "-" * (len(header) - 2)
    emit(header)
    emit(rule)
    for field, value, status, required in rows:
        shown = value if value else "(not set)"
        emit("  %-*s  %-*s  %-8s %s"
             % (CHECK_COL_FIELD, fit(field, CHECK_COL_FIELD),
                CHECK_COL_VALUE, fit(shown, CHECK_COL_VALUE),
                status, "yes" if required else "-"))
    emit(rule)
    emit("")
    emit("  OK = set by the author.  DEFAULT = still the scaffold's placeholder.")
    emit("  MISSING = a required field is absent.  unset = an optional field is")
    emit("  empty.  INVALID = Zenodo will reject this value.")
    emit("  A DEFAULT is a decision you have not made yet, and on a required")
    emit("  field it fails this check exactly as a missing value does.")
    emit("")

    # ---- errors: these block publication ----
    errors = []
    for field in missing_required:
        errors.append("required field '%s' is missing -- it must be set before a "
                      "DOI is minted" % field)
    for field in undecided_required:
        # The whole point of this pre-flight. A required field still carrying
        # the generator's placeholder is a decision nobody made, and it is
        # exactly what a silent default looks like on the way to a DOI.
        errors.append("required field '%s' is still the scaffold placeholder "
                      "(%r). That is not a decision -- set it to the real value "
                      "before publishing." % (field, values[field]))

    pub_type = values["publication_type"]
    if pub_type and pub_type not in ZENODO_PUBLICATION_TYPES:
        errors.append("publication_type '%s' is not a value Zenodo accepts. Use one "
                      "of: %s" % (pub_type, ", ".join(ZENODO_PUBLICATION_TYPES)))

    # ---- warnings: worth looking at, but not fatal ----
    warnings = []
    if not pub_type:
        genre = meta.get("genre", "")
        if genre in GENRE_PUBLICATION_TYPE:
            suggestion = ("The conservative default for genre '%s' is '%s'."
                          % (genre, GENRE_PUBLICATION_TYPE[genre]))
        else:
            suggestion = ("This draft's genre ('%s') is not one this generator "
                          "knows, so there is no genre default to fall back on. "
                          "'%s' claims no venue and no peer review, if you want "
                          "the safest answer."
                          % (genre or "unset", UNKNOWN_GENRE_PUBLICATION_TYPE))
        warnings.append("publication_type is not set. Do not let the publishing "
                        "script pick one for you -- declare it here. " + suggestion)

    journal_set = [f for f in JOURNAL_FIELDS if values.get(f)]
    if pub_type == "article" and not values.get("journal_title"):
        warnings.append("publication_type is 'article' but journal_title is empty. "
                        "Zenodo will show an article with no venue.")
    if journal_set and pub_type and pub_type != "article":
        warnings.append("%s set, but publication_type is '%s'. Zenodo only uses the "
                        "journal_* fields when publication_type is 'article', so "
                        "these values will not appear on the record."
                        % (", ".join(journal_set), pub_type))

    orcid = values["orcid"]
    if orcid and not is_placeholder("orcid", orcid) and not ORCID_RE.match(orcid):
        warnings.append("orcid '%s' is not in 0000-0000-0000-0000 form (16 digits, "
                        "last may be X)." % orcid)

    defaults = [f for f, _v, st, _r in rows if st == "DEFAULT"]
    if defaults:
        warnings.append("still carrying the scaffold placeholder: %s"
                        % ", ".join(defaults))

    abstract = re.search(r"(?ms)^##\s+Abstract\s*\n(.*?)(?=^##\s|\Z)", body)
    if abstract is None:
        warnings.append("no '## Abstract' heading in the body. publish_paper.py "
                        "takes the record description from it and will refuse to "
                        "stage the draft without one.")
    elif "<!--" in abstract.group(1):
        # Verified against publish_paper.abstract_of: it takes everything between
        # '## Abstract' and the next heading, comment markup included, and that
        # text becomes the public description of the record.
        errors.append("the Abstract section still contains a scaffold note. "
                      "publish_paper.py copies everything between '## Abstract' "
                      "and the next heading straight into the record description, "
                      "HTML comment and all. Delete the note first.")
    elif abstract.group(1).strip() in ("", PLACEHOLDER):
        errors.append("the Abstract section is empty or still the bare '%s' stub. "
                      "It becomes the description of the record -- the only part "
                      "most readers ever see." % PLACEHOLDER)

    # md2pdf.py writes the copyright line into the PDF from the front matter, so
    # a declared holder does reach the document. What does not survive is an
    # edit made after the PDF was rendered: the published file is frozen.
    holder = values["copyright"]
    pdf_path = os.path.splitext(path)[0] + ".pdf"
    if holder and os.path.isfile(pdf_path):
        try:
            stale = os.path.getmtime(pdf_path) < os.path.getmtime(path)
        except OSError:
            stale = False
        if stale:
            warnings.append("%s is older than this file, so it was rendered from "
                            "front matter that has since changed. Re-render it "
                            "before publishing -- the copyright line and the venue "
                            "line are baked into the PDF, and a published file can "
                            "never be replaced." % os.path.basename(pdf_path))

    if BUDGET_RE.search(raw):
        warnings.append("%d scaffold note(s) are still in the file -- delete them "
                        "before publication." % len(BUDGET_RE.findall(raw)))

    stray = len(re.findall(r"(?m)^%s\s*$" % PLACEHOLDER, body))
    if stray:
        warnings.append("%d section(s) still contain the bare '%s' body stub."
                        % (stray, PLACEHOLDER))

    if warnings:
        emit("  Warnings")
        for w in warnings:
            for i, line in enumerate(wrap(w, width=70, indent="")):
                emit("    %s %s" % ("-" if i == 0 else " ", line))
        emit("")

    if errors:
        emit("  ERRORS -- do not publish")
        for e in errors:
            for i, line in enumerate(wrap(e, width=70, indent="")):
                emit("    %s %s" % ("!" if i == 0 else " ", line))
        emit("")
        emit("  FAIL: %d error(s), %d warning(s)." % (len(errors), len(warnings)))
        emit("")
        return 1

    emit("  PASS: every required field is set to a real value. %d warning(s)."
         % len(warnings))
    emit("  Nothing here is checked again after the DOI is minted. The file is")
    emit("  frozen at publish time; the metadata is editable but already public.")
    emit("")
    return 0


def print_genres():
    emit("")
    emit("  Available genres")
    emit("")
    header = "  %-11s %8s %5s  %s" % ("GENRE", "WORDS", "SECS", "REQUIRED INPUT")
    emit(header)
    emit("  " + "-" * (len(header) - 2))
    for genre in sorted(SKELETONS, key=lambda g: GENRE_TARGETS[g]):
        sk = SKELETONS[genre]
        first = True
        for line in wrap(sk["needs"], width=46):
            if first:
                emit("  %-11s %8d %5d  %s"
                     % (genre, GENRE_TARGETS[genre], len(sk["sections"]) + 1, line))
                first = False
            else:
                emit("  %-11s %8s %5s  %s" % ("", "", "", line))
        for i, line in enumerate(wrap("model: " + sk["model"], width=46)):
            emit("  %-11s %8s %5s  %s" % ("", "", "", line))
        emit("  %-11s %8s %5s  %s"
             % ("", "", "", "publication_type default: %s"
                % publication_type_for(genre)))
        emit("")
    emit("  Section counts include References. Budgets exclude it.")
    emit("  Override any target for one draft with --target N.")
    emit("  The publication_type default is conservative: it claims no venue and")
    emit("  no peer review. Override it with --publication-type, or edit the front")
    emit("  matter. 'article' is never a default -- it asserts a journal.")
    emit("")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="paper_scaffold.py",
        description="Generate a structured working draft skeleton, or report "
                    "progress against one.",
    )
    parser.add_argument("title", nargs="?", help="the paper's working title")
    parser.add_argument("--genre", choices=sorted(SKELETONS),
                        help="which skeleton to use")
    parser.add_argument("--out", help="output .md path (default: slug of the title)")
    try:
        import paper_defaults as _d
        _author_default = _d.get("author") or AUTHOR_PLACEHOLDER
    except Exception:
        _author_default = AUTHOR_PLACEHOLDER
    parser.add_argument("--authors", default=_author_default,
                        help="semicolon-separated author names "
                             "(default comes from paper_defaults.py)")
    parser.add_argument("--keywords", default="",
                        help="comma-separated keywords")
    parser.add_argument("--license", dest="license_id", default=DEFAULT_LICENSE,
                        help="SPDX-style licence id for the front matter "
                             "(default: %s)" % DEFAULT_LICENSE)
    parser.add_argument("--target", type=int,
                        help="override the genre's total word target for this draft")
    parser.add_argument("--force", action="store_true",
                        help="allow overwriting an existing output file")
    parser.add_argument("--status", metavar="DRAFT.md",
                        help="report progress for an existing generated draft")
    parser.add_argument("--check", metavar="DRAFT.md",
                        help="pre-flight: show every field that will land on the "
                             "permanent Zenodo record, with OK / DEFAULT / MISSING "
                             "status. Exits 1 if a required field is missing.")
    parser.add_argument("--list", action="store_true",
                        help="show available genres with their target lengths")
    parser.add_argument("--publication-type", dest="publication_type",
                        choices=ZENODO_PUBLICATION_TYPES,
                        help="Zenodo publication_type for the front matter "
                             "(default: genre-appropriate, see --list)")
    parser.add_argument("--copyright", dest="copyright_holder",
                        help="copyright holder for the front matter "
                             "(default: the first author placeholder)")
    parser.add_argument("--version", dest="version", default=DEFAULT_VERSION,
                        help="version string for the front matter (default: %s)"
                             % DEFAULT_VERSION)
    args = parser.parse_args(argv)

    if args.list:
        print_genres()
        return 0

    if args.status:
        print_status(args.status)
        return 0

    if args.check:
        return check_draft(args.check)

    if not args.title or not args.genre:
        parser.print_usage()
        die("a title and --genre are required (or use --list / --status / --check)")

    title = ascii_only(args.title).strip()
    if not title:
        die("the title is empty after ASCII conversion")

    authors = [a.strip() for a in args.authors.split(";") if a.strip()]
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords:
        # No colon in the placeholder: a colon forces YAML quoting, and
        # publish_paper.py's keyword reader has carried those quote marks onto
        # a record before now. A bare placeholder cannot do that.
        keywords = ["EDIT keyword one", "EDIT keyword two"]

    out_path = args.out or (slugify(title) + ".md")
    out_path = os.path.abspath(out_path)
    if os.path.exists(out_path) and not args.force:
        die("%s already exists -- pass --force to overwrite it" % out_path)

    parent = os.path.dirname(out_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)

    text = build_draft(
        title=title,
        genre=args.genre,
        authors=authors,
        keywords=keywords,
        license_id=args.license_id,
        target=args.target,
        ai_note=DEFAULT_AI_ASSISTANCE,
        date=datetime.date.today().isoformat(),
        publication_type=args.publication_type,
        copyright_holder=args.copyright_holder,
        version=args.version,
    )
    pub_type = args.publication_type or publication_type_for(args.genre)

    with open(out_path, "w", encoding="ascii", errors="replace", newline="\n") as fh:
        fh.write(text)

    plan = budgets_for(args.genre, args.target)
    total = sum(b for _, b in plan)
    emit("")
    emit("  wrote %s" % out_path)
    emit("  genre: %s (%s)" % (args.genre, SKELETONS[args.genre]["label"]))
    emit("  %d sections + References, %d words budgeted"
         % (len(plan), total))
    emit("")
    emit("  Permanent-record fields written into the front matter. Edit them now:")
    # Never call a value the author chose a "default", and never call 'article'
    # conservative: it asserts a journal. The echo has to describe what actually
    # happened, or it is the same silent-default failure one layer up.
    if args.publication_type:
        origin = "(chosen with --publication-type)"
    else:
        origin = "(conservative default for genre '%s')" % args.genre
    emit("    publication_type : %-14s %s" % (pub_type, origin))
    if pub_type == "article":
        emit("                       NOTE: 'article' asserts a journal. Set")
        emit("                       journal_title in the front matter, or use a")
        emit("                       type that claims no venue.")
    emit("    copyright        : %s"
         % (args.copyright_holder or (authors[0] if authors else AUTHOR_PLACEHOLDER)))
    emit("    license          : %s" % args.license_id)
    emit("    version          : %s" % args.version)
    emit("    orcid            : (empty)")
    emit("    affiliation      : (empty)")
    emit("    journal_title    : (commented out; only used for publication_type article)")
    emit("")
    emit("  These end up on a public record with a permanent DOI. The PDF is frozen")
    emit("  at publish time, so anything that must appear IN the document -- the")
    emit("  copyright line above especially -- has to be in the Markdown first.")
    if args.genre == "empirical":
        emit("")
        emit("  NOTE: the empirical genre needs data you actually collected.")
        emit("  Read the warning block at the top of the draft.")
    emit("")
    emit("  next:  python paper_scaffold.py --status %s" % out_path)
    emit("  then:  python paper_scaffold.py --check  %s" % out_path)
    emit("")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        raise SystemExit(130)
