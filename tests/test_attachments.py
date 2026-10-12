import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from click.testing import CliRunner

from atl_cli.cli import cli
from atl_cli.client import jira_attach_files


class JiraAttachmentClientTests(unittest.TestCase):
    def test_uploads_multiple_files_as_multipart_with_xsrf_header(self):
        response = Mock()
        response.json.return_value = [
            {"id": "1", "filename": "first.txt"},
            {"id": "2", "filename": "second.txt"},
        ]

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.txt"
            second = Path(directory) / "second.txt"
            first.write_text("first")
            second.write_text("second")

            uploaded_contents = []

            def inspect_request(*args, **kwargs):
                uploaded_contents.extend(
                    (field, upload[0], upload[1].read())
                    for field, upload in kwargs["files"]
                )
                return response

            with patch("atl_cli.client._jira", side_effect=inspect_request) as request:
                result = jira_attach_files("TEST-1", [first, second])
                uploaded_files = request.call_args.kwargs["files"]

                self.assertEqual(result, response.json.return_value)
                request.assert_called_once()
                self.assertEqual(request.call_args.args, ("POST", "issue/TEST-1/attachments"))
                self.assertEqual(
                    request.call_args.kwargs["headers"],
                    {"X-Atlassian-Token": "no-check", "Content-Type": None},
                )
                self.assertEqual(
                    uploaded_contents,
                    [
                        ("file", "first.txt", b"first"),
                        ("file", "second.txt", b"second"),
                    ],
                )

            self.assertTrue(all(upload[1][1].closed for upload in uploaded_files))

    def test_prepares_a_multipart_request_without_the_session_json_content_type(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'[{"id":"1","filename":"evidence.txt"}]'
        session = requests.Session()
        session.auth = ("user@example.com", "token")
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        prepared_requests = []

        def inspect_send(prepared, **kwargs):
            prepared_requests.append(prepared)
            response.request = prepared
            return response

        session.send = Mock(side_effect=inspect_send)
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "evidence.txt"
            attachment.write_text("evidence")
            with patch(
                "atl_cli.client._session",
                return_value=(session, {"domain": "example.atlassian.net"}),
            ):
                jira_attach_files("TEST-1", [attachment])

        prepared = prepared_requests[0]
        content_type = prepared.headers["Content-Type"]
        boundary = content_type.partition("boundary=")[2]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(f"--{boundary}".encode(), prepared.body)
        self.assertIn(b'name="file"; filename="evidence.txt"', prepared.body)
        self.assertIn(b"evidence", prepared.body)
        self.assertEqual(prepared.headers["X-Atlassian-Token"], "no-check")
        self.assertTrue(prepared.headers["Authorization"].startswith("Basic "))

    def test_rejects_missing_files_before_calling_jira(self):
        with patch("atl_cli.client._jira") as request:
            with self.assertRaisesRegex(ValueError, "Attachment file not found"):
                jira_attach_files("TEST-1", ["missing.txt"])

        request.assert_not_called()


class JiraAttachmentCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_attach_command_uploads_all_files(self):
        uploaded = [
            {"id": "1", "filename": "first.txt"},
            {"id": "2", "filename": "second.txt"},
        ]
        with self.runner.isolated_filesystem():
            Path("first.txt").write_text("first")
            Path("second.txt").write_text("second")
            with patch("atl_cli.cli.jira_attach_files", return_value=uploaded) as attach:
                result = self.runner.invoke(
                    cli,
                    ["jira", "attach", "TEST-1", "first.txt", "second.txt"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        attach.assert_called_once()
        self.assertEqual(attach.call_args.args[0], "TEST-1")
        self.assertEqual([path.name for path in attach.call_args.args[1]], ["first.txt", "second.txt"])
        self.assertIn("Attached: first.txt to TEST-1 (id: 1)", result.output)
        self.assertIn("Attached: second.txt to TEST-1 (id: 2)", result.output)

    def test_comment_can_upload_repeatable_attachments(self):
        uploaded = [{"id": "1", "filename": "evidence.png"}]
        with self.runner.isolated_filesystem():
            Path("evidence.png").write_bytes(b"png")
            with (
                patch("atl_cli.cli.jira_comment", return_value={"id": "99"}) as comment,
                patch("atl_cli.cli.jira_attach_files", return_value=uploaded) as attach,
            ):
                result = self.runner.invoke(
                    cli,
                    [
                        "jira",
                        "comment",
                        "TEST-1",
                        "See attached",
                        "--attach",
                        "evidence.png",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        comment.assert_called_once_with("TEST-1", "See attached")
        attach.assert_called_once()
        self.assertEqual(attach.call_args.args[0], "TEST-1")
        self.assertEqual(attach.call_args.args[1][0].name, "evidence.png")
        self.assertIn("Comment added to TEST-1 (id: 99)", result.output)
        self.assertIn("Attached: evidence.png (id: 1)", result.output)

    def test_comment_reports_partial_success_when_attachment_upload_fails(self):
        with self.runner.isolated_filesystem():
            Path("evidence.png").write_bytes(b"png")
            with (
                patch("atl_cli.cli.jira_comment", return_value={"id": "99"}),
                patch(
                    "atl_cli.cli.jira_attach_files",
                    side_effect=RuntimeError("attachments are disabled"),
                ),
            ):
                result = self.runner.invoke(
                    cli,
                    [
                        "jira",
                        "comment",
                        "TEST-1",
                        "See attached",
                        "--attach",
                        "evidence.png",
                    ],
                )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Comment added to TEST-1 (id: 99)", result.output)
        self.assertIn("Attachment upload failed: attachments are disabled", result.output)
        self.assertIn("atl jira attach TEST-1 FILE", result.output)

    def test_comment_json_reports_partial_success_when_attachment_upload_fails(self):
        with self.runner.isolated_filesystem():
            Path("evidence.png").write_bytes(b"png")
            with (
                patch("atl_cli.cli.jira_comment", return_value={"id": "99"}),
                patch(
                    "atl_cli.cli.jira_attach_files",
                    side_effect=RuntimeError("attachments are disabled"),
                ),
            ):
                result = self.runner.invoke(
                    cli,
                    [
                        "jira",
                        "comment",
                        "TEST-1",
                        "See attached",
                        "--attach",
                        "evidence.png",
                        "--json",
                    ],
                )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(
            json.loads(result.output),
            {
                "comment": {"id": "99"},
                "attachments": [],
                "attachment_error": "attachments are disabled",
            },
        )


if __name__ == "__main__":
    unittest.main()
