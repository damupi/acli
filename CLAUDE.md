# Atlassian CLI (`atl`)

A command-line interface for interacting with Jira and Confluence using your
Atlassian API token. Covers issue management, transitions, comments, Confluence
pages and spaces.

## Project Structure

```
acli/
├── CLAUDE.md
├── pyproject.toml
├── atl_cli/
│   ├── __init__.py
│   ├── auth.py       # Credential storage, load/save helpers
│   ├── client.py     # HTTP client — all Jira and Confluence API calls
│   └── cli.py        # Click CLI commands
```

## Setup

```bash
pip install -e .
atl auth setup      # enter email, API token, and domain
atl auth status     # verify session is valid
```

## Auth Mechanism

Atlassian uses **HTTP Basic Auth** with your account email and an API token as
the password. No cookies, no OAuth, no session management needed.

```
Authorization: Basic base64(email:token)
```

`requests.Session.auth = (email, token)` handles this automatically.

### Credential storage

Stored at `~/.config/atl/credentials` as JSON:

```json
{
  "email": "user@domain.com",
  "token": "ATATT3x...",
  "domain": "gdcgroup.atlassian.net"
}
```

### Generating an API token

1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Give it a label (e.g. "atl-cli")
4. Copy the token immediately — it is only shown once
5. Run `atl auth setup` and paste it when prompted

## CLI Commands

```bash
# Auth
atl auth setup                                    # configure credentials
atl auth status                                   # verify live session

# Jira — Issues
atl jira view KEY                                 # view issue details
atl jira search --jql "..." [--limit 20]          # search with JQL
atl jira create --project X --summary "..."       # create an issue
atl jira comment KEY "text"                       # add a comment
atl jira transition KEY --status "In Progress"    # change issue status
atl jira assign KEY --to email                    # reassign issue
atl jira projects [--limit 30]                    # list projects

# Confluence — Spaces and Search
atl confluence spaces [--limit 50]               # list spaces
atl confluence search "query" [--limit 10]       # search pages
atl confluence recent [--limit 15]               # pages recently worked on
atl confluence pages --space SPACE_ID            # list pages in a space

# Confluence — Pages
atl confluence page view ID                      # view a page
atl confluence page create --space ID --title "Title" --body "<p>...</p>"
atl confluence page create --space ID --title "Title" --file page.html
atl confluence page update ID --title "Title" --body "<p>...</p>"
atl confluence page update ID --title "Title" --file page.html
```

All commands accept `--json` to output raw API response for scripting.

## Confirmed API Endpoints

### Jira REST API v3

Base URL: `https://{domain}/rest/api/3/`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `myself` | GET | Verify credentials, get own account info |
| `issue/{key}` | GET | Fetch a single issue |
| `issue` | POST | Create a new issue |
| `issue/{key}/comment` | POST | Add a comment |
| `issue/{key}/transitions` | GET | List available transitions |
| `issue/{key}/transitions` | POST | Perform a transition |
| `issue/{key}/assignee` | PUT | Assign an issue |
| `search` | GET | JQL search (`?jql=...&maxResults=N`) |
| `project/search` | GET | List projects |
| `user/search` | GET | Search users by email or name |

### Confluence REST API

Confluence uses two API versions depending on the operation:

| Endpoint | Version | Purpose |
|----------|---------|---------|
| `/wiki/api/v2/spaces` | v2 | List spaces |
| `/wiki/api/v2/pages/{id}` | v2 | Get a page (with `?body-format=storage`) |
| `/wiki/api/v2/spaces/{id}/pages` | v2 | List pages in a space |
| `/wiki/api/v2/pages` | v2 | Create a page |
| `/wiki/api/v2/pages/{id}` | v2 (PUT) | Update a page |
| `/wiki/rest/api/content/search` | v1 | CQL search (`?cql=...&limit=N`) |

**v2 vs v1 note:** Space listing, page CRUD, and body retrieval use the newer
`/wiki/api/v2/` endpoints. CQL search (`content/search`) remains on the v1
`/wiki/rest/api/` path because v2 search is not yet fully equivalent.

## Atlassian-specific Notes

### Issue description format (ADF)

Jira v3 requires issue descriptions in **Atlassian Document Format (ADF)**
rather than plain text or markdown. `client.py` includes `_text_to_adf()` to
wrap plain text strings in a minimal ADF document:

```python
{"type": "doc", "version": 1, "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "..."}]}
]}
```

### Confluence page body format (storage)

Confluence pages use a **storage format** — a subset of XHTML. Provide a
`<p>...</p>` block or full XHTML. `client.py` auto-wraps plain text in `<p>`
if the body does not start with `<`.

### Page updates require incrementing the version

`confluence_page_update()` fetches the current page first to read
`version.number`, then sends `version.number + 1` in the PUT payload.
Omitting this or sending the wrong version number results in a 409 conflict.

### User resolution for assignee/reporter

Jira v3 requires an `accountId` (not an email or display name) for
`assignee` and `reporter` fields. `jira_find_user(email_or_name)` calls
`GET /rest/api/3/user/search?query=...` to resolve the accountId.

### Transition IDs are not stable

Jira transition IDs vary by project and workflow configuration. `jira_transition()`
always looks them up dynamically by calling `GET /rest/api/3/issue/{key}/transitions`
and matching by `t["to"]["name"]` (case-insensitive).
