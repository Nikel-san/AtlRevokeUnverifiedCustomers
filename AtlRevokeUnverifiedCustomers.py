#!/usr/bin/env python3
"""Find inactive JSM portal customers and optionally remove service-desk access."""

import argparse
import base64
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ORG_API_BASE = "https://api.atlassian.com/admin/v1"
PAGE_SIZE = 100
REMOVE_BATCH_SIZE = 50
REPORT_COLUMNS = (
    "email",
    "display_name",
    "account_id",
    "service_desk",
    "issues_count",
    "action",
    "reason",
)


class ApiError(Exception):
    def __init__(self, api: str, message: str, status: int | None = None):
        super().__init__(message)
        self.api = api
        self.status = status


def request_json(
    url: str,
    *,
    api: str,
    headers: dict[str, str],
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", **headers}
    payload = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read()
    except urllib.error.HTTPError as error:
        raise ApiError(api, f"HTTP {error.code}", error.code) from None
    except (urllib.error.URLError, TimeoutError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        raise ApiError(api, f"request failed: {reason}") from None

    if not response_body:
        return {}
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise ApiError(api, f"server returned invalid JSON: {error}") from None
    if not isinstance(result, dict):
        raise ApiError(api, "server returned an unexpected response")
    return result


def paginated_values(
    url: str,
    *,
    api: str,
    headers: dict[str, str],
    next_link_host: str | None = None,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    current_url: str | None = url
    while current_url:
        if current_url in seen_urls:
            raise ApiError(api, "pagination returned a repeated page link")
        seen_urls.add(current_url)
        page = request_json(current_url, api=api, headers=headers)
        page_values = page.get("values", page.get("data"))
        if not isinstance(page_values, list):
            raise ApiError(api, "response did not contain a customer/user list")
        if not all(isinstance(item, dict) for item in page_values):
            raise ApiError(api, "response contained an invalid list item")
        values.extend(page_values)

        links = page.get("links")
        next_url = links.get("next") if isinstance(links, dict) else None
        if next_url:
            next_url = urllib.parse.urljoin(current_url, next_url)
            parsed_next_url = urllib.parse.urlparse(next_url)
            if next_link_host and (
                parsed_next_url.scheme != "https"
                or parsed_next_url.netloc != next_link_host
            ):
                raise ApiError(api, "pagination link points to an unexpected host")
            current_url = next_url
            continue

        if page.get("isLastPage") is True:
            break
        start = page.get("start")
        limit = page.get("limit")
        if isinstance(start, int) and isinstance(limit, int) and limit > 0:
            if len(page_values) == 0:
                raise ApiError(api, "pagination stopped before the last page")
            query = urllib.parse.urlparse(current_url).query
            params = urllib.parse.parse_qs(query)
            params["start"] = [str(start + limit)]
            params["limit"] = [str(limit)]
            current_url = urllib.parse.urlunparse(
                urllib.parse.urlparse(current_url)._replace(
                    query=urllib.parse.urlencode(params, doseq=True)
                )
            )
            continue
        raise ApiError(api, "response did not indicate whether more pages exist")
    return values


def read_identifiers(path: str | None) -> set[str]:
    if not path:
        return set()
    file_path = Path(path)
    try:
        if file_path.suffix.lower() == ".csv":
            with file_path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
        else:
            with file_path.open("r", encoding="utf-8-sig") as stream:
                rows = [[line.strip()] for line in stream if line.strip()]
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error}") from None

    if not rows:
        return set()
    identifiers: set[str] = set()
    header = [cell.strip().casefold().replace("-", "_") for cell in rows[0]]
    id_columns = [
        index
        for index, name in enumerate(header)
        if name in {"email", "account_id", "accountid"}
    ]
    data_rows = rows[1:] if id_columns else rows
    for row in data_rows:
        cells = [row[index] for index in id_columns if index < len(row)] if id_columns else row
        identifiers.update(cell.strip().casefold() for cell in cells if cell.strip())
    return identifiers


def resolve_config(args: argparse.Namespace) -> dict[str, str]:
    sources = {
        "token": (args.token, "ATLASSIAN_TOKEN"),
        "org": (args.org, "ATLASSIAN_ORG"),
        "site": (args.site, "ATLASSIAN_SITE"),
        "jira_email": (args.jira_email, "JIRA_EMAIL"),
        "jira_pat": (args.jira_pat, "JIRA_PAT"),
    }
    config = {
        key: (value if value is not None else os.getenv(env_name, "")).strip()
        for key, (value, env_name) in sources.items()
    }
    missing = [key for key, value in config.items() if not value.strip()]
    if missing:
        details = ", ".join(
            f"--{key.replace('_', '-')} / {sources[key][1]}" for key in missing
        )
        raise ValueError(f"missing required credentials/settings: {details}")

    if "://" not in config["site"]:
        config["site"] = f"https://{config['site']}"
    parsed_site = urllib.parse.urlparse(config["site"])
    if parsed_site.scheme not in {"http", "https"} or not parsed_site.netloc:
        raise ValueError("--site / ATLASSIAN_SITE must be an absolute HTTP(S) URL")
    config["site"] = config["site"].rstrip("/")
    return config


def org_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def jira_headers(email: str, token: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


def load_org_users(config: dict[str, str]) -> dict[str, dict[str, Any]]:
    url = (
        f"{ORG_API_BASE}/orgs/{urllib.parse.quote(config['org'], safe='')}"
        f"/users?limit={PAGE_SIZE}"
    )
    api_host = urllib.parse.urlparse(ORG_API_BASE).netloc
    records = paginated_values(
        url,
        api="organization",
        headers=org_headers(config["token"]),
        next_link_host=api_host,
    )
    return {
        str(record["account_id"]).casefold(): record
        for record in records
        if record.get("account_id")
    }


def load_service_desks(config: dict[str, str]) -> list[dict[str, Any]]:
    url = f"{config['site']}/rest/servicedeskapi/servicedesk?start=0&limit=50"
    return paginated_values(
        url,
        api="jira",
        headers=jira_headers(config["jira_email"], config["jira_pat"]),
    )


def select_service_desks(
    args: argparse.Namespace, service_desks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if args.all_service_desks:
        selected = service_desks
    elif args.service_desk_id:
        selected = [
            desk
            for desk in service_desks
            if str(desk.get("id")) == args.service_desk_id
        ]
    else:
        selected = [
            desk
            for desk in service_desks
            if str(desk.get("projectKey", "")).casefold() == args.project_key.casefold()
        ]
    if not selected:
        selector = (
            "all service desks"
            if args.all_service_desks
            else f"service desk {args.service_desk_id}"
            if args.service_desk_id
            else f"project {args.project_key}"
        )
        raise ValueError(f"no service desk found for {selector}")
    return selected


def list_customers(
    config: dict[str, str], service_desk_id: str
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"start": 0, "limit": 50})
    url = (
        f"{config['site']}/rest/servicedeskapi/servicedesk/"
        f"{urllib.parse.quote(service_desk_id, safe='')}/customer?{query}"
    )
    headers = jira_headers(config["jira_email"], config["jira_pat"])
    headers["X-ExperimentalApi"] = "opt-in"
    return paginated_values(url, api="jira", headers=headers)


def jira_user_profile(
    config: dict[str, str], account_id: str
) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"accountId": account_id, "expand": "groups,applicationRoles"}
    )
    return request_json(
        f"{config['site']}/rest/api/3/user?{query}",
        api="jira",
        headers=jira_headers(config["jira_email"], config["jira_pat"]),
    )


def is_portal_only(
    org_user: dict[str, Any], profile: dict[str, Any]
) -> tuple[bool, str]:
    if (
        org_user.get("account_type") != "atlassian"
        or profile.get("accountType") != "atlassian"
    ):
        return False, "not a person Atlassian account"
    if org_user.get("access_billable") is not False:
        return False, "organization directory does not confirm an unlicensed account"
    products = org_user.get("product_access")
    if not isinstance(products, list):
        return False, "organization directory product access is unavailable"
    if any(
        isinstance(product, dict)
        and str(product.get("key", "")).casefold().startswith("jira")
        for product in products
    ):
        return False, "has Jira product access"

    application_roles = profile.get("applicationRoles")
    if not isinstance(application_roles, dict) or not isinstance(
        application_roles.get("items"), list
    ):
        return False, "Jira application roles could not be verified"
    if application_roles.get("size") != len(application_roles["items"]):
        return False, "Jira application roles could not be verified completely"
    if application_roles["items"]:
        return False, "has Jira application roles"

    groups = profile.get("groups")
    if not isinstance(groups, dict) or not isinstance(groups.get("items"), list):
        return False, "Jira groups could not be verified"
    if groups.get("size") != len(groups["items"]) or not all(
        isinstance(group, dict) and isinstance(group.get("name"), str)
        for group in groups["items"]
    ):
        return False, "Jira groups could not be verified completely"
    if any("admin" in group["name"].casefold() for group in groups["items"]):
        return False, "belongs to an administrator group"
    return True, ""


def has_issues(config: dict[str, str], account_id: str) -> bool:
    escaped_id = account_id.replace("\\", "\\\\").replace('"', '\\"')
    jql = f'reporter = "{escaped_id}" OR creator = "{escaped_id}"'
    query = urllib.parse.urlencode(
        {"jql": jql, "maxResults": 1, "fields": "key"}
    )
    result = request_json(
        f"{config['site']}/rest/api/3/search/jql?{query}",
        api="jira",
        headers=jira_headers(config["jira_email"], config["jira_pat"]),
    )
    issues = result.get("issues")
    if not isinstance(issues, list):
        raise ApiError("jira", "issue search returned an invalid response")
    return bool(issues)


def customer_url(config: dict[str, str], service_desk_id: str) -> str:
    return (
        f"{config['site']}/rest/servicedeskapi/servicedesk/"
        f"{urllib.parse.quote(service_desk_id, safe='')}/customer"
    )


def write_report(rows: list[dict[str, str]]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(f"customer-revocation-report-{timestamp}")
    for suffix in range(1000):
        path = base.with_name(f"{base.name}{f'-{suffix}' if suffix else ''}.csv")
        try:
            with path.open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=REPORT_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            return path
        except FileExistsError:
            continue
    raise OSError("could not create a unique CSV report filename")


def print_row(row: dict[str, str]) -> None:
    print(
        f"{row['email']} | {row['display_name']} | {row['account_id']} | "
        f"{row['service_desk']} | {row['issues_count']} | "
        f"{row['action']}{': ' + row['reason'] if row['reason'] else ''}"
    )


def process(args: argparse.Namespace, config: dict[str, str]) -> int:
    input_mode = args.input_file is not None
    input_ids = read_identifiers(args.input_file)
    excluded_ids = read_identifiers(args.exclude_file)
    org_users = load_org_users(config)
    desks = select_service_desks(args, load_service_desks(config))
    rows: list[dict[str, str]] = []
    requested_matches: set[str] = set()
    candidates: list[tuple[dict[str, str], str, str]] = []
    error_count = 0

    for desk in desks:
        desk_id = str(desk.get("id", ""))
        desk_name = str(desk.get("projectName") or desk.get("projectKey") or desk_id)
        if not desk_id:
            raise ApiError("jira", "service desk response did not contain an ID")
        customers = list_customers(config, desk_id)
        for customer in customers:
            account_id = str(customer.get("accountId") or "")
            email = str(customer.get("emailAddress") or "")
            name = str(customer.get("displayName") or "")
            match_keys = {key.casefold() for key in (account_id, email) if key}
            row = {
                "email": email,
                "display_name": name,
                "account_id": account_id,
                "service_desk": desk_name,
                "issues_count": "",
                "action": "skipped",
                "reason": "",
            }
            if not account_id:
                row["reason"] = "customer record has no account ID"
                error_count += 1
                rows.append(row)
                continue
            matched_inputs = match_keys & input_ids
            requested_matches.update(matched_inputs)

            if match_keys & excluded_ids:
                row["issues_count"] = "not checked"
                row["reason"] = "present in exclude list"
                rows.append(row)
                continue
            if input_mode and not matched_inputs:
                row["issues_count"] = "not checked"
                row["reason"] = "not present in input list"
                rows.append(row)
                continue

            org_user = org_users.get(account_id.casefold())
            if not org_user:
                row["issues_count"] = "not checked"
                row["reason"] = "not found in organization directory"
                rows.append(row)
                continue
            try:
                profile = jira_user_profile(config, account_id)
            except ApiError as error:
                if error.status in {401, 403}:
                    raise
                row["issues_count"] = "not checked"
                row["reason"] = f"Jira profile check failed ({error})"
                error_count += 1
                rows.append(row)
                continue
            portal_only, reason = is_portal_only(org_user, profile)
            if not portal_only:
                row["issues_count"] = "not checked"
                row["reason"] = reason
                rows.append(row)
                continue

            if input_mode:
                row["issues_count"] = "not checked (input list)"
                candidates.append((row, desk_id, account_id))
            else:
                if has_issues(config, account_id):
                    row["issues_count"] = "1+"
                    row["reason"] = "has reported or created Jira issues"
                    rows.append(row)
                else:
                    row["issues_count"] = "0"
                    candidates.append((row, desk_id, account_id))

    if input_ids:
        missing_inputs = input_ids - requested_matches
        for identifier in sorted(missing_inputs):
            print(f"ERROR: input target was not found in selected service desks: {identifier}", file=sys.stderr)
            error_count += 1

    candidates_by_desk: dict[str, list[tuple[dict[str, str], str, str]]] = {}
    for candidate in candidates:
        candidates_by_desk.setdefault(candidate[1], []).append(candidate)
    for desk_id, desk_candidates in candidates_by_desk.items():
        for index in range(0, len(desk_candidates), REMOVE_BATCH_SIZE):
            batch = desk_candidates[index : index + REMOVE_BATCH_SIZE]
            for row, _, _ in batch:
                row["action"] = "would remove" if args.dry_run else "pending"
                row["reason"] = ""
            if not args.dry_run:
                account_ids = [account_id for _, _, account_id in batch]
                try:
                    request_json(
                        customer_url(config, desk_id),
                        api="jira",
                        headers={
                            **jira_headers(config["jira_email"], config["jira_pat"]),
                            "X-ExperimentalApi": "opt-in",
                        },
                        method="DELETE",
                        body={"accountIds": account_ids},
                    )
                except ApiError as error:
                    error_count += len(batch)
                    reason = f"removal failed ({error})"
                    for row, _, _ in batch:
                        row["action"] = "error"
                        row["reason"] = reason
                else:
                    for row, _, _ in batch:
                        row["action"] = "removed"
            rows.extend(row for row, _, _ in batch)

    for row in rows:
        print_row(row)
    report_path = write_report(rows)
    candidates_count = sum(row["action"] in {"would remove", "removed", "error"} for row in rows)
    removed_count = sum(row["action"] == "removed" for row in rows)
    skipped_count = sum(row["action"] == "skipped" for row in rows)
    print(
        f"Summary: customers scanned={len(rows)}, stale candidates={candidates_count}, "
        f"removed={removed_count}, skipped={skipped_count}, errors={error_count}"
    )
    print(f"CSV report: {report_path}")
    return 1 if error_count else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find inactive JSM portal customers; removals are live unless --dry-run is set."
    )
    parser.add_argument("--token", "--atlassian-token", dest="token")
    parser.add_argument("--org", "-o")
    parser.add_argument("--site")
    parser.add_argument("--jira-email")
    parser.add_argument("--jira-pat")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--service-desk-id", "-s")
    target.add_argument("--project-key", "-p")
    target.add_argument("--all-service-desks", action="store_true")
    parser.add_argument(
        "--input-file",
        help="CSV or text list of account IDs/emails to target instead of checking issue activity",
    )
    parser.add_argument(
        "--exclude-file",
        "--filename",
        "-f",
        dest="exclude_file",
        help="CSV allow list of account IDs/emails to skip",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report customers without removing service-desk access",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = resolve_config(args)
        return process(args, config)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except ApiError as error:
        if error.status in {401, 403}:
            if error.api == "organization":
                message = (
                    "Organization API authentication/permission failed "
                    f"(HTTP {error.status}); provide a valid organization API key "
                    "and organization-admin access."
                )
            else:
                message = (
                    f"Jira authentication/permission failed (HTTP {error.status}); "
                    "check the Jira email, PAT, and required project permissions."
                )
        else:
            message = f"{error.api.capitalize()} API error: {error}"
        print(f"ERROR: {message}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
