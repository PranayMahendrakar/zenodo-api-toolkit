r"""Tests for the two-column renderer.

Offline: a synthetic paper exercising every part of the layout is rendered
into a temporary folder and the PDF is read back. The things pinned here are
the ones that went wrong while the layout was being built - text clipped at a
column foot, a table's last row dropped, a caption left behind by its table -
and the design itself: two columns, spanning blocks, a standard title block.

    python tests/test_md2pdf.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz  # noqa: E402
import md2pdf as M  # noqa: E402

PASS = FAIL = 0


def check(what, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("ok    %s" % what)
    else:
        FAIL += 1
        print("FAIL  %s\n        got:  %r\n        want: %r" % (what, got, want))


tmp = tempfile.mkdtemp(prefix="md2pdf_test_")

# A figure drawn 10 inches wide (it must span both columns) and one drawn
# 3 inches wide (it must stay in a column). pHYs carries the physical size.
for name, px, dpi in (("wide.png", 2000, 200), ("narrow.png", 600, 200)):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, px, px // 2), False)
    pix.clear_with(200)
    pix.set_dpi(dpi, dpi)
    pix.save(os.path.join(tmp, name))

para = ("The paper argues a point in plain words, and this sentence is long "
        "enough to wrap several times inside a narrow column of text. ")
body = []
for i in range(1, 7):
    body.append("## %d. Section %d\n\n%s\n" % (i, i, (para * 9).strip()))
    if i == 2:
        body.append("Table 1 collects the values; this sentence is about the "
                    "table and is not its caption.\n")
        body.append("Table 1. Values from the sources, one row each.\n")
        body.append("| Source | Setting | Value | Consumer | Tested |\n"
                    "|---|---|---|---|---|\n" +
                    "".join("| Author %d (2026) | setting %d | %d.%d | C%d | yes |\n"
                            % (r, r, r, r, r % 4) for r in range(1, 19)) +
                    "| LASTROW (2026) | final | 9.9 | C4 | yes |\n")
        body.append("| a | b |\n|---|---|\n| small | table |\n")
    if i == 5:
        # The sentence directly above names the table but is not its caption;
        # the caption comes after. The caption must travel with the table.
        body.append("Table 2 summarises the rows below.\n")
        body.append("| Source | Setting | Value | Consumer | Tested |\n"
                    "|---|---|---|---|---|\n" +
                    "".join("| RowTwo%d (2026) | a setting described at some length | "
                            "a reported value with its unit | C1 | yes |\n" % r
                            for r in range(1, 5)))
        body.append("Table 2. The caption after.\n")
        body.append("| Name | Value |\n|---|---|\n| tinycell | 1 |\n")
        body.append("Table 3: a narrow table.\n")
    if i == 3:
        body.append("![Figure 1. A wide figure, re-plotted from the sources.](wide.png)\n")
        body.append("![Figure 2. A narrow figure.](narrow.png)\n")
    if i == 4:
        body.append("```\nAlgorithm 1. A decision drawn wider than one column can hold, "
                    "so it spans both\n" +
                    "".join("step %d: if the condition holds then act on the value and "
                            "record it\n" % s for s in range(1, 9)) + "ALGEND\n```\n")
        body.append("```\nshort code\nfits a column\n```\n")

SRC = """---
title: "A Test Paper About Layout: With a Subtitle"
author: "Mahendrakar, Pranay"
date: 2026-09-29
publication_type: article
journal_title: "Life of Research"
license: CC-BY-4.0
copyright: "Pranay Mahendrakar"
orcid: 0009-0003-7224-029X
affiliation: SONYTECH
keywords:
  - layout
  - two columns
ai_assistance: >
  Drafted with AI assistance under the author's direction. DISCLOSUREEND
---

# A Test Paper About Layout: With a Subtitle

Pranay Mahendrakar

## Abstract

This abstract states what the paper does in a few sentences.

%s
## References

Doe, J. (2026). A Reference. arXiv:2601.00001. doi:10.48550/arXiv.2601.00001
""" % "\n".join(body)

src_path = os.path.join(tmp, "paper.md")
open(src_path, "w", encoding="utf-8").write(SRC)
pdf = os.path.join(tmp, "paper.pdf")
pages = M.render(SRC, pdf, asset_dir=tmp)
doc = fitz.open(pdf)
# NFKC: the typeface sets "fi" and "fl" as ligatures, which get_text returns
# as single characters.
text = [unicodedata.normalize("NFKC", p.get_text()) for p in doc]
alltext = "\n".join(text)

check("the paper renders to several pages", pages >= 3 and len(doc) == pages, True)

# -- page 1: a standard title block ----------------------------------------
p1 = text[0]
check("the title is on page 1", "A Test Paper About Layout" in p1, True)
check("the author is printed as a name, not 'Last, First'",
      ("Pranay Mahendrakar" in p1, "Mahendrakar, Pranay" in p1), (True, False))
check("the venue and date line is on page 1",
      "Life of Research" in p1 and "29 September 2026" in p1, True)
check("the abstract is labelled", "Abstract." in p1, True)
check("the keywords are listed", "Keywords:" in p1 and "two columns" in p1, True)
check("the ORCID is on page 1", "0009-0003-7224-029X" in p1, True)
p1n = p1.replace("\u00a0", " ")
check("the author's work email is on page 1", "pranaymahendrakar@sonytech.in" in p1n, True)
check("the second email is on page 1", "mahendrakarpranay@gmail.com" in p1n, True)
check("the phone number is on page 1, unbroken", "+91 6361723454" in p1n, True)
uris = [l.get("uri") for l in doc[0].get_links()]
check("the emails are mailto links",
      ("mailto:pranaymahendrakar@sonytech.in" in uris,
       "mailto:mahendrakarpranay@gmail.com" in uris), (True, True))
check("the phone number is a tel link", "tel:+916361723454" in uris, True)
check("another author's paper never gets these contact details",
      M.contact_details("title: T\n", "Doe, Jane"), [])
check("a paper's own front matter overrides the standing email",
      M.contact_details("email: me@x.org\nphone: ''\n", "Mahendrakar, Pranay")[0], "me@x.org")
check("the body's own H1 and author line are not repeated",
      p1.count("A Test Paper About Layout"), 1)

# -- two columns -------------------------------------------------------------
lines = [fitz.Rect(l["bbox"]) for b in doc[1].get_text("dict")["blocks"]
         for l in b.get("lines", []) if M.BODY.y0 <= l["bbox"][1] <= M.BODY.y1]
left = [r for r in lines if r.x0 < M.BODY.x0 + 5 and r.x1 <= M.BODY.x0 + M.COLW + 1]
right = [r for r in lines if r.x0 >= M.BODY.x1 - M.COLW - 1]
check("body text is set in a left column", len(left) > 10, True)
check("and a right column", len(right) > 10, True)

# -- spanning blocks and their captions --------------------------------------
t_page = next(i for i, t in enumerate(text) if "Values from the sources" in t)
check("the wide table's caption is on the table's page",
      "Author 1 (2026)" in text[t_page], True)
check("the table's last row is not dropped", "LASTROW" in alltext, True)
row = next(l for b in doc[t_page].get_text("dict")["blocks"] for l in b.get("lines", [])
           if "Author 1 (2026)" in "".join(s["text"] for s in l["spans"]))
check("a wide table spans both columns",
      any(fitz.Rect(l["bbox"]).x1 > M.BODY.x0 + M.COLW + 20
          for b in doc[t_page].get_text("dict")["blocks"] for l in b.get("lines", [])
          if abs(l["bbox"][1] - row["bbox"][1]) < 2), True)
check("a sentence about the table stays in the text, before the caption",
      alltext.index("is about the") < alltext.index("Values from the sources"), True)
check("a small table stays in the flow", "small" in alltext, True)
t3 = next(i for i, t in enumerate(text) if "tinycell" in t)
check("a caption below a narrow table also moves above it",
      0 <= text[t3].find("Table 3: a narrow table") < text[t3].find("tinycell"), True)
t2 = next(i for i, t in enumerate(text) if "RowTwo1" in t)
check("a caption below a table moves above it, on the table's page",
      0 <= text[t2].find("Table 2. The caption after") < text[t2].find("RowTwo1"), True)
check("a sentence that merely names the table is not taken as its caption",
      "Table 2 summarises the rows below" in alltext
      and not (0 <= text[t2].find("Table 2 summarises") < text[t2].find("RowTwo1")
               and text[t2].find("RowTwo1") - text[t2].find("Table 2 summarises") < 80),
      True)

imgs = [(i, info) for i, p in enumerate(doc) for info in p.get_image_info()]
widths = sorted(round(fitz.Rect(info["bbox"]).width) for _, info in imgs)
check("both figures are embedded", len(imgs), 2)
check("the wide figure spans the text width",
      abs(widths[-1] - M.BODY.width) < 3, True)
check("the narrow figure fits one column", abs(widths[0] - M.COLW) < 3, True)
check("figure captions are printed from the alt text",
      "Figure 1. A wide figure" in alltext and "Figure 2. A narrow figure" in alltext, True)
a_page = next(i for i, t in enumerate(text) if "Algorithm 1." in t)
check("the whole algorithm is on its page", "ALGEND" in text[a_page], True)
check("short code stays in the flow", "fits a column" in alltext, True)

# -- end matter, furniture, metadata ------------------------------------------
check("the AI disclosure is printed in full", "DISCLOSUREEND" in alltext, True)
check("the references are printed", "A Reference" in alltext, True)
check("page 2 carries the running head",
      "A Test Paper About Layout" in text[1] and "Life of Research" in text[1], True)
check("every page is numbered",
      all(str(i + 1) in t for i, t in enumerate(text)), True)
check("the licence line is on page 1", "Licensed under CC BY 4.0" in p1, True)
check("the PDF has title metadata", doc.metadata["title"].startswith("A Test Paper"), True)
check("the PDF has a bookmark outline", len(doc.get_toc()) >= 6, True)
check("the file is compressed (images deflated)", os.path.getsize(pdf) < 400_000, True)

_h, _f, _s, _m, plan_ = M._layout(SRC, tmp, fitz.Archive(tmp))
end_page = max(i for i, items in enumerate(plan_) if any(k == "flow" for k, _x, _r in items))
flows = [r for k, _x, r in plan_[end_page] if k == "flow"]
cols = []
for r in flows:
    ys = [l["bbox"][3] for b in doc[end_page].get_text("dict")["blocks"]
          for l in b.get("lines", []) if fitz.Rect(l["bbox"]).intersects(r)]
    cols.append(max(ys) if ys else r.y0)
check("the columns on the page where the text ends are balanced",
      len(cols) == 2 and abs(cols[0] - cols[1]) < 40, True)
check("an algorithm's body is not swallowed into its caption",
      "step 1: if the condition holds" in alltext
      and alltext.index("Algorithm 1.") < alltext.index("step 1: if the condition holds"),
      True)

# -- the reserved DOI, printed at publish time -----------------------------------
doi_pdf = os.path.join(tmp, "doi.pdf")
M.render(SRC, doi_pdf, asset_dir=tmp, doi="10.5281/zenodo.123456")
dd = fitz.open(doi_pdf)
check("a DOI passed at publish time is printed on page 1",
      "DOI 10.5281/zenodo.123456" in dd[0].get_text(), True)
check("and in the end matter", "DOI: 10.5281/zenodo.123456" in dd[-1].get_text(), True)
check("the DOI on page 1 is a link",
      "https://doi.org/10.5281/zenodo.123456" in [l.get("uri") for l in dd[0].get_links()], True)
check("without touching the markdown on disk",
      "zenodo.123456" in open(src_path, encoding="utf-8").read(), False)
check("with_doi replaces an existing doi rather than adding a second",
      M.with_doi("---\ntitle: T\ndoi: old\n---\nbody\n", "new").count("doi:"), 1)
dd.close()

# -- the completeness guard ----------------------------------------------------
# verify() must refuse a layout that lost words. Simulate one by dropping the
# last flow rect from the plan it checks against.
head, flow, spans, meta = M.build_parts(SRC, tmp)
arch = fitz.Archive(tmp)
plan = M.plan_pages(M._height(head, M.BODY.width, arch) + 1, flow, spans,
                    [M._height(s, M.BODY.width, arch) for s in spans], arch)
broken = [list(items) for items in plan]
for items in reversed(broken):
    flows = [x for x in items if x[0] == "flow"]
    if flows:
        items.remove(flows[-1])
        break
try:
    M.verify(pdf, broken, head, flow, spans, arch)
    refused = False
except RuntimeError:
    refused = True
check("a layout that lost text is refused, not written", refused, True)

# -- what the 29 Sep 2026 page-by-page review found ------------------------------
esc = M.prepare_markdown("Pick k* with pi_{k*,t} > 0, and FN*_u too.\n\nThis *stays* italic.\n")
html = M.md.Markdown(extensions=["extra"]).convert(esc)
check("mathematical stars are printed, not read as emphasis",
      ("k*" in html, "k*,t" in html, "FN*_u" in html), (True, True, True))
check("real emphasis still works", "<em>stays</em>" in html, True)
check("an already escaped star is left alone",
      M.prepare_markdown("a \\* b\n"), "a \\* b\n")
check("a word hyphenated at a line break is rejoined without a space",
      M.prepare_markdown("machine-\nchecked guarantees"), "machine-checked guarantees")
check("a code block is never rewritten",
      M.prepare_markdown("```\nx*y and a-\nb\n```\n"), "```\nx*y and a-\nb\n```\n")
check("a doubled table header keeps only the second, full one",
      M.prepare_markdown("| a | b |\n|---|---|\n| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |")
      .splitlines()[0], "| a | b | c |")

check("a spaced double hyphen is set as an en dash",
      "&#8211;" in M.typeset("<p>the question -- which one</p>"), True)
check("an exponent is set as a superscript",
      M.typeset("<p>10^-8 of it</p>"), "<p>10<sup>-8</sup> of it</p>")
check("typography never touches code",
      M.typeset("<pre><code>a -- b ^2</code></pre>"), "<pre><code>a -- b ^2</code></pre>")

check("an empty section left behind by a spanning figure is dropped",
      M.drop_empty_sections('<h2>9. End</h2><p>x</p><h2>Figure</h2><div id="span-0"></div>'
                            '<h2>References</h2><div class="refs"><p>R</p></div>'),
      '<h2>9. End</h2><p>x</p><div id="span-0"></div>'
      '<h2>References</h2><div class="refs"><p>R</p></div>')
check("a section that opens with a subsection is not empty",
      M.drop_empty_sections("<h2>7. A</h2><h3>7.1 B</h3><p>x</p>"),
      "<h2>7. A</h2><h3>7.1 B</h3><p>x</p>")

flow_ = ('<p>We plot this in Figure 1a.</p><p>Smith et al., 2025, Figure 1 differs.</p>'
         '<p>More.</p><div id="span-0"></div><p>End.</p>')
moved = M.anchor_at_first_citation(flow_, [("Figure", "1")])
check("a spanning block is anchored right at its first citation",
      moved.startswith('<p>We plot this in Figure 1a<a id="span-0"></a>.</p>'), True)
check("and its old anchor is gone", '<div id="span-0">' in moved, False)
two = ('<p>Table 1 and Figure 1 hold the numbers.</p><p>x</p>'
       '<div id="span-0"></div><p>y</p><div id="span-1"></div>')
got = M.anchor_at_first_citation(two, [("Figure", "1"), ("Table", "1")])
check("blocks cited in one sentence keep the citation order",
      got.index('id="span-1"') < got.index('id="span-0"'), True)
check("both old anchors are removed", got.count("<div"), 0)
mine = ('<h2>4. Results</h2><p>Their Table 1 reports it (Choi et al., 2025, Table 1).</p>'
        '<h2>10. Numbers</h2><p>Table 1 consolidates the values.</p><div id="span-0"></div>')
got = M.anchor_at_first_citation(mine, [("Table", "1")])
check("another paper's Table 1 never anchors this paper's table",
      got.index('id="span-0"') > got.index("10. Numbers"), True)
check("a table anchors at the sentence in its own section that introduces it",
      "Table 1<a id=\"span-0\"></a> consolidates" in got, True)
check("an indented code block keeps its stars and gets no backslashes",
      M.prepare_markdown("Text.\n\n    P(Y = k*) = pi_{k*,t}\n"),
      "Text.\n\n    P(Y = k*) = pi_{k*,t}\n")
flow2 = '<p>Text.</p><div id="span-0"></div><p>End.</p><div id="span-1"></div>'
spans2 = ['<table><tr><td>Kohli, Fig. 1a</td></tr></table>', '<div class="figure"></div>']
got = M.anchor_via_other_blocks(flow2, [("Table", "1"), ("Figure", "1")], spans2)
check("a figure cited only inside a table is anchored right after that table",
      got, '<p>Text.</p><div id="span-0"></div><a id="span-1"></a><p>End.</p>')
check("and never with a block-level anchor inside a line of text",
      M.anchor_via_other_blocks('<h3>10.1 Table 1<a id="span-0"></a>: values</h3>'
                                '<div id="span-1"></div>', [("Table", "1"), ("Figure", "1")],
                                spans2),
      '<h3>10.1 Table 1<a id="span-0"></a><a id="span-1"></a>: values</h3>')
check("scientific notation is set with a multiplication sign, unbroken",
      M.typeset("<p>p = 3 x 10^-8</p>"),
      "<p>p&#160;=&#160;3&#160;&#215;&#160;10<sup>-8</sup></p>")
check("<= and >= are set as relations",
      M.typeset("<p>abs(rho) &lt;= 0.18 and n &gt;= 3</p>"),
      "<p>abs(rho) &#8804; 0.18 and n &#8805; 3</p>")
other = ('<p>As in Table 4 of the paper.</p><div id="span-0"></div>')
check("another paper's table is not a citation of this one",
      M.anchor_at_first_citation(other, [("Table", "4")]), other)

check("the running head keeps a question-mark title whole",
      M.short_title("Who Pulls the Plug? Self-Report and Authority"), "Who Pulls the Plug?")

# A heading that would land at the foot of a column moves to the next one.
filler = "<p>" + "filler words " * 70 + "</p>"
st = M._story(filler + '<h2 id="e1">7. A heading</h2><p id="e2">' + "body " * 200 + "</p>", None)
probe = M._story(filler, None)
_more, f = probe.place(fitz.Rect(0, 0, M.COLW, 5000))
# Room below the filler for the heading (11pt above it, ~14pt tall) but not
# for two lines of its text: without the rule it would end the column.
foot = fitz.Rect(f).y1 + 36
plain = M._story(filler + '<h2 id="e1">7. A heading</h2><p id="e2">' + "body " * 200 + "</p>", None)
plain.place(fitz.Rect(0, 0, M.COLW, foot))
ev0 = []
plain.element_positions(lambda p: ev0.append(p.heading))
more_, used, ev1 = M.place_column(st, fitz.Rect(0, 0, M.COLW, foot))
check("the test really puts a heading at the column foot", 2 in ev0, True)
check("a heading at the foot of a column is moved to the next column",
      (used.y1 < foot, any(h == 2 for _i, h, _r in ev1)), (True, False))

# -- front-matter helpers ------------------------------------------------------
check("block-style keyword lists are read",
      M.fm_list("keywords:\n  - a\n  - b c\n", "keywords"), ["a", "b c"])
check("flow-style keyword lists are read", M.fm_list("keywords: [a, 'b c']", "keywords"),
      ["a", "b c"])
check("'Last, First' becomes 'First Last'", M.display_name("Doe, Jane"), "Jane Doe")
check("a name without a comma is unchanged", M.display_name("Jane Doe"), "Jane Doe")
check("the running head uses the title before the colon",
      M.short_title("Main Title: A Long Subtitle"), "Main Title")
check("ISO dates print as words", M.long_date("2026-09-05"), "5 September 2026")

shutil.rmtree(tmp, ignore_errors=True)
print("")
print("%d passed%s" % (PASS, ", %d FAILED" % FAIL if FAIL else ""))
sys.exit(1 if FAIL else 0)
