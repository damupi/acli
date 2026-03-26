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

def _text_to_adf(text: str) -> dict:
    """Convert a plain text string to Atlassian Document Format (ADF)."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


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
        fields["description"] = _text_to_adf(description)
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
    payload = {"body": _text_to_adf(body)}
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
