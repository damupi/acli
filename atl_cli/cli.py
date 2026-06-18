import json
import re
import sys

import click

from .auth import CREDS_PATH, load_credentials, save_credentials
from .client import (
    confluence_page_comment_add,
    confluence_page_comments,
    confluence_page_create,
    confluence_page_get,
    confluence_page_update,
    confluence_pages_in_space,
    confluence_recent,
    confluence_search,
    confluence_spaces,
    jira_assign,
    jira_comment,
    jira_comment_delete,
    jira_comment_update,
    jira_comments,
    jira_create,
    jira_fields,
    jira_find_user,
    jira_get,
    jira_issue_types,
    jira_search_users,
    jira_link_issues,
    jira_myself,
    jira_watch,
    jira_projects,
    jira_search,
    jira_transition,
    jira_update,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _json(data) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False))


def _adf_to_text(adf: dict | None) -> str:
    """Extract plain text from an Atlassian Document Format dict."""
    if not adf or not isinstance(adf, dict):
        return ""

    def _walk(node: dict) -> str:
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_walk(child) for child in node.get("content", [])]
        joined = "".join(parts)
        if node.get("type") in ("paragraph", "heading", "listItem", "blockquote"):
            return joined + "\n"
        return joined

    return _walk(adf).strip()


def _read_body(body: str | None, file: str | None) -> str | None:
    """Read body content from --body or --file, preferring --file."""
    if file:
        with open(file) as f:
            return f.read()
    return body


def _parse_custom_field(raw: str) -> tuple[str, object]:
    """Parse KEY=VALUE. VALUE is decoded as JSON if valid, else plain string."""
    if "=" not in raw:
        raise click.BadParameter(
            f"Must be KEY=VALUE, got: {raw!r}", param_hint="--custom-field"
        )
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise click.BadParameter(
            f"Key cannot be empty, got: {raw!r}", param_hint="--custom-field"
        )
    try:
        return key, json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return key, value


# ── CLI Root ──────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Atlassian CLI — interact with Jira and Confluence from the terminal."""


# ── auth ──────────────────────────────────────────────────────────────────────

@cli.group()
def auth():
    """Manage authentication."""


@auth.command("setup")
def auth_setup():
    """Configure and verify your Atlassian credentials.

    \b
    You need:
      - Your Atlassian account email
      - An API token from https://id.atlassian.com/manage-profile/security/api-tokens
      - Your Atlassian domain (e.g. gdcgroup.atlassian.net)
    """
    email = click.prompt("Atlassian email")
    token = click.prompt("API token", hide_input=True)
    domain = click.prompt("Atlassian domain", default="gdcgroup.atlassian.net")

    # Save preliminary credentials so the client can use them for verification
    save_credentials(email, token, domain)

    click.echo("\nVerifying credentials...")
    try:
        me = jira_myself()
    except Exception as e:
        click.echo(f"Verification failed: {e}", err=True)
        click.echo("Check your email, token, and domain — then run: atl auth setup", err=True)
        sys.exit(1)

    click.echo(f"\nSaved to {CREDS_PATH}")
    click.echo(f"  Account:  {me.get('displayName', '?')} ({me.get('emailAddress', '?')})")
    click.echo(f"  Domain:   {domain}")
    click.echo(f"  Account ID: {me.get('accountId', '?')}")


@auth.command("status")
def auth_status():
    """Show stored credentials and verify they are still valid."""
    creds = load_credentials()
    click.echo(f"Email:   {creds['email']}")
    click.echo(f"Domain:  {creds['domain']}")
    click.echo(f"Token:   {creds['token'][:12]}...")
    click.echo("\nVerifying live session...")
    try:
        me = jira_myself()
        click.echo(f"Session: valid ({me.get('displayName', '?')})")
    except Exception as e:
        click.echo(f"Session: INVALID ({e})")
        click.echo("Run: atl auth setup")


# ── jira ──────────────────────────────────────────────────────────────────────

@cli.group()
def jira():
    """Jira issue management."""


@jira.command("view")
@click.argument("key")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_view(key, as_json):
    """View a Jira issue.

    \b
    KEY is the issue key, e.g. WEBDATA-123
    """
    issue = jira_get(key)
    if as_json:
        _json(issue)
        return

    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    status = fields.get("status", {}).get("name", "?")
    issuetype = fields.get("issuetype", {}).get("name", "?")
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    priority = (fields.get("priority") or {}).get("name", "?")
    creds = load_credentials()
    url = f"https://{creds['domain']}/browse/{issue['key']}"

    description_adf = fields.get("description")
    description = _adf_to_text(description_adf) if description_adf else ""

    click.echo(f"{issue['key']}  [{issuetype}]  {status}")
    click.echo(f"Summary:    {summary}")
    click.echo(f"Assignee:   {assignee}")
    click.echo(f"Priority:   {priority}")
    click.echo(f"URL:        {url}")
    if description:
        click.echo(f"\nDescription:")
        for line in description.splitlines():
            click.echo(f"  {line}")

    custom_entries = {k: v for k, v in fields.items() if k.startswith("customfield_") and v is not None}
    if custom_entries:
        click.echo("\nCustom Fields:")
        for cf_key, cf_val in sorted(custom_entries.items()):
            if isinstance(cf_val, dict):
                if cf_val.get("type") == "doc":
                    text = _adf_to_text(cf_val)
                    display = (text[:80] + "…") if len(text) > 80 else text
                else:
                    display = cf_val.get("value") or cf_val.get("name") or cf_val.get("displayName") or json.dumps(cf_val)
            elif isinstance(cf_val, list):
                display = ", ".join(
                    (i.get("value") or i.get("name") or str(i)) if isinstance(i, dict) else str(i)
                    for i in cf_val
                ) if cf_val else ""
            else:
                display = str(cf_val)
            if display:
                click.echo(f"  {cf_key}: {display}")


jira.add_command(jira_view, name="issue")


@jira.command("search")
@click.option("--jql", required=True, help="JQL query string")
@click.option("--limit", default=20, show_default=True, help="Max results to return")
@click.option("--fields", default=None, help="Comma-separated fields (e.g. key,summary,status)")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_search_cmd(jql, limit, fields, as_json):
    """Search Jira issues using JQL.

    \b
    Examples:
      atl jira search --jql "project = WEBDATA AND status = 'In Progress'"
      atl jira search --jql "assignee = currentUser() ORDER BY updated DESC" --limit 10
    """
    field_list = [f.strip() for f in fields.split(",")] if fields else None
    issues = jira_search(jql, fields=field_list, limit=limit)
    if as_json:
        _json(issues)
        return
    if not issues:
        click.echo("No issues found.")
        return
    for issue in issues:
        f = issue.get("fields", {})
        status = f.get("status", {}).get("name", "?")
        issuetype = f.get("issuetype", {}).get("name", "?")
        summary = f.get("summary", "")
        click.echo(f"{issue['key']:<16}  {status:<16}  {issuetype:<12}  {summary}")


@jira.command("create")
@click.option("--project", required=True, help="Project key (e.g. WEBDATA)")
@click.option("--type", "issuetype", default="Task", show_default=True, help="Issue type")
@click.option("--summary", required=True, help="Issue summary / title")
@click.option("--description", "description", default=None, help="Issue description. Supports markdown: **bold**, _italic_, `code`, # headings, - lists.")
@click.option("--description-file", "description_file", default=None, help="Read description from a markdown file. Supports bold, italic, headings, lists, inline code.")
@click.option("--assignee", default=None, help="Assignee email address")
@click.option("--reporter", default=None, help="Reporter email address")
@click.option("--priority", default=None, help="Priority (e.g. High, Medium, Low)")
@click.option("--label", "labels", default=None, help="Comma-separated labels (e.g. bug,cli)")
@click.option("--custom-field", "raw_custom_fields", multiple=True, metavar="KEY=VALUE",
              help="Custom field in KEY=VALUE form (repeatable). VALUE is parsed as JSON if valid.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_create_cmd(project, issuetype, summary, description, description_file,
                    assignee, reporter, priority, labels, raw_custom_fields, as_json):
    """Create a new Jira issue."""
    desc = _read_body(description, description_file)
    label_list = [l.strip() for l in labels.split(",")] if labels else []
    custom_fields = dict(_parse_custom_field(f) for f in raw_custom_fields) if raw_custom_fields else None
    issue = jira_create(
        project=project,
        issuetype=issuetype,
        summary=summary,
        description=desc,
        assignee_email=assignee,
        reporter_email=reporter,
        priority=priority,
        labels=label_list,
        custom_fields=custom_fields,
    )
    if as_json:
        _json(issue)
        return
    creds = load_credentials()
    key = issue.get("key", "?")
    url = f"https://{creds['domain']}/browse/{key}"
    click.echo(f"Created: {key}")
    click.echo(f"URL:     {url}")


@jira.command("update")
@click.argument("key")
@click.option("--summary", default=None, help="New issue summary / title")
@click.option("--description", default=None, help="New description (markdown)")
@click.option("--description-file", "description_file", default=None, help="Read description from a markdown file")
@click.option("--priority", default=None, help="New priority (e.g. High, Medium, Low)")
@click.option("--label", "labels", default=None, help="Comma-separated labels (replaces existing)")
@click.option("--custom-field", "raw_custom_fields", multiple=True, metavar="KEY=VALUE",
              help="Custom field in KEY=VALUE form (repeatable). VALUE is parsed as JSON if valid.")
@click.option("--json", "as_json", is_flag=True)
def jira_update_cmd(key, summary, description, description_file, priority, labels, raw_custom_fields, as_json):
    """Update fields on an existing Jira issue."""
    desc = _read_body(description, description_file)
    label_list = [l.strip() for l in labels.split(",")] if labels else None
    custom_fields = dict(_parse_custom_field(f) for f in raw_custom_fields) if raw_custom_fields else None
    jira_update(key, summary=summary, description=desc, priority=priority, labels=label_list, custom_fields=custom_fields)
    if as_json:
        _json({"key": key, "updated": True})
    else:
        click.echo(f"{key} updated.")


@jira.command("comment")
@click.argument("key")
@click.argument("text", default="")
@click.option("--file", "file", default=None, help="Read comment body from a markdown file. Supports bold, italic, headings, lists, inline code, and @[Name](accountId) mentions.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_comment_cmd(key, text, file, as_json):
    """Add a comment to a Jira issue.

    \b
    KEY is the issue key, e.g. WEBDATA-123
    TEXT is the comment body (or use --file)
    """
    body = _read_body(text or None, file)
    if not body:
        click.echo("Provide comment text as argument or via --file.", err=True)
        sys.exit(1)
    result = jira_comment(key, body)
    if as_json:
        _json(result)
        return
    click.echo(f"Comment added to {key} (id: {result.get('id', '?')})")


@jira.command("comments")
@click.argument("key")
@click.option("--limit", default=50, show_default=True, help="Max comments to return")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_comments_cmd(key, limit, as_json):
    """List comments on a Jira issue.

    \b
    KEY is the issue key, e.g. WEBDATA-123
    """
    comments = jira_comments(key, limit=limit)
    if as_json:
        _json(comments)
        return
    if not comments:
        click.echo(f"No comments on {key}.")
        return
    click.echo(f"{len(comments)} comment(s) on {key}:\n")
    for c in comments:
        author = (c.get("author") or {}).get("displayName", "Unknown")
        created = c.get("created", "")[:10]
        updated = c.get("updated", "")[:10]
        comment_id = c.get("id", "?")
        body_text = _adf_to_text(c.get("body"))
        edited = f"  [edited {updated}]" if updated and updated != created else ""
        click.echo(f"── [{comment_id}] {author}  {created}{edited}")
        for line in body_text.splitlines():
            click.echo(f"   {line}")
        click.echo()


@jira.command("comment-update")
@click.argument("key")
@click.argument("comment_id")
@click.argument("text", default="")
@click.option("--file", "file", default=None, help="Read comment body from a markdown file. Supports bold, italic, headings, lists, inline code, and @[Name](accountId) mentions.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_comment_update_cmd(key, comment_id, text, file, as_json):
    """Edit an existing comment on a Jira issue.

    \b
    KEY        is the issue key, e.g. WEBDATA-123
    COMMENT_ID is the numeric comment ID
    TEXT       is the new comment body (or use --file)
    """
    body = _read_body(text or None, file)
    if not body:
        click.echo("Provide comment text as argument or via --file.", err=True)
        sys.exit(1)
    result = jira_comment_update(key, comment_id, body)
    if as_json:
        _json(result)
    else:
        click.echo(f"Comment {comment_id} on {key} updated.")


@jira.command("comment-delete")
@click.argument("key")
@click.argument("comment_id")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_comment_delete_cmd(key, comment_id, as_json):
    """Delete a comment from a Jira issue.

    \b
    KEY        is the issue key, e.g. WEBDATA-123
    COMMENT_ID is the numeric comment ID
    """
    click.confirm(f"Delete comment {comment_id} from {key}? This cannot be undone.", abort=True)
    jira_comment_delete(key, comment_id)
    if as_json:
        _json({"key": key, "comment_id": comment_id, "deleted": True})
    else:
        click.echo(f"Comment {comment_id} deleted from {key}.")


@jira.command("transition")
@click.argument("key")
@click.option("--status", "status_name", required=True, help="Target status name (e.g. 'In Progress')")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_transition_cmd(key, status_name, as_json):
    """Transition a Jira issue to a new status.

    \b
    KEY is the issue key, e.g. WEBDATA-123
    """
    jira_transition(key, status_name)
    if not as_json:
        click.echo(f"{key} transitioned to '{status_name}'")
    else:
        _json({"key": key, "status": status_name})


@jira.command("assign")
@click.argument("key")
@click.option("--to", "email", required=True, help="Assignee email address")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_assign_cmd(key, email, as_json):
    """Assign a Jira issue to a user.

    \b
    KEY is the issue key, e.g. WEBDATA-123
    """
    jira_assign(key, email)
    if not as_json:
        click.echo(f"{key} assigned to {email}")
    else:
        _json({"key": key, "assignee": email})


@jira.command("watch")
@click.argument("key")
@click.option("--user", "email", required=True, help="Watcher email address")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_watch_cmd(key, email, as_json):
    """Add a watcher to a Jira issue.

    \b
    KEY is the issue key, e.g. WEBDATA-123
    """
    try:
        jira_watch(key, email)
    except (ValueError, RuntimeError) as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    if not as_json:
        click.echo(f"{email} added as watcher to {key}")
    else:
        _json({"key": key, "watcher": email})


@jira.command("link")
@click.argument("key")
@click.option("--to", "target_key", required=True, help="Key of the issue to link to (e.g. GDCU-8290)")
@click.option("--type", "link_type", required=True, help='Link type name (e.g. "relates to", "is caused by")')
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_link_cmd(key, target_key, link_type, as_json):
    """Link two Jira issues.

    \b
    KEY is the inward issue key, e.g. WEBDATA-928
    """
    jira_link_issues(key, target_key, link_type)
    if as_json:
        _json({"inward": key, "outward": target_key, "type": link_type})
    else:
        click.echo(f"Linked {key} -> {target_key} ({link_type})")


@jira.command("users")
@click.argument("query")
@click.option("--limit", default=20, show_default=True, help="Max results to return")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_users_cmd(query, limit, as_json):
    """Search Jira users by name or email.

    \b
    QUERY is a name or email fragment, e.g. "mike" or "mike.pett@"

    Output includes accountId (for @[Name](accountId) mentions), display name,
    and email address.
    """
    users = jira_search_users(query, limit=limit)
    if as_json:
        _json(users)
        return
    if not users:
        click.echo("No users found.")
        return
    for u in users:
        account_id = u.get("accountId", "?")
        name = u.get("displayName", "?")
        email = u.get("emailAddress", "")
        click.echo(f"{account_id}  {name:<30}  {email}")


@jira.command("projects")
@click.option("--limit", default=30, show_default=True, help="Max projects to return")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_projects_cmd(limit, as_json):
    """List available Jira projects."""
    projects = jira_projects(limit=limit)
    if as_json:
        _json(projects)
        return
    if not projects:
        click.echo("No projects found.")
        return
    for p in projects:
        key = p.get("key", "?")
        name = p.get("name", "?")
        ptype = p.get("projectTypeKey", "?")
        click.echo(f"{key:<16}  {ptype:<12}  {name}")


@jira.command("issue-types")
@click.argument("project")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_issue_types_cmd(project, as_json):
    """List issue types available for a project.

    \b
    PROJECT is the project key, e.g. GDCU
    """
    types = jira_issue_types(project)
    if as_json:
        _json(types)
        return
    if not types:
        click.echo(f"No issue types found for {project}.")
        return
    for t in types:
        subtask = "  [subtask]" if t.get("subtask") else ""
        click.echo(f"{t.get('id', '?'):<6}  {t.get('name', '?')}{subtask}")


@jira.command("fields")
@click.argument("project")
@click.option("--type", "issuetype", default=None, help="Filter to a specific issue type (e.g. Task, Story)")
@click.option("--required", "required_only", is_flag=True, help="Show only required fields")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def jira_fields_cmd(project, issuetype, required_only, as_json):
    """Show fields available when creating an issue.

    \b
    PROJECT is the project key, e.g. GDCU
    Use --type to filter by issue type and --required to see only required fields.

    \b
    Examples:
      atl jira fields GDCU --required
      atl jira fields GDCU --type Task
      atl jira fields GDCU --type Story --required
    """
    fields = jira_fields(project, issuetype=issuetype, required_only=required_only)
    if as_json:
        _json(fields)
        return
    if not fields:
        msg = f"No fields found for {project}"
        if issuetype:
            msg += f" / {issuetype}"
        click.echo(msg + ".")
        return

    # Group by issue type when not filtered
    from itertools import groupby
    key_fn = lambda f: f["issuetype"]
    grouped = groupby(sorted(fields, key=key_fn), key=key_fn)
    for itype_name, itype_fields in grouped:
        click.echo(f"\n── {itype_name} ──")
        for f in sorted(itype_fields, key=lambda x: (not x["required"], x["name"])):
            req = "* " if f["required"] else "  "
            schema_type = f.get("schema", {}).get("type", "")
            allowed = f.get("allowedValues", [])
            allowed_str = ""
            if allowed:
                values = [
                    v.get("value") or v.get("name") or str(v)
                    for v in allowed[:6]
                ]
                allowed_str = "  [" + ", ".join(values)
                if len(allowed) > 6:
                    allowed_str += f", +{len(allowed) - 6} more"
                allowed_str += "]"
            click.echo(f"  {req}{f['id']:<28}  {f['name']:<30}  {schema_type}{allowed_str}")
    click.echo()
    click.echo("  * = required field")


# ── confluence ────────────────────────────────────────────────────────────────

@cli.group()
def confluence():
    """Confluence space and page management."""


@confluence.command("spaces")
@click.option("--limit", default=50, show_default=True, help="Max spaces to return")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_spaces_cmd(limit, as_json):
    """List Confluence spaces."""
    spaces = confluence_spaces(limit=limit)
    if as_json:
        _json(spaces)
        return
    if not spaces:
        click.echo("No spaces found.")
        return
    for s in spaces:
        key = s.get("key", "?")
        name = s.get("name", "?")
        space_id = s.get("id", "?")
        click.echo(f"{key:<20}  {str(space_id):<12}  {name}")


@confluence.command("search")
@click.argument("query")
@click.option("--limit", default=10, show_default=True, help="Max results to return")
@click.option("--space", "space_key", default=None, help="Restrict to a space key (e.g. WEBANALYTICS)")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_search_cmd(query, limit, space_key, as_json):
    """Search Confluence pages and content.

    \b
    QUERY is a plain text search term. It is automatically wrapped in a CQL
    fulltext query. Use --space to restrict to a single space.
    """
    cql = f'text ~ "{query}"'
    if space_key:
        cql += f' AND space.key = "{space_key}"'
    cql += " ORDER BY lastModified DESC"
    results = confluence_search(cql, limit=limit)
    if as_json:
        _json(results)
        return
    if not results:
        click.echo("No results.")
        return
    creds = load_credentials()
    for r in results:
        title = r.get("title", "?")
        space = r.get("space", {}).get("key", "?")
        page_id = r.get("id", "")
        date = (r.get("history", {}).get("lastUpdated", {}) or {}).get("when", "")[:10]
        url = f"https://{creds['domain']}/wiki{r.get('_links', {}).get('webui', '')}"
        click.echo(f"{date}  [{space:<16}]  {title}")
        click.echo(f"             {url}")


@confluence.command("recent")
@click.option("--limit", default=15, show_default=True, help="Max results to return")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_recent_cmd(limit, as_json):
    """Show pages you have recently worked on."""
    results = confluence_recent(limit=limit)
    if as_json:
        _json(results)
        return
    if not results:
        click.echo("No recent pages found.")
        return
    creds = load_credentials()
    for r in results:
        title = r.get("title", "?")
        space = r.get("space", {}).get("key", "?")
        date = (r.get("history", {}).get("lastUpdated", {}) or {}).get("when", "")[:10]
        url = f"https://{creds['domain']}/wiki{r.get('_links', {}).get('webui', '')}"
        click.echo(f"{date}  [{space:<16}]  {title}")
        click.echo(f"             {url}")


@confluence.command("pages")
@click.option("--space", "space_id", required=True, help="Space ID (numeric, from 'atl confluence spaces')")
@click.option("--limit", default=50, show_default=True, help="Max pages to return")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_pages_cmd(space_id, limit, as_json):
    """List pages in a Confluence space.

    \b
    Use 'atl confluence spaces' to find space IDs.
    """
    pages = confluence_pages_in_space(space_id, limit=limit)
    if as_json:
        _json(pages)
        return
    if not pages:
        click.echo("No pages found.")
        return
    for p in pages:
        page_id = p.get("id", "?")
        title = p.get("title", "?")
        status = p.get("status", "?")
        click.echo(f"{str(page_id):<16}  {status:<12}  {title}")


@confluence.group("page")
def confluence_page():
    """View and manage individual Confluence pages."""


@confluence_page.command("view")
@click.argument("page_id")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_page_view(page_id, as_json):
    """View a Confluence page by ID."""
    page = confluence_page_get(page_id)
    if as_json:
        _json(page)
        return
    title = page.get("title", "?")
    status = page.get("status", "?")
    version = page.get("version", {}).get("number", "?")
    space_key = page.get("spaceId", "?")
    creds = load_credentials()
    url = f"https://{creds['domain']}/wiki/spaces/{space_key}/pages/{page_id}"

    body_value = page.get("body", {}).get("storage", {}).get("value", "")

    click.echo(f"{title}")
    click.echo(f"Status:   {status}")
    click.echo(f"Version:  {version}")
    click.echo(f"URL:      {url}")
    if body_value:
        click.echo(f"\nBody (storage format):")
        # Print first 80 chars per line to avoid overwhelming terminal
        for line in body_value.splitlines()[:40]:
            click.echo(f"  {line[:120]}")


@confluence_page.command("create")
@click.option("--space", "space_id", required=True, help="Space ID (numeric)")
@click.option("--title", required=True, help="Page title")
@click.option("--body", "body", default=None, help="Page body (Confluence storage XHTML)")
@click.option("--file", "file", default=None, help="Read body from file")
@click.option("--parent", "parent_id", default=None, help="Parent page ID")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_page_create_cmd(space_id, title, body, file, parent_id, as_json):
    """Create a new Confluence page."""
    content = _read_body(body, file)
    if not content:
        click.echo("Provide page body via --body or --file.", err=True)
        sys.exit(1)
    page = confluence_page_create(
        space_id=space_id,
        title=title,
        body=content,
        parent_id=parent_id,
    )
    if as_json:
        _json(page)
        return
    creds = load_credentials()
    pid = page.get("id", "?")
    click.echo(f"Created page: {pid}")
    click.echo(f"Title:  {title}")
    click.echo(f"URL:    https://{creds['domain']}/wiki/spaces/{space_id}/pages/{pid}")


@confluence_page.command("update")
@click.argument("page_id")
@click.option("--title", required=True, help="Page title (required even if unchanged)")
@click.option("--body", "body", default=None, help="New page body (Confluence storage XHTML)")
@click.option("--file", "file", default=None, help="Read body from file")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_page_update_cmd(page_id, title, body, file, as_json):
    """Update an existing Confluence page.

    \b
    PAGE_ID is the numeric page ID.
    Version is auto-incremented — no need to supply it manually.
    """
    content = _read_body(body, file)
    if not content:
        click.echo("Provide page body via --body or --file.", err=True)
        sys.exit(1)
    page = confluence_page_update(page_id=page_id, title=title, body=content)
    if as_json:
        _json(page)
        return
    new_version = page.get("version", {}).get("number", "?")
    click.echo(f"Updated page: {page_id}")
    click.echo(f"Title:   {title}")
    click.echo(f"Version: {new_version}")


@confluence_page.command("comments")
@click.argument("page_id")
@click.option("--limit", default=50, show_default=True, help="Max comments to return")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_page_comments_cmd(page_id, limit, as_json):
    """List comments on a Confluence page.

    \b
    PAGE_ID is the numeric page ID.
    """
    comments = confluence_page_comments(page_id, limit=limit)
    if as_json:
        _json(comments)
        return
    if not comments:
        click.echo(f"No comments on page {page_id}.")
        return
    click.echo(f"{len(comments)} comment(s) on page {page_id}:\n")
    for c in comments:
        author = (c.get("history", {}).get("createdBy") or {}).get("displayName", "Unknown")
        created = (c.get("history", {}).get("createdDate", "") or "")[:10]
        comment_id = c.get("id", "?")
        body_val = (c.get("body", {}).get("storage", {}) or {}).get("value", "")
        body_text = re.sub(r"<[^>]+>", "", body_val).strip()
        click.echo(f"── [{comment_id}] {author}  {created}")
        for line in body_text.splitlines():
            if line.strip():
                click.echo(f"   {line}")
        click.echo()


@confluence_page.command("comment")
@click.argument("page_id")
@click.argument("text", default="")
@click.option("--file", "file", default=None, help="Read comment body from a file (plain text or XHTML)")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def confluence_page_comment_cmd(page_id, text, file, as_json):
    """Add a comment to a Confluence page.

    \b
    PAGE_ID is the numeric page ID.
    TEXT is the comment body (plain text), or use --file for XHTML.
    """
    body = _read_body(text or None, file)
    if not body:
        click.echo("Provide comment text as argument or via --file.", err=True)
        sys.exit(1)
    result = confluence_page_comment_add(page_id, body)
    if as_json:
        _json(result)
        return
    click.echo(f"Comment added to page {page_id} (id: {result.get('id', '?')})")
