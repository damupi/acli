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

_INLINE_RE = re.compile(r"(\*\*(.+?)\*\*|(?<!\w)_(.+?)_(?!\w)|`(.+?)`|\[([^\]]+)\]\(([^)]+)\))")


def _inline_adf(line: str) -> list[dict]:
    """Parse a single line of text into a list of ADF inline text nodes.

    Recognises **bold**, _italic_, `code`, and [label](url) spans. All other
    text is emitted as plain text nodes. Spans are processed left-to-right;
    overlapping or nested spans are not supported.

    Args:
        line: A single line of markdown text.

    Returns:
        A list of ADF text node dicts suitable for use inside a paragraph or
        heading ``content`` array.
    """
    nodes: list[dict] = []
    cursor = 0
    for m in _INLINE_RE.finditer(line):
        # Emit any literal text that precedes this match
        if m.start() > cursor:
            nodes.append({"type": "text", "text": line[cursor : m.start()]})
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
        elif raw.startswith("["):  # [label](url)
            nodes.append({
                "type": "text",
                "text": m.group(5),
                "marks": [{"type": "link", "attrs": {"href": m.group(6)}}],
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
        nodes.append({"type": "text", "text": line[cursor:]})
    # Guarantee at least one node so callers never receive an empty content list
    if not nodes:
        nodes.append({"type": "text", "text": ""})
    return nodes


def _markdown_to_adf(text: str) -> dict:
    """Convert a markdown string to Atlassian Document Format (ADF).

    Supported markdown elements:

    * ``# H1`` / ``## H2`` / ``### H3`` — heading nodes with ``attrs.level``
    * ``- item`` / ``* item`` — bulletList > listItem > paragraph
    * ``**bold**`` — strong mark
    * ``_italic_`` — em mark
    * `` `code` `` (inline) — code mark
    * Fenced code blocks (``` ... ```) — codeBlock node
    * Blank-line-separated text — separate paragraph nodes

    Inline marks (**bold**, _italic_, `code`) are also parsed inside headings
    and list items.

    Args:
        text: A markdown-formatted string.

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

        # ── heading ───────────────────────────────────────────────────────────
        heading_match = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            content.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _inline_adf(heading_text),
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
                        "content": _inline_adf(item_text),
                    }],
                })
                i += 1
            content.append({"type": "bulletList", "content": list_items})
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
        ):
            para_lines.append(lines[i])
            i += 1

        if para_lines:
            # Join lines with a space and parse inline marks
            para_text = " ".join(para_lines)
            content.append({
                "type": "paragraph",
                "content": _inline_adf(para_text),
            })

    # If nothing was produced (e.g. only blank lines), emit an empty paragraph
    if not content:
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": ""}],
        })

    return {"type": "doc", "version": 1, "content": content}


# ── Jira — Users ──────────────────────────────────────────────────────────────

def jira_find_user(email_or_name: str) -> dict:
    """Find a Jira user by email or display name.

    Uses GET /rest/api/3/user/search?query=... Returns the first match or raises
    ValueError if none found.
    """
    r = _jira("GET", "user/search", params={"query": email_or_name})
    results = r.json()
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
) -> dict:
    """Create a new Jira issue."""
    fields: dict = {
        "project": {"key": project},
        "issuetype": {"name": issuetype},
        "summary": summary,
    }
    if description:
        fields["description"] = _markdown_to_adf(description)
    if assignee_email:
        user = jira_find_user(assignee_email)
        fields["assignee"] = {"accountId": user["accountId"]}
    if reporter_email:
        user = jira_find_user(reporter_email)
        fields["reporter"] = {"accountId": user["accountId"]}
    if priority:
        fields["priority"] = {"name": priority}
    if labels:
        fields["labels"] = labels
    return _jira("POST", "issue", json={"fields": fields}).json()


def jira_search(jql: str, fields: list[str] | None = None, limit: int = 20) -> list[dict]:
    """Search Jira issues using JQL. Returns a list of issue dicts."""
    payload: dict = {
        "jql": jql,
        "maxResults": limit,
        "fields": fields or ["summary", "status", "issuetype", "assignee", "priority"],
    }
    r = _jira("POST", "search/jql", json=payload)
    return r.json().get("issues", [])


def jira_comment(key: str, body: str) -> dict:
    """Add a comment to a Jira issue."""
    payload = {"body": _markdown_to_adf(body)}
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


def jira_projects(limit: int = 50) -> list[dict]:
    """List Jira projects."""
    r = _jira("GET", "project/search", params={"maxResults": limit})
    return r.json().get("values", [])


def jira_update(
    key: str,
    summary: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    labels: list[str] | None = None,
) -> None:
    """Update fields on an existing Jira issue using PUT /rest/api/3/issue/{key}."""
    fields: dict = {}
    if summary is not None:
        fields["summary"] = summary
    if description is not None:
        fields["description"] = _markdown_to_adf(description)
    if priority is not None:
        fields["priority"] = {"name": priority}
    if labels is not None:
        fields["labels"] = labels
    if not fields:
        raise ValueError("No fields specified to update.")
    _jira("PUT", f"issue/{key}", json={"fields": fields})


def jira_comment_update(key: str, comment_id: str, body: str) -> dict:
    """Update an existing comment using PUT /rest/api/3/issue/{key}/comment/{commentId}."""
    payload = {"body": _markdown_to_adf(body)}
    return _jira("PUT", f"issue/{key}/comment/{comment_id}", json=payload).json()


def jira_comment_delete(key: str, comment_id: str) -> None:
    """Delete a comment using DELETE /rest/api/3/issue/{key}/comment/{commentId}."""
    _jira("DELETE", f"issue/{key}/comment/{comment_id}")


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
    payload = {
        "id": page_id,
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
