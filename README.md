# AtlRevokeUnverifiedCustomers

Remove JSM portal customers with `loggedInStatus` exactly `PENDING_LOGIN` from
one or every Atlassian service desk. A contact must also have `agent: false`.
Logged-in customers and contacts without a confirmed customer role or pending
login status are preserved. Access is removed from service desks only;
Atlassian accounts are never deleted or deactivated.
JSM portal-only accounts (`qm:` IDs or Jira `accountType=customer`) are
identified directly and do not require an Organization directory record.

Live execution is the default. Add `--dry-run` to preview removals without
changing service-desk access.

## Requirements

- Python 3.10 or later; no additional Python packages are required.
- A Jira API token and Jira account email.
- An Atlassian Organization API key and organization ID when a non-portal
  Atlassian account requires organization-level validation.
- Permission to search issues in relevant Jira projects.
- Organization directory permission when validating non-portal Atlassian
  accounts.
- Permission to list service desks and remove customers from selected desks
  when running live.

## Environment variables

Jira settings are required. Organization settings are optional unless a
non-portal Atlassian account requires organization-level validation.
Corresponding command-line options override environment values. The
organization bearer token is used only with the Organization API; Jira site
requests use Jira Basic authentication.

| Variable | Command-line option | Purpose |
| --- | --- | --- |
| `ATLASSIAN_TOKEN` | `--token`, `--atlassian-token` | Organization API key |
| `ATLASSIAN_ORG` | `-o`, `--org` | Organization ID |
| `ATLASSIAN_SITE` | `-s`, `--site` | Jira site hostname or URL |
| `JIRA_EMAIL` | `--jira-email` | Jira service-account email |
| `JIRA_PAT` | `--jira-pat` | Jira API token |

`--site` accepts a hostname or an HTTP(S) URL. HTTPS is assumed when the
scheme is omitted.

## Usage

Set credentials in PowerShell:

```powershell
$env:ATLASSIAN_TOKEN = "organization-api-key"
$env:ATLASSIAN_ORG = "organization-id"
$env:ATLASSIAN_SITE = "your-site.atlassian.net"
$env:JIRA_EMAIL = "you@example.com"
$env:JIRA_PAT = "jira-api-token"
```

Run against one service desk, a project, or all service desks:

```powershell
python .\AtlRevokeUnverifiedCustomers.py -d 12 --dry-run
python .\AtlRevokeUnverifiedCustomers.py -p HELP --dry-run
python .\AtlRevokeUnverifiedCustomers.py -s your-site.atlassian.net --all-service-desks --dry-run
```

Target only listed account IDs/emails, while excluding an allow-list:

```powershell
python .\AtlRevokeUnverifiedCustomers.py --all-service-desks --input-file .\targets.csv -f .\allow-list.csv --dry-run
```

After reviewing a dry-run report, omit `--dry-run` to remove access:

```powershell
python .\AtlRevokeUnverifiedCustomers.py -d 12
```

## Options

- `-s`, `--site`: Jira site hostname or URL (env: `ATLASSIAN_SITE`).
- `-d`, `--service-desk-id`: process one service desk by ID.
- `-p`, `--project-key`: resolve a project key to its service desk.
- `--all-service-desks`: process every available service desk.
- `--token`, `--atlassian-token`: Organization API key (env: `ATLASSIAN_TOKEN`).
- `-o`, `--org`: Organization ID (env: `ATLASSIAN_ORG`).
- `--jira-email`: Jira service-account email (env: `JIRA_EMAIL`).
- `--jira-pat`: Jira API token (env: `JIRA_PAT`).
- `--input-file PATH`: CSV or newline-delimited IDs/emails to limit processing
  to matching contacts; matches are still removed only when their
  `loggedInStatus` is `PENDING_LOGIN` and `agent` is `false`.
- `-f`, `--exclude-file PATH` (alias `--filename`): CSV allow-list of IDs or
  emails to skip; exclusions take precedence over the input list.
- `--dry-run`: preview decisions without removing customers. Live execution
  is the default.
- `--workers N`: maximum concurrent contact enrichment and issue-history
  checks (default: 8).
- `--out PATH`: write the CSV report to this path. Existing files are never
  overwritten. By default, a unique timestamped filename is generated.

## Output

The script prints each customer’s email, display name, account ID, account
type, service desk, status, action, and reason, followed by a summary of desks
processed, customers scanned, candidates, would-remove/removed totals, skips,
and errors. `WARN`, `SUCCESS`, and `ERROR` labels and action statuses use color
in interactive terminals and respect `NO_COLOR`.

The CSV report contains these columns:

| Column | Description |
| --- | --- |
| `email` | Customer email address |
| `display_name` | Customer display name |
| `account_id` | Atlassian account ID |
| `account_type` | `customer`, `atlassian`, or the profile-reported account type |
| `service_desk` | Service-desk name |
| `status` | Contact `loggedInStatus`, including `PENDING_LOGIN` or `LOGGED_IN` |
| `action` | `would remove`, `removed`, `skipped`, or `error` |
| `reason` | Explanation for a skipped or failed action |

In live mode the script removes customer access from the selected service
desk(s) only. It never deletes or deactivates Atlassian accounts.

Customers are enumerated with the paginated
`GET /rest/servicedeskapi/servicedesk/{serviceDeskId}/customer` endpoint.
Project contact discovery uses the paginated
`GET /rest/servicedesk/1/project/{projectId}/contacts/search?query=&page=N`
endpoint and falls back to the legacy `/contacts` endpoint when search is not
available. Contacts are cached by project ID for reuse across service desks and
indexed by account ID, email, and unique normalized display name. Unresolved
customers fall back to individual searches by account ID and email; portal IDs
(`qm:`) are searched by email, not by their internal ID. A single result from
an email search is accepted even when Jira redacts its email or returns a
different identifier. Ambiguous display-name matches are not used. If bulk
discovery is unavailable, the script warns and uses the per-account fallback.

Removal requires both a canonical `PENDING_LOGIN` status and `agent: false`.
Statuses such as `has_logged_in`, `hasPendingLogin`, `loggedIn`, and similar
Atlassian aliases are normalized before validation, while `LOGGED_IN` and other
active states are preserved.
`--input-file` narrows the contacts processed but cannot override those safety
checks. Excluded and nonmatching customers remain in the report as skipped, and
filtered customers are not contact-enriched. An empty input file matches no
customers. `LOGGED_IN`, missing/unknown statuses, agents, and contacts whose
agent status is not explicitly `false` are skipped.

Every eligible candidate is checked for issue history with
`reporter = "{accountId}" OR creator = "{accountId}"` and `maxResults=1`.
Portal IDs (`qm:`) bypass JQL as specified. HTTP 400 is treated as no matching
issues; authorization failures terminate with exit code 2. If issue verification
is unavailable for any other reason, the customer is skipped and reported as
unverifiable. Non-portal Atlassian accounts must also pass the Jira
application-role and administrator-group checks, plus organization-directory
validation when required. Organization credentials are optional for portal-only
processing; when organization validation is required but its credentials are
missing, that account is skipped and processing continues.

Transient read-only GET failures (HTTP 429 and common 5xx responses) are
retried with bounded backoff. HTTPS connections are reused, and fallback
contact lookups run concurrently. Removal requests are never automatically
retried and are sent in batches of 50.
