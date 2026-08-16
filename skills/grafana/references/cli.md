# Grafana CLI reference

## Commands

- `grafana profiles [--json]` — list configured accounts without a network call.
- `grafana status --profile <profile>` — verify authentication and count visible Loki sources.
- `grafana datasources --profile <profile> [--limit 20]` — list visible Loki data sources.
- `grafana labels --profile <profile> --datasource <uid> [--since 1h]` — list label names.
- `grafana values <label> --profile <profile> --datasource <uid> [--since 1h] [--limit 50]` — list bounded label values.
- `grafana logs --selector '<selector>' [--contains <text>] [--exclude <text>] [--regexp <regex>] ...` — build a guarded LogQL query.
- `grafana errors --selector '<selector>' ...` — add a case-insensitive error, exception, fatal, panic, or failed filter.
- `grafana query '<logql>' ...` — run an explicit LogQL range query.

Every log read defaults to `--since 1h --limit 100 --direction backward`. `--since` accepts `s`,
`m`, `h`, or `d`, is capped at 30 days, and may not be combined with `--start` or `--end`.
Explicit bounds are RFC 3339 timestamps. Limits are capped at 1,000. A result equal to the requested
limit warns that more lines may exist. Every log query must begin with a selector containing at
least one exact non-empty label match; match-all and negative-only selectors are refused.

Text output is compact and redacts common bearer tokens, authorization headers, passwords, API
keys, access tokens, and secret assignments. `--json` returns the provider payload and is raw.

## Setup

Create a Grafana service account with the least privilege that can list and query the intended Loki
data source. A Viewer can query data sources in standard Grafana. Grafana Enterprise or Cloud can
use data-source permissions or RBAC to narrow the token to the intended source. Service accounts are
organization-scoped.

Required Rundesk-managed values:

```text
GRAFANA_BASE_URL
GRAFANA_SERVICE_ACCOUNT_TOKEN
```

Optional: `GRAFANA_LOKI_UID` chooses the ordinary Loki source and `GRAFANA_LABEL` names the account.

```dotenv
GRAFANA_BASE_URL=https://grafana.example.test
GRAFANA_SERVICE_ACCOUNT_TOKEN=
GRAFANA_LOKI_UID=logs

GRAFANA_BASE_URL__PRODUCTION=https://grafana.example.test
GRAFANA_SERVICE_ACCOUNT_TOKEN__PRODUCTION=
GRAFANA_LOKI_UID__PRODUCTION=prod-logs
```

The older dotenv spelling remains supported:

```dotenv
GRAFANA_PROFILES=production
GRAFANA_DEFAULT_PROFILE=production
GRAFANA_PRODUCTION_BASE_URL=https://grafana.example.test
GRAFANA_PRODUCTION_TOKEN=
GRAFANA_PRODUCTION_LOKI_UID=prod-logs
GRAFANA_PRODUCTION_LABEL=Production logs
```

Resolution order is the Rundesk `__ACCOUNT` spelling, the legacy infix spelling, then plain values
for the default account only. Configuration files follow `ENVIRONMENTS.md`; keep them mode `0600`.

## API and output

The command uses only read endpoints:

- `GET /api/datasources` for discovery.
- `GET /api/datasources/proxy/uid/<uid>/loki/api/v1/labels`.
- `GET /api/datasources/proxy/uid/<uid>/loki/api/v1/label/<name>/values`.
- `GET /api/datasources/proxy/uid/<uid>/loki/api/v1/query_range`.

It refuses non-HTTPS Grafana origins, embedded credentials, paths, cross-origin redirects, unknown
API routes, selectors without an exact non-empty label match, non-Loki data-source UIDs, invalid
labels, and API responses over 10 MiB.
The raw LogQL command is intentionally available because parsing the full language locally would
create a second, incomplete LogQL implementation; time and result bounds remain enforced around it.

Text log rows contain RFC 3339 UTC time, sorted stream labels, and a one-line redacted message.
`--json` includes profile, data-source UID, query, requested bounds, provider result, and raw lines.

## Investigation workflow

1. Discover the profile and Loki UID.
2. Inspect label names, then values for likely service and environment labels.
3. Query one service and a short time range.
4. Add exact text or a narrow regex; use `errors` only as a first-pass vocabulary scan.
5. Group repeats by message and labels; correlate request, trace, deployment, pod, and host fields.
6. Widen only when the bounded result is empty, and report every widened dimension.
7. Cite timestamps and stable identifiers in conclusions; treat root cause as inference unless the
   logs explicitly establish it.

## Validation

```sh
python3 "$RUNDESK_SKILLS/grafana/scripts/grafana.d/test-grafana.py" -q
"$RUNDESK_SKILLS/grafana/scripts/grafana" --help
```

Tests are offline and use synthetic Grafana/Loki responses. Optional live smoke tests are
`profiles`, `datasources --limit 5`, `labels --since 5m`, and a selector-scoped `logs --since 5m
--limit 5`. No command mutates Grafana or Loki.

Official references:

- https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/data_source/
- https://grafana.com/docs/grafana/latest/administration/service-accounts/
- https://grafana.com/docs/loki/latest/reference/loki-http-api/
- https://grafana.com/docs/loki/latest/query/
