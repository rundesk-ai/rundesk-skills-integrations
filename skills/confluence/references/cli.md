---
name: confluence
description: Reading Confluence Cloud spaces, page lists, page trees, search results, and page detail.
category: providers
---

# confluence

## Entry Point

- List configured profiles: `confluence profiles`
- List visible spaces: `confluence spaces --profile example`
- List pages in a space: `confluence list --profile example --space DOCS --limit 10`
- Print a space document tree: `confluence tree --profile example --space DOCS --depth 3`
- Print a root page tree: `confluence tree --profile example --space DOCS --root EXAMPLE_PAGE_ID --depth 3`
- Search configured spaces: `confluence search --profile example --query "release notes" --limit 10`
- Search one space: `confluence search --profile example --space DOCS --query "release notes" --limit 10`
- Run explicit CQL: `confluence search --profile example --cql 'space = "DOCS" and type = page order by lastmodified desc' --limit 10`
- Fetch page detail: `confluence page EXAMPLE_PAGE_ID --profile example --full`
- Fetch page JSON with normalized body text: `confluence page EXAMPLE_PAGE_ID --profile example --full --json`

Default output is compact text for agent context. `list` prints CSV-style page rows, `tree` prints an indented page hierarchy, and `search` prints compact page cards. Use `--json` only for debugging, exports, or consumers that need raw plus normalized Confluence fields.

## Validation

- Run `python3 "$RUNDESK_SKILLS/confluence/scripts/confluence.d/test-confluence.py"`.
- Tests are offline and use synthetic fixtures; they do not need Confluence credentials.
- Optional live read-only smoke tests:
  - `confluence profiles`
  - `confluence spaces --profile example --limit 5`
  - `confluence list --profile example --space DOCS --limit 3`
  - `confluence tree --profile example --space DOCS --depth 2 --max-pages 25`
  - `confluence search --profile example --space DOCS --query "release" --limit 3`
  - `confluence page EXAMPLE_PAGE_ID --profile example --full --json`

## Provider

This integration is self-contained: its provider contract lives in this reference, not in a separate file or shared folder.

The integration reads Confluence Cloud through Atlassian REST APIs. It does not require browser login state or Atlassian CLI state. It supports multiple profiles through `.env` keys, where one profile maps to one Atlassian site/account credential and its known Confluence spaces.

Confluence may reuse the matching Jira/Atlassian credentials for a profile. Set `CONFLUENCE_<PROFILE>_*` keys only when the Confluence site or account differs from the Jira keys.

### Recommended Connection

Use an Atlassian account API token stored only in local `.env`.

Minimum Confluence read access must allow:

```text
space listing
page listing
page fetch
page children/descendants
content search
page attachments/comments metadata
```

For OAuth-style apps, relevant read scopes include `read:space:confluence`, page/content read scopes, and the read scopes needed for child content, attachments, comments, and CQL search. Exact granular scopes depend on the app model and Atlassian auth flow.

### Setup

Keep real tokens in local `.env` only. Never commit them. Restrict the selected dotenv file to the owner (`chmod 600 .env`); the CLI warns when group or other permission bits are present.

Every Confluence or reused Jira `BASE_URL` must be an HTTPS origin such as `https://example.atlassian.net`: do not include credentials, a path, query, or fragment.

Recommended local keys when Confluence reuses Jira credentials:

```dotenv
JIRA_PROFILES=example,example-two
JIRA_DEFAULT_PROFILE=example

JIRA_EXAMPLE_LABEL=Example Atlassian
JIRA_EXAMPLE_BASE_URL=https://example.atlassian.net
JIRA_EXAMPLE_EMAIL=agent@example.com
JIRA_EXAMPLE_API_TOKEN=
JIRA_EXAMPLE_PROJECTS=APP,OPS
CONFLUENCE_EXAMPLE_SPACES=DOCS,TEAM

JIRA_EXAMPLE_TWO_LABEL=Example Two Atlassian
JIRA_EXAMPLE_TWO_BASE_URL=https://example-two.atlassian.net
JIRA_EXAMPLE_TWO_EMAIL=agent@example.com
JIRA_EXAMPLE_TWO_API_TOKEN=
JIRA_EXAMPLE_TWO_PROJECTS=ENG,HELP
CONFLUENCE_EXAMPLE_TWO_SPACES=ENGDOCS,HELP
```

Optional profile-specific Confluence overrides:

```dotenv
CONFLUENCE_PROFILES=example
CONFLUENCE_DEFAULT_PROFILE=example
CONFLUENCE_EXAMPLE_LABEL=Example Docs
CONFLUENCE_EXAMPLE_BASE_URL=https://example.atlassian.net
CONFLUENCE_EXAMPLE_EMAIL=agent@example.com
CONFLUENCE_EXAMPLE_API_TOKEN=
CONFLUENCE_EXAMPLE_SPACES=DOCS,TEAM
```

### Output Shape

`list` prints one CSV row per page:

```text
id,title,space,status,version,parent,profile
EXAMPLE_CHILD_PAGE,Example release notes,DOCS,current,4,EXAMPLE_HOME_PAGE,example
```

`tree` prints one page per line with indentation for hierarchy:

```text
Confluence tree | profile=example site=Example Docs space=DOCS pages=2
- id=EXAMPLE_HOME_PAGE | space=DOCS | type=page | title=Home
  - id=EXAMPLE_CHILD_PAGE | space=DOCS | type=page | title=Example release notes
```

`page --json` includes raw page data and a `normalized` object with page id, title, profile, site, space, type, status, version, URL, ancestors, body text, and related metadata collections when `--full` is used.

### Space Mapping Rules

- `CONFLUENCE_<PROFILE>_SPACES` is the source of truth for default searchable spaces.
- `search --query` is bounded to configured spaces unless `--space`, `--all-spaces`, or `--cql` is provided.
- `list` and `tree` require `--space` to avoid broad accidental scans.
- `tree --root PAGE_ID` uses Confluence's descendants endpoint; rootless `tree --space` builds a hierarchy from pages returned for that space.

### Safety Notes

- Tokens belong in local `.env`, never committed docs, examples, or chat logs.
- Provider base URLs must be HTTPS origins. Authorization is retained only for same-origin redirects and is stripped before a cross-origin redirect.
- Some spaces contain sensitive operational notes. Keep live checks small and targeted.
- Raw `--json` output can include full page bodies and should not be pasted into public systems.
- The integration does not create, edit, move, delete, comment on, or upload Confluence content.

### Official References

- [Confluence pages](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
- [Confluence spaces](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-space/)
- [Confluence search/CQL](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/)
- [Confluence children](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-children/)
- [Confluence descendants](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-descendants/)
