---
name: confluence
description: Use when an agent needs Confluence Cloud space, page-list, tree, search, or page-detail reads, or guarded edits to existing pages with version confirmation.
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
- Preview a page title edit: `confluence edit EXAMPLE_PAGE_ID --profile example --title "Updated title"`
- Preview a body edit from storage XHTML: `confluence edit EXAMPLE_PAGE_ID --profile example --body-file page.xhtml`
- Confirm the reviewed version: `confluence edit EXAMPLE_PAGE_ID --profile example --body-file page.xhtml --confirm --expected-version 3`

Default output is compact text for agent context. `list` prints CSV-style page rows, `tree` prints an indented page hierarchy, and `search` prints compact page cards. Use `--json` only for debugging, exports, or consumers that need raw plus normalized Confluence fields.

Edits default to a dry-run. The dry-run fetches the current page version and prints the target
space, next version, title, body character count, and body SHA-256. A confirmed edit must include
that current version in `--expected-version`; the command refuses the update if the page changed
since the preview. `--body` and `--body-file` contain Confluence storage XHTML, not Markdown. Omit
`--title` or the body option to preserve that part of the page. A body file must be one explicit
regular local file.

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

The integration reads Confluence Cloud through Atlassian REST APIs. It does not require browser login state or Atlassian CLI state. It supports multiple profiles through `.env` keys, where one profile maps to one Atlassian site/account credential and its known Confluence spaces.

Confluence may reuse the matching Jira/Atlassian credentials for an account, in either spelling, because one Atlassian API token serves both services. Set `CONFLUENCE_*` keys only when the Confluence site or account differs from the Jira keys; a Confluence value always wins over the shared Jira one.

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

#### Environment keys

Two spellings resolve, and both may be present at once.

**Rundesk-managed (preferred).** `rundesk skills configure` writes these; `rundesk.json` declares the three required names:

```text
CONFLUENCE_BASE_URL     the Atlassian site origin, such as https://example.atlassian.net
CONFLUENCE_EMAIL        the email address the API token belongs to
CONFLUENCE_API_TOKEN    an Atlassian API token
```

An account is a `__<ACCOUNT>` suffix on the same name, and the plain name is the **default** account. Optional `CONFLUENCE_SPACES` and `CONFLUENCE_LABEL` follow the same rule. A new account needs no declaration; it is discovered by scanning the environment.

```dotenv
CONFLUENCE_BASE_URL=https://example.atlassian.net
CONFLUENCE_EMAIL=agent@example.com
CONFLUENCE_API_TOKEN=
CONFLUENCE_SPACES=DOCS,TEAM

CONFLUENCE_BASE_URL__EXAMPLE_TWO=https://example-two.atlassian.net
CONFLUENCE_EMAIL__EXAMPLE_TWO=agent@example.com
CONFLUENCE_API_TOKEN__EXAMPLE_TWO=
CONFLUENCE_SPACES__EXAMPLE_TWO=ENGDOCS,HELP
CONFLUENCE_LABEL__EXAMPLE_TWO=Example Two Atlassian
```

The `JIRA_*` twins of `BASE_URL`, `EMAIL`, `API_TOKEN`, and `LABEL` resolve too, so an account configured for the `jira` skill needs no second copy of the credential: `JIRA_API_TOKEN__EXAMPLE_TWO` serves `confluence` unless `CONFLUENCE_API_TOKEN__EXAMPLE_TWO` is set. `SPACES` is Confluence-only.

**This repository's own dotenv keys.** `CONFLUENCE_<PROFILE>_<FIELD>` (and the `JIRA_<PROFILE>_<FIELD>` twin) are read directly by this command, not managed by Rundesk. They are kept so an existing dotenv keeps working, and they lose to the Rundesk spelling for the same account.

Every Confluence spelling is exhausted before the shared Jira twin is consulted, so for one field of one account the order is: `CONFLUENCE_<FIELD>__<ACCOUNT>`, then `CONFLUENCE_<ACCOUNT>_<FIELD>`, then — for the default account only — the plain `CONFLUENCE_<FIELD>`; and only then the same three under `JIRA_`. A Confluence-specific value therefore always wins over the shared Jira one, even a Rundesk-managed one: without that, a site whose Confluence lives on a different host than its Jira would silently read the wrong host.

**A named account never falls back to a plain value**, because that is how one site's URL gets paired with another site's token. The default account is the unnamed one, `default`, or the name in `CONFLUENCE_DEFAULT_PROFILE` / `JIRA_DEFAULT_PROFILE`.

Legacy local keys, when Confluence reuses Jira credentials:

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

`CONFLUENCE_PROFILES` / `JIRA_PROFILES` and `CONFLUENCE_DEFAULT_PROFILE` / `JIRA_DEFAULT_PROFILE` stay the explicit list of accounts `profiles` prints. Leave them unset to let the accounts present in the environment be discovered instead.

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

- `CONFLUENCE_SPACES__<ACCOUNT>` (or the legacy `CONFLUENCE_<PROFILE>_SPACES`, or the plain `CONFLUENCE_SPACES` for the default account) is the source of truth for default searchable spaces.
- `search --query` is bounded to configured spaces unless `--space`, `--all-spaces`, or `--cql` is provided.
- `list` and `tree` require `--space` to avoid broad accidental scans.
- `tree --root PAGE_ID` uses Confluence's descendants endpoint; rootless `tree --space` builds a hierarchy from pages returned for that space.

### Write permissions

Editing requires permission to view the page and its space, permission to update pages in the
space, and an OAuth/Connect app scope that permits Confluence page writes. Exact permission and
scope names depend on the Atlassian authentication model; the command still enforces the
profile's configured space allowlist locally.

### Safety Notes

- Tokens belong in local `.env`, never committed docs, examples, or chat logs.
- Provider base URLs must be HTTPS origins. Authorization is retained only for same-origin redirects and is stripped before a cross-origin redirect.
- Some spaces contain sensitive operational notes. Keep live checks small and targeted.
- Raw `--json` output can include full page bodies and should not be pasted into public systems.
- Page edits require `--confirm` and the exact `--expected-version` shown by a dry-run.
- Edits use one page id, preserve omitted title/body fields, and send no automatic retries for the
  confirmed update to avoid duplicate version creation after an uncertain network result.
- The integration does not create, delete, move, comment on, or upload Confluence content.

### Official References

- [Confluence pages](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
- [Confluence spaces](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-space/)
- [Confluence search/CQL](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/)
- [Confluence children](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-children/)
- [Confluence descendants](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-descendants/)
