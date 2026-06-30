"""
Tests for src/utils.py — md_to_html converter.

Run with:  python -m pytest tests/ -v
"""

from src.utils import md_to_html


# ── Headings ──────────────────────────────────────────────────────────────────

def test_h1():
    assert md_to_html("# Title") == "<h1>Title</h1>"


def test_h2():
    assert md_to_html("## Section") == "<h2>Section</h2>"


def test_h3():
    assert md_to_html("### Sub") == "<h3>Sub</h3>"


def test_heading_with_inline_bold():
    html = md_to_html("## **Important** Section")
    assert "<h2>" in html
    assert "<strong>Important</strong>" in html


# ── Inline formatting ─────────────────────────────────────────────────────────

def test_bold():
    assert "<strong>bold</strong>" in md_to_html("**bold**")


def test_italic():
    assert "<em>italic</em>" in md_to_html("*italic*")


def test_code():
    assert "<code>snippet</code>" in md_to_html("`snippet`")


def test_inline_mixed():
    html = md_to_html("Use **pip** or `conda`")
    assert "<strong>pip</strong>" in html
    assert "<code>conda</code>" in html


# ── Unordered lists ───────────────────────────────────────────────────────────

def test_unordered_list_structure():
    html = md_to_html("- alpha\n- beta\n- gamma")
    assert "<ul>" in html
    assert "<li>alpha</li>" in html
    assert "<li>beta</li>" in html
    assert "<li>gamma</li>" in html
    assert "</ul>" in html


def test_unordered_list_closes_before_heading():
    html = md_to_html("- item\n## Section")
    assert html.index("</ul>") < html.index("<h2>")


def test_unordered_list_closes_before_paragraph():
    html = md_to_html("- item\n\nParagraph")
    assert "</ul>" in html
    assert "<p>Paragraph</p>" in html


# ── Ordered lists ─────────────────────────────────────────────────────────────

def test_ordered_list_structure():
    html = md_to_html("1. first\n2. second\n3. third")
    assert "<ol>" in html
    assert "<li>first</li>" in html
    assert "<li>second</li>" in html
    assert "<li>third</li>" in html
    assert "</ol>" in html


# ── Tables ────────────────────────────────────────────────────────────────────

def test_table_basic():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    html = md_to_html(md)
    assert "<thead>" in html
    assert "<td>A</td>" in html
    assert "<td>1</td>" in html
    assert "</table>" in html


def test_table_separator_not_rendered_as_row():
    md = "| Col |\n|-----|\n| Val |"
    html = md_to_html(md)
    assert "-----" not in html


def test_table_multiple_data_rows():
    md = "| Name | Amount |\n|------|--------|\n| Rent | 1500 |\n| Food | 400 |"
    html = md_to_html(md)
    assert "<td>Rent</td>" in html
    assert "<td>Food</td>" in html
    assert html.count("<tr>") >= 3  # header row + 2 data rows


# ── Other block elements ──────────────────────────────────────────────────────

def test_horizontal_rule():
    assert "<hr>" in md_to_html("---")


def test_horizontal_rule_longer():
    assert "<hr>" in md_to_html("------")


def test_paragraph():
    assert "<p>hello world</p>" in md_to_html("hello world")


def test_blank_line_separates_paragraphs():
    html = md_to_html("para one\n\npara two")
    assert "<p>para one</p>" in html
    assert "<p>para two</p>" in html


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_string():
    assert md_to_html("") == ""


def test_returns_string():
    assert isinstance(md_to_html("anything"), str)


def test_multiline_mixed_content():
    md = "## Summary\n\n- Cut dining\n- Review subscriptions\n\nTotal savings: **$200**"
    html = md_to_html(md)
    assert "<h2>Summary</h2>" in html
    assert "<li>Cut dining</li>" in html
    assert "<strong>$200</strong>" in html
