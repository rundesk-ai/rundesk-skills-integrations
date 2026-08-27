# Slack fetch sources

The external material this package is measured against. Load it when verifying, challenging, or
updating a claim about Slack's API surface, scopes, pagination, or limits. The command's own
contract is in [cli.md](./cli.md); nothing here restates it.

Each entry is Slack's own documentation for one surface this package uses or deliberately declines.

| Source | What to take from it |
|---|---|
| [Slack search help](https://slack.com/help/articles/202528808-How-to-search-in-Slack) | Search modifiers and the browser/desktop search surface. |
| [`search.messages`](https://api.slack.com/methods/search.messages) | The credentialed message-search method. |
| [`conversations.list`](https://api.slack.com/methods/conversations.list) | Conversation types, read scopes, and cursor pagination. |
| [`conversations.history`](https://api.slack.com/methods/conversations.history) | Bounded channel and DM history with Slack timestamps. |
| [`conversations.replies`](https://api.slack.com/methods/conversations.replies) | Cursor pagination, history scopes by conversation type, and current rate-limit constraints. |
| [`files.info`](https://api.slack.com/methods/files.info) | The read-only file metadata method, its `files:read` scope, and the file object's `mode`, `size`, `mimetype`, and private download URL, which is fetched by presenting the same token as a bearer credential. |
| [Slack deep linking](https://docs.slack.dev/interactivity/deep-linking/) | Supported desktop URI targets. It does not define a local message-history or desktop-cache API, which is why this package reads neither. |
| [Slack system requirements](https://slack.com/help/articles/115002037526-System-requirements-for-using-Slack.) | Supported macOS and browser clients. |
