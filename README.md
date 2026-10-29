# Atlassian JSM customer access cleanup

`AtlRevokeUnverifiedCustomers.py` finds JSM portal customers with no issues reported or
created, or targets only the accounts in an explicitly supplied input list.
It removes access from service desks only; it never deletes or deactivates
Atlassian accounts. Runs remove access by default; pass `--dry-run` to report
the actions without making changes.

## Requirements

- Python 3.10 or later; the script uses only the standard library.
- An Atlassian Organization API key and organization ID.
- A Jira API token and Jira account email.
- Permission to read the organization directory and the Jira projects/issues
  being checked. The account used for issue search must be able to see all
  relevant projects; otherwise Jira's search results may not represent all
  issue participation.

Supply settings as command-line options or environment variables. A supplied
command-line option takes precedence. The site may be given with or without
the `https://` scheme; HTTPS is assumed when omitted.

| Option | Environment variable | Purpose |
| --- | --- | --- |
| `--token`, `--atlassian-token` | `ATLASSIAN_TOKEN` | Organization API bearer token |
| `--org`, `-o` | `ATLASSIAN_ORG` | Organization ID |
| `--site` | `ATLASSIAN_SITE` | Jira site base URL |
| `--jira-email` | `JIRA_EMAIL` | Jira account email |
| `--jira-pat` | `JIRA_PAT` | Jira API token |

All five settings are required. The Organization bearer token is sent only to
the Organization API; Jira requests use Jira Basic authentication.

## Target selection

Choose exactly one service-desk selector:

- `--service-desk-id`, `-s`: one service desk ID.
- `--project-key`, `-p`: resolve one project key to its service desk.
- `--all-service-desks`: every service desk available to the Jira account.

By default, the script checks whether each customer has any issues for which
they are reporter or creator. Customers with no matching issues are candidates.
Because the issue search requests at most one result, the report shows `0` or
`1+` for this check rather than presenting a partial result as an exact count.
`--input-file` accepts a CSV or newline-delimited list of account IDs/emails;
when supplied, matching customers are targeted without an issue search.
`--exclude-file` (also `--filename`, `-f`) accepts a CSV allow list of account
IDs/emails and always takes precedence over the target list.

Before making a customer a candidate, the script confirms the organization
directory and Jira profile identify a person account without billable or Jira
product access, and checks complete Jira application-role and group data for
roles or administrator membership. Missing or ambiguous account data is
skipped.

## Examples

Dry run against one service desk (explicitly opt in to dry-run mode):

```powershell
$env:ATLASSIAN_TOKEN = "organization-api-key"
$env:ATLASSIAN_ORG = "organization-id"
$env:ATLASSIAN_SITE = "https://your-site.atlassian.net"
$env:JIRA_EMAIL = "you@example.com"
$env:JIRA_PAT = "jira-api-token"
python .\AtlRevokeUnverifiedCustomers.py --service-desk-id 12 --dry-run
```

Dry run for a project or all service desks:

```powershell
python .\AtlRevokeUnverifiedCustomers.py --project-key HELP --dry-run
python .\AtlRevokeUnverifiedCustomers.py --all-service-desks --exclude-file .\allow-list.csv --dry-run
```

Target only IDs/emails in an input file:

```powershell
python .\AtlRevokeUnverifiedCustomers.py --all-service-desks --input-file .\targets.csv --dry-run
```

Perform removal against one service desk (live mode is the default):

```powershell
python .\AtlRevokeUnverifiedCustomers.py --service-desk-id 12
```

Each run prints per-customer decisions and a summary, and creates a uniquely
named CSV report in the current directory. Without `--dry-run`, customers are
removed from the selected service desk(s); no Atlassian account is deleted or
deactivated.
