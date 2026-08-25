# Writing a Jira ticket

Use this guide whenever you write an issue description or a comment body. A ticket is read by
someone who was not in the conversation that produced it, so it has to say what is wanted, why it
matters, and how anyone can tell it is finished — without them asking you.

The CLI converts your markdown to Atlassian Document Format, so headings, lists, and code fences
become real Jira formatting. `create` refuses a description with no headings, or one missing its
issue type's sections, and names exactly what is absent.

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

Every mutation is a dry-run until `--confirm`. Read the dry-run, confirm the sections are the ones
you intended, then confirm.

## Templates

Headings may be any level, and extra sections are always welcome. Only these are required.

### Story and Task

```markdown
## Objective

One or two sentences naming the user-visible outcome. Not the implementation.

## Background

What is true today, and what forced this onto the backlog. Link the incident, ticket, or
conversation that prompted it.

## Business value

Who is affected, how often, and what it costs to leave it alone.

## Requirements

- One idea per bullet, each independently checkable.
- Name the contract that changes: endpoint, field, table, screen, permission.
- Call out what is deliberately excluded.

## Acceptance criteria

- A product-observable condition, not a private implementation step.
- Written so a reviewer can execute it and get a yes or no.

## Open questions

- What is still undecided, and who decides it. Write `None` when nothing is open.
```

### Bug

```markdown
## Problem

One sentence: what goes wrong, for whom.

## Steps to reproduce

1. The exact sequence, from a known starting state.
2. Include the account, data, and environment needed.

## Expected

What should have happened.

## Actual

What happened instead. Paste the error in a fenced block.

## Impact

Who is hit, how often, and whether a workaround exists.

## Acceptance criteria

- The reproduction above no longer produces the actual behavior.
```

### Spike

```markdown
## Question

The single question this timeboxed work must answer.

## Context

Why the answer is needed now, and what decision waits on it.

## Timebox

The maximum effort before stopping and reporting what is known.

## Deliverable

What is handed back: a recommendation, a benchmark, a prototype, a written comparison.
```

## Accepted section names

A required section is satisfied by a top-level heading whose text matches one of these. A heading
inside a bullet or a quote does not count, and a heading that merely mentions a section name — for
example `## Not the acceptance criteria` — does not either.

| Section | Also accepted |
|---|---|
| Objective | Goal |
| Background | Context |
| Business value | Value, Why |
| Requirements | Scope |
| Acceptance criteria | Acceptance, Done when, Definition of done |
| Open questions | Questions |
| Problem | Issue, Defect |
| Steps to reproduce | Reproduction, Repro, Steps |
| Expected | Expected behavior, Expected result |
| Actual | Actual behavior, Actual result |
| Impact | Severity |
| Question | Research question |
| Timebox | Time box |
| Deliverable | Deliverables, Output |

Issue types are matched by a known word, so `Bug - Production` and `User Story` both resolve. A
type the catalog does not recognize — a custom or non-English one — still must carry at least one
heading, but its sections are not checked. The command says on stderr when that fallback applies.

`--freeform` skips the section check and sends the description as written. It still renders
markdown. Use it for a genuine exception, not to avoid writing the sections.

## How to write the content

- **One idea per bullet.** A bullet holding three requirements cannot be checked off.
- **Name the thing.** `POST /sessions/{id}` and `sessions.revoked_at`, not "the endpoint" and
  "the column".
- **Acceptance criteria describe product-observable conditions, not implementation steps.**
  `Use Redis and add unit tests` is not acceptance. `A revoked session's next request returns 401`
  is.
- **Say what is out of scope.** The reader cannot infer the boundary you had in mind.
- **Do not paste a transcript.** Summarize what it established and link it.
- **Write the summary as a statement of the outcome**, short enough to scan in a backlog.

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

Not supported: tables, panels, images, task lists, footnotes, and inline HTML. They are left as
literal text rather than dropped, so nothing you write is ever lost. An image written as
`![alt](url)` stays literal text and does not become a link; attach the file with `upload` instead.

Three deliberate differences from ordinary markdown:

- **A single newline becomes a line break.** Paragraphs are not reflowed, so the shape you write is
  the shape Jira shows.
- **`_` does not emphasize inside a word.** `JIRA_API_TOKEN` and `file_name.py` survive intact.
  Use `*emphasis*`, or `_emphasis_` surrounded by spaces.
- **Only `http`, `https`, and `mailto` links are created.** A ticket is shared and clickable, so any
  other target stays literal text.

Escape a character with a backslash when you need it literally: `\*not emphasis\*`.

## Reading a ticket back

`jira detail APP-252 --profile example --full` re-renders the stored document as markdown, so a
description written through this tool reads back exactly as it was written. A ticket written in
Jira's own editor reads back with its headings, lists, code blocks, quotes, rules, and inline
formatting intact; a construct this catalog does not render — a table, a panel, an expand — arrives
as its text without that structure. A mention or emoji reads back as its display text. Raise
`--description-limit` when a long description is being cut short.
