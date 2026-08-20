---
name: grafana
description: Use when the user needs to discover Grafana Loki data sources, inspect available log labels, search bounded logs with LogQL or text filters, find recent errors, or gather production incident evidence. It is read-only and uses Grafana's authenticated data-source proxy. Do not use for changing dashboards, alerts, data sources, users, or Grafana configuration.
---

# Grafana

Run `$RUNDESK_SKILLS/grafana/scripts/grafana`; it resolves credentials itself, so never inspect or
print their source. Read `references/cli.md` only for setup, environment keys, output fields, API
behavior, LogQL examples, or validation.

Start with profiles and data-source discovery. Never guess a profile when several are configured,
and never guess a Loki UID when several are visible.

```sh
"$RUNDESK_SKILLS/grafana/scripts/grafana" profiles
"$RUNDESK_SKILLS/grafana/scripts/grafana" datasources --profile <profile> --limit 20
"$RUNDESK_SKILLS/grafana/scripts/grafana" labels --profile <profile> --datasource <uid> --since 1h
"$RUNDESK_SKILLS/grafana/scripts/grafana" values service_name --profile <profile> --datasource <uid> --since 1h --limit 50
```

Prefer structured selectors before text scanning. Begin with the narrowest known service,
environment, namespace, container, or job label, then inspect at most one hour and 100 lines. Widen
time, labels, or limits one dimension at a time and say when the command reports truncation.
Every log query must use exactly one stream selector with at least one exact non-empty label match;
discover labels and values first rather than using a match-all or multi-selector expression.

```sh
"$RUNDESK_SKILLS/grafana/scripts/grafana" logs --profile <profile> --datasource <uid> --selector '{service_name="api"}' --since 1h --contains timeout --limit 100
"$RUNDESK_SKILLS/grafana/scripts/grafana" errors --profile <profile> --datasource <uid> --selector '{service_name="api",environment="production"}' --since 30m --limit 100
"$RUNDESK_SKILLS/grafana/scripts/grafana" query '{service_name="api"} | json | status >= 500' --profile <profile> --datasource <uid> --since 15m --limit 100
```

Use `errors` for discovery, not as proof that every failure matches its built-in terms. Correlate
timestamps, labels, request or trace IDs, deployments, and repeated messages. Distinguish a log line
that reports an error from the root cause inferred from several lines.

Default text output redacts common credential-shaped values and collapses multiline entries. Raw
`--json` can contain secrets, personal data, headers, request bodies, and full log text; use it only
when explicitly needed and never paste sensitive values into chat, issues, commits, or reports.

This skill has no write command. Do not add dashboard, alert, annotation, data-source, user, or
service-account mutations without explicit owner approval.
