# Descriptions and comments in Jira

Issue descriptions and comment bodies are markdown. The CLI converts it to Atlassian Document
Format, so headings, lists, and code fences become real Jira formatting, and reads it back as the
same markdown. The CLI does not check or impose any structure: what the description says, and how it
is laid out, is the writer's choice.

## Pass the description as a file, not as an argument

A structured description does not survive an argv string intact. Write it to a file and pass the
path:

```sh
jira create --profile example --project APP --issue-type Story \
  --summary "Revoke a session from the admin console" \
  --description-file /tmp/app-session-revoke.md
```

`--description -` reads standard input instead, and `comment` takes `--body-file` and `--body -`
the same way. Pass `--description` inline only for a genuinely short body.

Every mutation is a dry-run until `--confirm`. Read the dry-run, check the rendered body is the one
you intended, then confirm.

## Supported markdown

| You write | Jira shows |
|---|---|
| `# Heading` through `###### Heading` | headings 1-6 |
| `- item`, `* item`, `+ item` | a bullet list, nested by indent |
| `1. item`, `1) item` | a numbered list, starting at the first number |
| ` ```sql ` … ` ``` ` | a code block with that language |
| `> quoted` | a block quote |
| `---` | a horizontal rule |
| `**strong**`, `*emphasis*`, `` `code` ``, `~~struck~~` | inline formatting |
| `[label](https://example.test/a)` | a link |
| `@[Display Name]` | a native mention of that Jira user |

Not supported: tables, panels, images, task lists, footnotes, and inline HTML. They are left as
literal text rather than dropped, so nothing you write is ever lost. An image written as
`![alt](url)` stays literal text and does not become a link; attach the file with `upload` instead.

Two bounds worth knowing. Lists nest eight levels deep, and a more deeply indented item is kept as
an item of the eighth rather than nesting further. A description or comment read with
`--description-file`, `--body-file`, or `-` is bounded to 262144 characters, and a longer body is
refused rather than truncated.

Three deliberate differences from ordinary markdown:

- **A single newline becomes a line break.** Paragraphs are not reflowed, so the shape you write is
  the shape Jira shows.
- **`_` does not emphasize inside a word.** `JIRA_API_TOKEN` and `file_name.py` survive intact.
  Use `*emphasis*`, or `_emphasis_` surrounded by spaces.
- **Only `http`, `https`, and `mailto` links are created.** A ticket is shared and clickable, so any
  other target stays literal text.

Escape a character with a backslash when you need it literally: `\*not emphasis\*`.

**Mentioning someone.** `@[Alex Example]` becomes a native Jira mention: the command resolves the
display name through Jira before writing, and Jira notifies the account it names. A bare
`@Alex Example` stays plain text and notifies nobody. One description or comment carries one
mention, and a name matching no Jira account, or more than one, is refused before the ticket is
written. Write `@\[Alex Example]` to keep the characters literal; so does a complete link or image
form, so `@[Alex Example](https://example.test/a)` is still a link and
`![@[Alex Example]](https://example.test/i.png)` is still text. See
[the CLI reference](cli.md) for the resolution and preview rules.

## Reading a ticket back

`jira detail APP-252 --profile example --full` re-renders the stored document as markdown, so a
description written through this tool reads back exactly as it was written. Text that would
otherwise read as markup a second time — a paragraph starting with `#` or `-`, literal `*` or
backticks in prose, whitespace or brackets inside inline code, a `]` in a link label, a fence inside
a code block — comes back escaped, padded, or fenced wide enough that re-rendering it produces the
same ticket. A ticket written in Jira's own editor
reads back with its headings, lists, code blocks, quotes, rules, and inline formatting intact; a
construct this catalog does not render — a table, a panel, an expand — arrives as its text without
that structure. A mention or emoji reads back as its display text, so a read-back that is re-sent as a
description sends plain text rather than mentioning anyone again. Raise
`--description-limit` when a long description is being cut short.
