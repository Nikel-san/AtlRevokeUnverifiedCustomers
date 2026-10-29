# AtlRevokeUnverifiedCustomers

Remove inactive JSM portal customers from one or every Atlassian service desk.
Customers are identified by having no reported or created Jira issues, or by
being explicitly listed in an input file. Access is removed from service
desks only; Atlassian accounts are never deleted or deactivated.

Live execution is the default. Add `--dry-run` to preview removals without
changing service-desk access.

## Requirements

- Python 3.10 or later; no additional Python packages are required.
- An Atlassian Organization API key and organization ID.
- A Jira API token and Jira account email.
- Permission to read the organization directory and all Jira projects whose
  issues should count as customer activity.
- Permission to list service desks and remove customers from selected desks
  when running live.

## Environment variables

All five variables are required. Corresponding command-line options override
environment values. The organization bearer token is used only with the
Organization API; Jira site requests use Jira Basic authentication.

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
- `--input-file PATH`: CSV or newline-delimited IDs/emails to target instead
  of checking issue activity.
- `-f`, `--exclude-file PATH` (alias `--filename`): CSV allow-list of IDs or
  emails to skip; exclusions take precedence over the input list.
- `--dry-run`: preview decisions without removing customers. Live execution
  is the default.
- `--out PATH`: write the CSV report to this path. Existing files are never
  overwritten. By default, a unique timestamped filename is generated.

## Output

The script prints each customer’s email, display name, account ID, service
desk, issue activity, action, and reason, followed by a summary of desks
processed, customers scanned, candidates, would-remove/removed totals, skips,
and errors. `WARN`, `SUCCESS`, and `ERROR` labels and action statuses use color
in interactive terminals and respect `NO_COLOR`.

The CSV report contains these columns:

| Column | Description |
| --- | --- |
| `email` | Customer email address |
| `display_name` | Customer display name |
| `account_id` | Atlassian account ID |
| `service_desk` | Service-desk name |
| `issues_count` | `0`, `1+`, or why issue activity was not checked |
| `action` | `would remove`, `removed`, `skipped`, or `error` |
| `reason` | Explanation for a skipped or failed action |

Issue searches request at most one matching issue, so `issues_count` is
reported as `0` or `1+`, not as an exact count. In live mode the script
removes customer access from the selected service desk(s) only. It never
deletes or deactivates Atlassian accounts.

Transient read-only GET failures (HTTP 429 and common 5xx responses) are
retried with bounded backoff. Removal requests are never automatically retried.
