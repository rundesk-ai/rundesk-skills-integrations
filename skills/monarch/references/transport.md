# Monarch transport reference

Read this only when maintaining authentication or GraphQL operations, or diagnosing a Monarch API
schema change. Monarch publishes no developer API, so this contract can change without notice.

The operation shapes were checked against `bradleyseanf/monarchmoneycommunity`,
`thedavidweng/monarchmoney-cli`, `eshaffer321/monarch-go`, `robcerda/monarch-mcp-server`, and
`pulsemcp/mcp-servers`; none is imported or vendored, and the integration remains standard-library
only. Keep an operation only when at least two independent sources agree on its shape.
Do not reintroduce the rejected `SetBudgetAmount`/`budgetId` variant or the top-level-argument shapes
from `keithah/monarchmoney-ts`; multiple other sources agree on the input-object forms below.

## Authentication and GraphQL

- Use `https://api.monarch.com`; the retired `api.monarchmoney.com` causes login failures.
- Login with `POST /auth/login/` and JSON
  `{"username": <email>, "password": <password>, "trusted_device": true, "supports_mfa": true}`.
  Add `totp` only when an MFA seed is configured. MFA challenges can arrive as HTTP 401/403 or an
  error code inside HTTP 200; retry the same endpoint once with a freshly generated TOTP.
- Send `Accept: application/json`, `Content-Type: application/json`, `Client-Platform: web`, the
  integration's `User-Agent`, `Origin: https://app.monarch.com`,
  `Referer: https://app.monarch.com/`, and `device-uuid: <stable uuid4>` on login and GraphQL calls.
- Read the session token from the response's top-level `token`. Refuse JWT-shaped feature tokens.
- Send GraphQL to `POST /graphql` with JSON `{"operationName": <name>, "query": <document>,
  "variables": <object>}` and `Authorization: Token <token>`. Drop authorization on cross-origin
  redirects.
- Treat a populated top-level GraphQL `errors` array as failure even when HTTP status is 200.

## Named operations

| Command | Operation | Root field |
| --- | --- | --- |
| `accounts`, `status`, name resolution | `GetAccounts` | `accounts` |
| `networth` | `Common_GetAggregateSnapshots` | `aggregateSnapshots(filters: AggregateSnapshotFilters)` |
| `networth` fallback | `GetAggregateSnapshots` | same, without the asset/liability split |
| `transactions` | `GetTransactionsList` | `allTransactions(filters: TransactionFilterInput)` |
| `categories` | `GetCategories` | `categories` |
| `budgets` | `Common_GetJointPlanningData` | `budgetData(startMonth:, endMonth:)` |
| `cashflow` | `Web_GetCashFlowPage` | `aggregates(filters:, fillEmptyValues:)` and `aggregates(filters:, groupBy: ["categoryGroup"])` |
| `holdings` | `Web_GetHoldings` | `portfolio(input: PortfolioInput)` |
| `edit`, `tag`, `undo` read-back | `GetTransactionDrawer` | `getTransaction(id: UUID!, redirectPosted: Boolean)` |
| category-group resolution | `ManageGetCategoryGroups` | `categoryGroups` |
| tag resolution | `GetHouseholdTransactionTags` | `householdTransactionTags(search:, limit:)` |
| rule commands | `GetTransactionRules` | `transactionRules` |

Mutations are limited by the implementation's `MUTATIONS` allowlist:

| Command | Operation | Root field | Input |
| --- | --- | --- | --- |
| `edit` | `Web_TransactionDrawerUpdateTransaction` | `updateTransaction(input: UpdateTransactionMutationInput!)` | `{id, category?, name?, notes?}` |
| `tag` | `Web_SetTransactionTags` | `setTransactionTags(input: SetTransactionTagsInput!)` | `{transactionId, tagIds}` |
| `category create` | `Web_CreateCategory` | `createCategory(input: CreateCategoryInput!)` | `{group, name, icon}` |
| `rule create` | `Common_CreateTransactionRuleMutationV2` | `createTransactionRuleV2(input: CreateTransactionRuleInput!)` | `{merchantNameCriteria, setCategoryAction, applyToExistingTransactions}` |
| `rule delete` | `Common_DeleteTransactionRule` | `deleteTransactionRule(id: ID!)` | bare `id` |
| `budget set` | `Common_UpdateBudgetItem` | `updateOrCreateBudgetItem(input: UpdateOrCreateBudgetItemMutationInput!)` | `{categoryId, amount, timeframe, startDate, applyToFuture}` |

Keep these non-obvious shapes:

- `edit --category` sends `category`, and `--merchant` sends `name`.
- `setTransactionTags` replaces the full tag set; read current tags before applying additions or
  removals.
- `deleteTransactionRule` takes a bare `$id`, not an input object.
- Keep `applyToExistingTransactions` and `applyToFuture` false; either can widen one request into an
  unbounded write.
- Check both top-level GraphQL errors and a mutation payload's own `errors` field. For rule deletion,
  only explicit `deleted: false` proves failure because some successful responses omit the field.
- If the asset/liability fields on `aggregateSnapshots` are rejected, retry once with the
  date/balance-only fallback and report the missing split.
