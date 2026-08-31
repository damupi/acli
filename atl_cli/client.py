import re

import requests

from .auth import load_credentials


def _session() -> tuple[requests.Session, dict]:
    """Return a configured requests.Session and the loaded credentials."""
    creds = load_credentials()
    s = requests.Session()
    s.auth = (creds["email"], creds["token"])
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return s, creds


def _jira(method: str, path: str, **kwargs) -> requests.Response:
    """Make an authenticated request to the Jira REST API v3."""
    s, creds = _session()
    url = f"https://{creds['domain']}/rest/api/3/{path}"
    r = s.request(method, url, **kwargs)
    if not r.ok:
        raise RuntimeError(
            f"Jira API error {r.status_code} {r.request.method} {path}: {r.text[:300]}"
        )
    return r


def _confluence(method: str, path: str, **kwargs) -> requests.Response:
    """Make an authenticated request to the Confluence API.

    path must include the full /wiki/... prefix.
    """
    s, creds = _session()
    url = f"https://{creds['domain']}{path}"
    r = s.request(method, url, **kwargs)
    if not r.ok:
        raise RuntimeError(
            f"Confluence API error {r.status_code} {r.request.method} {path}: {r.text[:300]}"
        )
    return r


# ── Auth ──────────────────────────────────────────────────────────────────────

def jira_myself() -> dict:
    """Return the authenticated user's profile. Used to verify credentials."""
    return _jira("GET", "myself").json()


# ── ADF helper ────────────────────────────────────────────────────────────────

_INLINE_RE = re.compile(r"(\*\*(.+?)\*\*|(?<!\w)_(.+?)_(?!\w)|`(.+?)`|@\[([^\]]+)\]\(([^)]+)\)|\[([^\]]+)\]\(([^)]+)\))")
_AUTO_LINK_RE = re.compile(
    r"https?://[^\s<]+|(?<![A-Za-z0-9-])[A-Z][A-Z0-9]+-\d+(?![A-Za-z0-9-])"
)
_URL_TRAILING_PUNCTUATION = ".,;:!?\"'>"
_URL_BRACKETS = {")": "(", "]": "[", "}": "{"}


def _trim_url(url: str) -> tuple[str, str]:
    """Split punctuation that is not part of a bare URL from its end."""
    end = len(url)
    while end:
        closing = url[end - 1]
        if closing in _URL_TRAILING_PUNCTUATION:
            end -= 1
            continue
        if closing in _URL_BRACKETS:
            opening = _URL_BRACKETS[closing]
            candidate = url[:end]
            if candidate.count(closing) > candidate.count(opening):
                end -= 1
                continue
        break
    return url[:end], url[end:]


def _plain_text_adf(text: str, jira_base_url: str | None = None) -> list[dict]:
    """Convert plain text to ADF nodes, adding links for URLs and Jira keys."""
    nodes: list[dict] = []
    cursor = 0
    for match in _AUTO_LINK_RE.finditer(text):
        if match.start() > cursor:
            nodes.append({"type": "text", "text": text[cursor:match.start()]})

        value = match.group(0)
        if value.startswith(("http://", "https://")):
            link_text, trailing = _trim_url(value)
            nodes.append({
                "type": "text",
                "text": link_text,
                "marks": [{"type": "link", "attrs": {"href": link_text}}],
            })
            if trailing:
                nodes.append({"type": "text", "text": trailing})
        elif jira_base_url:
            nodes.append({
                "type": "text",
                "text": value,
                "marks": [{
                    "type": "link",
                    "attrs": {"href": f"{jira_base_url.rstrip('/')}/browse/{value}"},
                }],
            })
        else:
            nodes.append({"type": "text", "text": value})
        cursor = match.end()

    if cursor < len(text):
        nodes.append({"type": "text", "text": text[cursor:]})
    return nodes


def _inline_adf(line: str, jira_base_url: str | None = None) -> list[dict]:
    """Parse a single line of text into a list of ADF inline text nodes.

    Recognises **bold**, _italic_, `code`, [label](url), mentions, bare URLs,
    and Jira issue keys. Spans are processed left-to-right; overlapping or
    nested spans are not supported. Jira keys remain plain text when no Jira
    base URL is supplied.

    Args:
        line: A single line of markdown text.
        jira_base_url: Jira site URL used to build issue links, if available.

    Returns:
        A list of ADF text node dicts suitable for use inside a paragraph or
        heading ``content`` array.
    """
    nodes: list[dict] = []
    cursor = 0
    for m in _INLINE_RE.finditer(line):
        # Emit any literal text that precedes this match, auto-linking bare URLs
        # and Jira issue keys without touching protected Markdown spans.
        if m.start() > cursor:
            nodes.extend(_plain_text_adf(line[cursor:m.start()], jira_base_url))
        raw = m.group(0)
        if raw.startswith("**"):
            nodes.append({
                "type": "text",
                "text": m.group(2),
                "marks": [{"type": "strong"}],
            })
        elif raw.startswith("_"):
            nodes.append({
                "type": "text",
                "text": m.group(3),
                "marks": [{"type": "em"}],
            })
        elif raw.startswith("@["):  # @[Display Name](accountId) — Jira mention
            nodes.append({
                "type": "mention",
                "attrs": {"id": m.group(6), "text": f"@{m.group(5)}"},
            })
        elif raw.startswith("["):  # [label](url)
            nodes.append({
                "type": "text",
                "text": m.group(7),
                "marks": [{"type": "link", "attrs": {"href": m.group(8)}}],
            })
        else:  # backtick inline code
            nodes.append({
                "type": "text",
                "text": m.group(4),
                "marks": [{"type": "code"}],
            })
        cursor = m.end()
    # Remaining literal text after the last match
    if cursor < len(line):
        nodes.extend(_plain_text_adf(line[cursor:], jira_base_url))
    # Guarantee at least one node so callers never receive an empty content list
    if not nodes:
        nodes.append({"type": "text", "text": ""})
    return nodes


def _markdown_to_adf(text: str, jira_base_url: str | None = None) -> dict:
    """Convert a markdown string to Atlassian Document Format (ADF).

    Supported markdown elements:

    * ``# H1`` / ``## H2`` / ``### H3`` — heading nodes with ``attrs.level``
    * ``- item`` / ``* item`` — bulletList > listItem > paragraph
    * ``**bold**`` — strong mark
    * ``_italic_`` — em mark
    * `` `code` `` (inline) — code mark
    * Fenced code blocks (``` ... ```) — codeBlock node
    * Pipe tables (``| col | col |``) — table node with header row
    * ``> quoted text`` — blockquote node, including quoted paragraphs
    * Bare HTTP(S) URLs — link marks
    * Jira issue keys — link marks when ``jira_base_url`` is provided
    * Blank-line-separated text — separate paragraph nodes

    Inline marks (**bold**, _italic_, `code`, links, and mentions) are also
    parsed inside headings, blockquotes, list items, and table cells.

    Args:
        text: A markdown-formatted string.
        jira_base_url: Jira site URL used to build issue links, if available.

    Returns:
        A complete ADF document dict with ``type``, ``version``, and
        ``content`` keys.
    """
    if not text:
        return {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": ""}]}
        ]}

    content: list[dict] = []
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]

        # ── fenced code block ─────────────────────────────────────────────────
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()  # optional language hint
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            node: dict = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "\n".join(code_lines)}],
            }
            if lang:
                node["attrs"] = {"language": lang}
            content.append(node)
            i += 1  # skip closing ```
            continue

        # ── blockquote ────────────────────────────────────────────────────────
        if line.startswith(">"):
            quoted_lines: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                quoted_lines.append(re.sub(r"^> ?", "", lines[i]))
                i += 1

            paragraphs: list[list[str]] = [[]]
            for quoted_line in quoted_lines:
                if quoted_line.strip():
                    paragraphs[-1].append(quoted_line)
                elif paragraphs[-1]:
                    paragraphs.append([])

            quote_content = [
                {
                    "type": "paragraph",
                    "content": _inline_adf(" ".join(paragraph), jira_base_url),
                }
                for paragraph in paragraphs
                if paragraph
            ]
            if not quote_content:
                quote_content.append({
                    "type": "paragraph",
                    "content": [{"type": "text", "text": ""}],
                })
            content.append({"type": "blockquote", "content": quote_content})
            continue

        # ── heading ───────────────────────────────────────────────────────────
        heading_match = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            content.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _inline_adf(heading_text, jira_base_url),
            })
            i += 1
            continue

        # ── bullet list ───────────────────────────────────────────────────────
        if re.match(r"^[-*]\s+", line):
            list_items: list[dict] = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i]):
                item_text = re.sub(r"^[-*]\s+", "", lines[i])
                list_items.append({
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": _inline_adf(item_text, jira_base_url),
                    }],
                })
                i += 1
            content.append({"type": "bulletList", "content": list_items})
            continue

        # ── markdown table ────────────────────────────────────────────────────
        if re.match(r"^\|", line):
            table_lines: list[str] = []
            while i < len(lines) and re.match(r"^\|", lines[i]):
                table_lines.append(lines[i])
                i += 1

            def _parse_row(row_line: str) -> list[str]:
                # Split on | not preceded by \ to allow \| inside cell content
                parts = re.split(r"(?<!\\)\|", row_line)
                return [p.strip().replace(r"\|", "|") for p in parts[1:-1]]

            # Drop separator rows (|---|---|  or  |:---|:---:|)
            data_rows = [r for r in table_lines if not re.match(r"^\|[\s\-:|]+\|", r)]

            if data_rows:
                adf_rows: list[dict] = []
                for row_idx, row_line in enumerate(data_rows):
                    is_header = row_idx == 0
                    cell_type = "tableHeader" if is_header else "tableCell"
                    row_cells: list[dict] = []
                    for cell_text in _parse_row(row_line):
                        cell: dict = {
                            "type": cell_type,
                            "content": [{
                                "type": "paragraph",
                                "content": _inline_adf(cell_text, jira_base_url),
                            }],
                        }
                        if is_header:
                            cell["attrs"] = {"background": "#F3F3F3"}
                        row_cells.append(cell)
                    adf_rows.append({"type": "tableRow", "content": row_cells})
                content.append({
                    "type": "table",
                    "attrs": {"layout": "default"},
                    "content": adf_rows,
                })
            continue

        # ── blank line — paragraph separator, skip ────────────────────────────
        if line.strip() == "":
            i += 1
            continue

        # ── paragraph — collect consecutive non-blank, non-special lines ──────
        para_lines: list[str] = []
        while (
            i < len(lines)
            and lines[i].strip() != ""
            and not lines[i].strip().startswith("```")
            and not re.match(r"^(#{1,3})\s+", lines[i])
            and not re.match(r"^[-*]\s+", lines[i])
            and not re.match(r"^\|", lines[i])
            and not lines[i].startswith(">")
        ):
            para_lines.append(lines[i])
            i += 1

        if para_lines:
            # Join lines with a space and parse inline marks
            para_text = " ".join(para_lines)
            content.append({
                "type": "paragraph",
                "content": _inline_adf(para_text, jira_base_url),
            })

    # If nothing was produced (e.g. only blank lines), emit an empty paragraph
    if not content:
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": ""}],
        })

    return {"type": "doc", "version": 1, "content": content}


def _jira_markdown_to_adf(text: str) -> dict:
    """Convert Markdown to ADF with issue links for the configured Jira site."""
    creds = load_credentials()
    return _markdown_to_adf(text, jira_base_url=f"https://{creds['domain']}")


# ── Jira — Users ──────────────────────────────────────────────────────────────

def jira_search_users(query: str, limit: int = 20) -> list[dict]:
    """Search Jira users by name or email. Returns up to limit matches."""
    r = _jira("GET", "user/search", params={"query": query, "maxResults": limit})
    return r.json()


def jira_find_user(email_or_name: str) -> dict:
    """Find a Jira user by email or display name. Returns the first match or raises."""
    results = jira_search_users(email_or_name, limit=1)
    if not results:
        raise ValueError(f"No Jira user found matching '{email_or_name}'")
    return results[0]


# ── Jira — Issues ─────────────────────────────────────────────────────────────

def jira_get(key: str) -> dict:
    """Fetch a single Jira issue by key (e.g. WEBDATA-123)."""
    return _jira("GET", f"issue/{key}").json()


def jira_create(
    project: str,
    issuetype: str,
    summary: str,
    description: str | None = None,
    assignee_email: str | None = None,
    reporter_email: str | None = None,
    priority: str | None = None,
    labels: list[str] | None = None,
    custom_fields: dict | None = None,
    watcher_emails: list[str] | None = None,
    links: list[tuple[str, str]] | None = None,
    sprint: str | None = None,
) -> dict:
    """Create a new Jira issue.

    watcher_emails: list of email addresses to add as watchers after creation.
    links: list of (link_type, target_key) tuples to create after creation.
    sprint: sprint name (partial match accepted); resolved to customfield_10010.
    """
    fields: dict = {
        "project": {"key": project},
        "issuetype": {"name": issuetype},
        "summary": summary,
    }
    if description:
        fields["description"] = _jira_markdown_to_adf(description)
    if assignee_email:
        user = jira_find_user(assignee_email)
        fields["assignee"] = {"accountId": user["accountId"]}
    if reporter_email:
        try:
            user = jira_find_user(reporter_email)
        except ValueError:
            raise ValueError(
                f"Could not resolve reporter '{reporter_email}' — "
                "use 'atl jira users <query>' to find the correct email or name."
            )
        fields["reporter"] = {"accountId": user["accountId"]}
    if priority:
        fields["priority"] = {"name": priority}
    if labels:
        fields["labels"] = labels
    if sprint:
        sp = jira_sprint_find(project, sprint)
        fields["customfield_10010"] = {"id": sp["id"]}
    if custom_fields:
        fields.update(custom_fields)
    issue = _jira("POST", "issue", json={"fields": fields}).json()
    key = issue.get("key")
    if key:
        for email in (watcher_emails or []):
            jira_watch(key, email)
        for link_type, target_key in (links or []):
            jira_link_issues(key, target_key, link_type)
    return issue


def jira_search(jql: str, fields: list[str] | None = None, limit: int = 20) -> list[dict]:
    """Search Jira issues using JQL. Returns a list of issue dicts."""
    payload: dict = {
        "jql": jql,
        "maxResults": limit,
        "fields": fields or ["summary", "status", "issuetype", "assignee", "priority"],
    }
    r = _jira("POST", "search/jql", json=payload)
    return r.json().get("issues", [])


def jira_comments(key: str, limit: int = 50) -> list[dict]:
    """Fetch comments on a Jira issue."""
    r = _jira("GET", f"issue/{key}/comment", params={"maxResults": limit, "orderBy": "created"})
    return r.json().get("comments", [])


def jira_comment(key: str, body: str) -> dict:
    """Add a comment to a Jira issue."""
    payload = {"body": _jira_markdown_to_adf(body)}
    return _jira("POST", f"issue/{key}/comment", json=payload).json()


def jira_transition(key: str, status_name: str) -> None:
    """Transition a Jira issue to a new status by name."""
    r = _jira("GET", f"issue/{key}/transitions")
    transitions = r.json().get("transitions", [])
    match = next(
        (t for t in transitions if t["to"]["name"].lower() == status_name.lower()),
        None,
    )
    if match is None:
        available = ", ".join(t["to"]["name"] for t in transitions)
        raise ValueError(
            f"Status '{status_name}' not found for {key}. Available: {available}"
        )
    _jira("POST", f"issue/{key}/transitions", json={"transition": {"id": match["id"]}})


def jira_assign(key: str, email: str) -> None:
    """Assign a Jira issue to a user identified by email."""
    user = jira_find_user(email)
    _jira("PUT", f"issue/{key}/assignee", json={"accountId": user["accountId"]})


def jira_watchers_list(key: str) -> list[dict]:
    """Return all watchers on a Jira issue."""
    r = _jira("GET", f"issue/{key}/watchers")
    return r.json().get("watchers", [])


def jira_watch(key: str, email: str) -> None:
    """Add a watcher to a Jira issue by email."""
    user = jira_find_user(email)
    _jira("POST", f"issue/{key}/watchers", json=user["accountId"])


def jira_unwatch(key: str, email: str) -> None:
    """Remove a watcher from a Jira issue by email."""
    user = jira_find_user(email)
    _jira("DELETE", f"issue/{key}/watchers", params={"accountId": user["accountId"]})


def jira_sprint_find(project_key: str, sprint_name: str) -> dict:
    """Find a sprint by name within any scrum board for the given project.

    Returns the sprint dict (id, name, state, startDate, endDate) or raises.
    Matches case-insensitively; partial matches are accepted if unique.
    """
    s, creds = _session()
    base = f"https://{creds['domain']}/rest/agile/1.0"

    boards_r = s.get(f"{base}/board", params={"projectKeyOrId": project_key, "type": "scrum", "maxResults": 50})
    if not boards_r.ok:
        raise RuntimeError(f"Agile API error {boards_r.status_code}: {boards_r.text[:200]}")
    boards = boards_r.json().get("values", [])
    if not boards:
        raise ValueError(f"No scrum board found for project {project_key}")

    needle = sprint_name.lower()
    matches: list[dict] = []
    for board in boards:
        start = 0
        while True:
            r = s.get(
                f"{base}/board/{board['id']}/sprint",
                params={"state": "active,future,closed", "maxResults": 50, "startAt": start},
            )
            if not r.ok:
                break
            data = r.json()
            for sprint in data.get("values", []):
                if needle in sprint.get("name", "").lower():
                    matches.append(sprint)
            if data.get("isLast", True):
                break
            start += 50

    if not matches:
        raise ValueError(f"No sprint matching '{sprint_name}' found in project {project_key}")
    if len(matches) > 1:
        names = ", ".join(m["name"] for m in matches)
        raise ValueError(f"Ambiguous sprint name '{sprint_name}' — matches: {names}")
    return matches[0]


def jira_sprints_list(project_key: str, state: str = "active,future") -> list[dict]:
    """List sprints for a project's scrum board(s)."""
    s, creds = _session()
    base = f"https://{creds['domain']}/rest/agile/1.0"

    boards_r = s.get(f"{base}/board", params={"projectKeyOrId": project_key, "type": "scrum", "maxResults": 50})
    if not boards_r.ok:
        raise RuntimeError(f"Agile API error {boards_r.status_code}: {boards_r.text[:200]}")
    boards = boards_r.json().get("values", [])
    if not boards:
        raise ValueError(f"No scrum board found for project {project_key}")

    sprints: list[dict] = []
    for board in boards:
        start = 0
        while True:
            r = s.get(
                f"{base}/board/{board['id']}/sprint",
                params={"state": state, "maxResults": 50, "startAt": start},
            )
            if not r.ok:
                break
            data = r.json()
            sprints.extend(data.get("values", []))
            if data.get("isLast", True):
                break
            start += 50
    return sprints


def jira_projects(limit: int = 50) -> list[dict]:
    """List Jira projects."""
    r = _jira("GET", "project/search", params={"maxResults": limit})
    return r.json().get("values", [])


def jira_issue_types(project_key: str) -> list[dict]:
    """Return issue types available for a project."""
    r = _jira(
        "GET",
        "issue/createmeta",
        params={"projectKeys": project_key, "expand": "projects.issuetypes"},
    )
    projects = r.json().get("projects", [])
    if not projects:
        return []
    return projects[0].get("issuetypes", [])


def jira_fields(project_key: str, issuetype: str | None = None, required_only: bool = False) -> list[dict]:
    """Return fields for a project (optionally filtered by issue type and required status).

    Each returned dict has: id, name, required, schema, allowedValues.
    """
    params = {
        "projectKeys": project_key,
        "expand": "projects.issuetypes.fields",
    }
    if issuetype:
        params["issuetypeNames"] = issuetype
    r = _jira("GET", "issue/createmeta", params=params)
    projects = r.json().get("projects", [])
    if not projects:
        return []

    fields: list[dict] = []
    for itype in projects[0].get("issuetypes", []):
        for field_id, field_meta in itype.get("fields", {}).items():
            if required_only and not field_meta.get("required"):
                continue
            allowed = field_meta.get("allowedValues", [])
            fields.append({
                "id": field_id,
                "name": field_meta.get("name", field_id),
                "required": field_meta.get("required", False),
                "schema": field_meta.get("schema", {}),
                "allowedValues": allowed,
                "issuetype": itype.get("name", "?"),
            })
    return fields


def jira_update(
    key: str,
    summary: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    labels: list[str] | None = None,
    custom_fields: dict | None = None,
    sprint: str | None = None,
) -> None:
    """Update fields on an existing Jira issue using PUT /rest/api/3/issue/{key}."""
    fields: dict = {}
    if summary is not None:
        fields["summary"] = summary
    if description is not None:
        fields["description"] = _jira_markdown_to_adf(description)
    if priority is not None:
        fields["priority"] = {"name": priority}
    if labels is not None:
        fields["labels"] = labels
    if sprint is not None:
        # Derive the project key from the issue key (e.g. GDCU-123 → GDCU)
        project_key = key.split("-")[0]
        sp = jira_sprint_find(project_key, sprint)
        fields["customfield_10010"] = {"id": sp["id"]}
    if custom_fields:
        fields.update(custom_fields)
    if not fields:
        raise ValueError("No fields specified to update.")
    _jira("PUT", f"issue/{key}", json={"fields": fields})


def jira_comment_update(key: str, comment_id: str, body: str) -> dict:
    """Update an existing comment using PUT /rest/api/3/issue/{key}/comment/{commentId}."""
    payload = {"body": _jira_markdown_to_adf(body)}
    return _jira("PUT", f"issue/{key}/comment/{comment_id}", json=payload).json()


def jira_comment_delete(key: str, comment_id: str) -> None:
    """Delete a comment using DELETE /rest/api/3/issue/{key}/comment/{commentId}."""
    _jira("DELETE", f"issue/{key}/comment/{comment_id}")


_CONFLUENCE_PAGE_RE = re.compile(r"/wiki/spaces/[^/]+/pages/(\d+)")


def jira_add_remote_link(
    key: str,
    url: str,
    title: str,
    *,
    summary: str | None = None,
    icon_url: str | None = None,
    relationship: str = "relates to",
    global_id: str | None = None,
) -> dict:
    """Attach a remote (web) link to an issue's Links panel.

    POST /rest/api/3/issue/{key}/remotelink

    If global_id is not provided and url matches a Confluence page URL
    pattern (/wiki/spaces/.../pages/{pageId}/...), a globalId is
    auto-derived as "appId=confluence-page&pageId={pageId}" so re-running
    the command updates the existing remote link instead of creating a
    duplicate.
    """
    if not global_id:
        m = _CONFLUENCE_PAGE_RE.search(url)
        if m:
            global_id = f"appId=confluence-page&pageId={m.group(1)}"

    payload: dict = {
        "object": {
            "url": url,
            "title": title,
            **({"summary": summary} if summary else {}),
            **({"icon": {"url16x16": icon_url}} if icon_url else {}),
        },
        "relationship": relationship,
    }
    if global_id:
        payload["globalId"] = global_id
    return _jira("POST", f"issue/{key}/remotelink", json=payload).json()


def jira_remote_links(key: str) -> list[dict]:
    """List remote links on an issue. GET /rest/api/3/issue/{key}/remotelink"""
    return _jira("GET", f"issue/{key}/remotelink").json()


def jira_remote_link_delete(key: str, link_id: str) -> None:
    """Remove a remote link. DELETE /rest/api/3/issue/{key}/remotelink/{linkId}"""
    _jira("DELETE", f"issue/{key}/remotelink/{link_id}")


def jira_link_issues(inward_key: str, outward_key: str, link_type: str) -> None:
    """Link two issues using POST /rest/api/3/issueLink."""
    payload = {
        "type": {"name": link_type},
        "inwardIssue": {"key": inward_key},
        "outwardIssue": {"key": outward_key},
    }
    try:
        _jira("POST", "issueLink", json=payload)
    except RuntimeError as exc:
        r = _jira("GET", "issueLinkType")
        available = ", ".join(t["name"] for t in r.json().get("issueLinkTypes", []))
        raise ValueError(
            f"Link type '{link_type}' not found. Available: {available}"
        ) from exc


# ── Confluence — helpers ───────────────────────────────────────────────────────

def _wrap_body(body: str) -> str:
    """Wrap plain text in a <p> tag if it doesn't start with '<'."""
    if body and not body.lstrip().startswith("<"):
        return f"<p>{body}</p>"
    return body


# ── Confluence — Spaces ───────────────────────────────────────────────────────

def confluence_spaces(limit: int = 50) -> list[dict]:
    """List Confluence spaces using the v2 API."""
    r = _confluence("GET", "/wiki/api/v2/spaces", params={"limit": limit})
    return r.json().get("results", [])


# ── Confluence — Search ───────────────────────────────────────────────────────

def confluence_search(cql: str, limit: int = 10) -> list[dict]:
    """Search Confluence content using CQL."""
    r = _confluence(
        "GET",
        "/wiki/rest/api/content/search",
        params={"cql": cql, "limit": limit, "expand": "space,history.lastUpdated"},
    )
    return r.json().get("results", [])


# ── Confluence — Pages ────────────────────────────────────────────────────────

def confluence_page_get(page_id: str) -> dict:
    """Fetch a single Confluence page by ID (includes storage body)."""
    r = _confluence(
        "GET",
        f"/wiki/api/v2/pages/{page_id}",
        params={"body-format": "storage"},
    )
    return r.json()


def confluence_pages_in_space(space_id: str, limit: int = 50) -> list[dict]:
    """List pages in a Confluence space."""
    r = _confluence(
        "GET",
        f"/wiki/api/v2/spaces/{space_id}/pages",
        params={"limit": limit},
    )
    return r.json().get("results", [])


def confluence_page_create(
    space_id: str,
    title: str,
    body: str,
    parent_id: str | None = None,
) -> dict:
    """Create a new Confluence page in the given space."""
    payload: dict = {
        "spaceId": space_id,
        "title": title,
        "body": {
            "representation": "storage",
            "value": _wrap_body(body),
        },
    }
    if parent_id:
        payload["parentId"] = parent_id
    return _confluence("POST", "/wiki/api/v2/pages", json=payload).json()


def confluence_page_update(page_id: str, title: str, body: str) -> dict:
    """Update an existing Confluence page. Automatically increments version number."""
    current = confluence_page_get(page_id)
    current_version = current.get("version", {}).get("number", 1)
    current_status = current.get("status", "current")
    payload = {
        "id": page_id,
        "status": current_status,
        "title": title,
        "version": {"number": current_version + 1},
        "body": {
            "representation": "storage",
            "value": _wrap_body(body),
        },
    }
    return _confluence("PUT", f"/wiki/api/v2/pages/{page_id}", json=payload).json()


def confluence_recent(limit: int = 15) -> list[dict]:
    """Return pages recently modified by the current user."""
    cql = "contributor = currentUser() ORDER BY lastModified DESC"
    return confluence_search(cql, limit=limit)


# ── Confluence — Page comments ────────────────────────────────────────────────

def confluence_page_comments(page_id: str, limit: int = 50) -> list[dict]:
    """List inline and footer comments on a Confluence page."""
    r = _confluence(
        "GET",
        f"/wiki/rest/api/content/{page_id}/child/comment",
        params={
            "expand": "body.storage,version,history.createdBy",
            "limit": limit,
        },
    )
    return r.json().get("results", [])


def confluence_page_comment_add(page_id: str, body: str) -> dict:
    """Add a footer comment to a Confluence page (plain text or XHTML)."""
    if not body.lstrip().startswith("<"):
        body = f"<p>{body}</p>"
    payload = {
        "type": "comment",
        "container": {"id": page_id, "type": "page"},
        "body": {
            "storage": {
                "value": body,
                "representation": "storage",
            }
        },
    }
    return _confluence("POST", "/wiki/rest/api/content", json=payload).json()
