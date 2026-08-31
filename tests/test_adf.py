import unittest

from atl_cli.client import _inline_adf, _markdown_to_adf


JIRA_BASE_URL = "https://example.atlassian.net"


class MarkdownToAdfTests(unittest.TestCase):
    def test_blockquote_preserves_inline_formatting(self):
        adf = _markdown_to_adf(
            "> _A single shared account is sufficient._ See **INF-4895**.",
            jira_base_url=JIRA_BASE_URL,
        )

        quote = adf["content"][0]
        self.assertEqual(quote["type"], "blockquote")
        nodes = quote["content"][0]["content"]
        self.assertEqual(nodes[0]["text"], "A single shared account is sufficient.")
        self.assertEqual(nodes[0]["marks"], [{"type": "em"}])
        self.assertEqual(nodes[2]["text"], "INF-4895")
        self.assertEqual(nodes[2]["marks"], [{"type": "strong"}])

    def test_blockquote_supports_multiple_lines_and_paragraphs(self):
        adf = _markdown_to_adf(">first line\n> second line\n>\n> second paragraph")

        quote = adf["content"][0]
        self.assertEqual(quote["type"], "blockquote")
        self.assertEqual(len(quote["content"]), 2)
        self.assertEqual(quote["content"][0]["content"][0]["text"], "first line second line")
        self.assertEqual(quote["content"][1]["content"][0]["text"], "second paragraph")

    def test_blockquote_supports_links_code_and_mentions(self):
        adf = _markdown_to_adf(
            "> [docs](https://example.com) `INF-1` @[Jane](account-123) INF-2",
            jira_base_url=JIRA_BASE_URL,
        )
        nodes = adf["content"][0]["content"][0]["content"]

        self.assertEqual(nodes[0]["marks"][0]["type"], "link")
        self.assertEqual(nodes[2]["marks"], [{"type": "code"}])
        self.assertEqual(nodes[4]["type"], "mention")
        self.assertEqual(nodes[6]["marks"][0]["attrs"]["href"], f"{JIRA_BASE_URL}/browse/INF-2")

    def test_bare_url_is_linked_without_trailing_punctuation(self):
        nodes = _inline_adf("See https://example.com/docs?q=1, then continue.")

        self.assertEqual(nodes[1]["text"], "https://example.com/docs?q=1")
        self.assertEqual(nodes[1]["marks"][0]["attrs"]["href"], "https://example.com/docs?q=1")
        self.assertEqual(nodes[2]["text"], ",")

    def test_balanced_parenthesis_stays_in_url_and_unmatched_one_does_not(self):
        nodes = _inline_adf("(https://example.com/a_(b)).")

        self.assertEqual(nodes[1]["text"], "https://example.com/a_(b)")
        self.assertEqual(nodes[2]["text"], ").")

    def test_url_wrappers_are_not_included_in_link(self):
        for text, expected_trailing in (
            ('"https://example.com/docs"', '"'),
            ("'https://example.com/docs'", "'"),
            ("<https://example.com/docs>", ">"),
        ):
            with self.subTest(text=text):
                nodes = _inline_adf(text)
                self.assertEqual(nodes[1]["text"], "https://example.com/docs")
                self.assertEqual(nodes[2]["text"], expected_trailing)

    def test_jira_issue_key_is_linked_when_base_url_is_available(self):
        nodes = _inline_adf("Fix INF-4895 next", jira_base_url=f"{JIRA_BASE_URL}/")

        self.assertEqual(nodes[1]["text"], "INF-4895")
        self.assertEqual(nodes[1]["marks"][0]["attrs"]["href"], f"{JIRA_BASE_URL}/browse/INF-4895")

    def test_jira_issue_key_stays_plain_without_base_url(self):
        nodes = _inline_adf("Fix INF-4895 next")

        self.assertEqual(nodes, [
            {"type": "text", "text": "Fix "},
            {"type": "text", "text": "INF-4895"},
            {"type": "text", "text": " next"},
        ])

    def test_code_and_explicit_links_are_not_auto_linked(self):
        nodes = _inline_adf(
            "`https://example.com INF-1` [INF-2](https://other.example/INF-2)",
            jira_base_url=JIRA_BASE_URL,
        )

        self.assertEqual(nodes[0]["marks"], [{"type": "code"}])
        self.assertEqual(nodes[0]["text"], "https://example.com INF-1")
        self.assertEqual(nodes[2]["text"], "INF-2")
        self.assertEqual(nodes[2]["marks"][0]["attrs"]["href"], "https://other.example/INF-2")

    def test_embedded_and_lowercase_pseudo_keys_are_not_linked(self):
        nodes = _inline_adf(
            "fooINF-1 INF-2bar inf-3 A-4",
            jira_base_url=JIRA_BASE_URL,
        )

        self.assertEqual(nodes, [{"type": "text", "text": "fooINF-1 INF-2bar inf-3 A-4"}])

    def test_existing_block_elements_still_render(self):
        adf = _markdown_to_adf(
            "# Heading\n\n- item\n\n| A |\n|---|\n| B |\n\n```python\nprint('ok')\n```"
        )

        self.assertEqual(
            [node["type"] for node in adf["content"]],
            ["heading", "bulletList", "table", "codeBlock"],
        )

    def test_blank_input_produces_empty_paragraph(self):
        adf = _markdown_to_adf("")
        self.assertEqual(adf["content"][0]["type"], "paragraph")
        self.assertEqual(adf["content"][0]["content"][0]["text"], "")


if __name__ == "__main__":
    unittest.main()
