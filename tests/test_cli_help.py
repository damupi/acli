import unittest

from click.testing import CliRunner

from atl_cli.cli import cli


class JiraMarkdownHelpTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_comment_help_documents_markdown_syntax(self):
        result = self.runner.invoke(cli, ["jira", "comment", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("> quoted text", result.output)
        self.assertIn("https://example.com", result.output)
        self.assertIn("INF-4895", result.output)
        self.assertIn('atl jira comment WEBDATA-123 "> Will take 48h"', result.output)

    def test_comment_update_help_documents_markdown_syntax(self):
        result = self.runner.invoke(cli, ["jira", "comment-update", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("> quoted text", result.output)
        self.assertIn("automatically linked URL", result.output)
        self.assertIn("automatically linked Jira issue", result.output)


if __name__ == "__main__":
    unittest.main()
