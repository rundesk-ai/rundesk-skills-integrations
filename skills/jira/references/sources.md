# Jira sources

The external material this package is measured against. Load it when verifying, challenging, or
updating a claim about Jira's API surface, document format, or mentions. The command's own contract
is in [cli.md](./cli.md); nothing here restates it.

Each entry is Atlassian's own documentation for one surface this package uses.

| Source | What to take from it |
|---|---|
| [Jira issue search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/) | JQL search and its pagination. |
| [Jira issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/) | Issue create, edit, read, and delete. |
| [Atlassian Document Format](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/) | The document structure descriptions and comments are rendered into and read back from. |
| [ADF mention node](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/mention/) | The native mention node a resolved `@[Display Name]` becomes. |
| [Jira user search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-user-search/) | Resolving a display name to exactly one account. |
| [Jira comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/) | Reading and adding comments. |
| [Jira attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/) | Attachment metadata, download, and upload. |
| [Jira Software boards](https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/) | Board discovery and backlog. |
| [Jira Software epics](https://developer.atlassian.com/cloud/jira/software/rest/api-group-epic/) | Epic issues and assignment. |
| [Jira Software sprints](https://developer.atlassian.com/cloud/jira/software/rest/api-group-sprint/) | Sprint discovery, state, and assignment. |
