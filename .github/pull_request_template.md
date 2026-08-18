## Summary

<!-- State what changes and why in one or two lines. -->

-

## Scope and compatibility

- Packages changed:
- User-visible behavior:
- Preserved behavior:
- Dependencies added: none
- Credential, profile, or mutable-state changes: none
- Live service mutations: none

## Critical risk

<!-- Required for auth, credentials, privacy, destructive commands, or other critical risk. Write "None" when no critical risk applies. -->

- Risk:
- Guard:

## Validation

- [ ] `python3 -m unittest discover -s tests -v`
- [ ] Every touched package suite passes with its exact command recorded below, or no package changed.
- [ ] Every touched launcher returns zero for credential-free `--help`, or no launcher changed.
- [ ] Every touched launcher resolves from outside the repository, or no launcher changed.
- [ ] `git diff --check`
- [ ] Required GitHub checks pass for the exact head commit.

```text
# Exact package and manual verification commands with observed results
```

## Repository gates

- [ ] The diff contains no credential, customer identifier, private-project language, owner-specific path, or unrelated artifact.
- [ ] Reads remain bounded by default and report truncation explicitly.
- [ ] Mutations remain previews until exact confirmation, and ambiguous account selection is refused.
- [ ] JSON remains opt-in; default text output and compatibility impact are documented.
- [ ] No package imports, executes, or depends on a sibling package.
- [ ] Runtime code remains Python 3.9+ and standard-library only, unless the owner approved a dependency.
- [ ] Credential-free help, offline profiles, secret redaction, and configuration precedence remain intact.
- [ ] `README.md`, `manifest.json`, and `skills/` agree.
- [ ] Any required semantic `manifest.json` version change follows `RELEASING.md` and is stated below.

## Release

- Manifest version: `<before>` → `<after>`
- SemVer reason:
- Release or follow-up required after merge:

## Manual user path

<!-- Give the shortest representative command and expected result. State clearly when no live service API call was made. -->

```text

```
