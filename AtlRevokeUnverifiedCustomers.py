#!/usr/bin/env python3
"""AtlRevokeUnverifiedCustomers.py

Remove invited JSM portal customers from selected service desks. Live removal
is the default; use --dry-run to preview changes.

Environment variables:
- ATLASSIAN_TOKEN / ATLASSIAN_ORG: Organization directory access
- ATLASSIAN_SITE / JIRA_EMAIL / JIRA_PAT: Jira site API access
"""

import argparse
import base64
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ORG_API_BASE = "https://api.atlassian.com/admin/v1"
PAGE_SIZE = 100
REMOVE_BATCH_SIZE = 50
MAX_READ_RETRIES = 3
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
REPORT_COLUMNS = (
    "email",
    "display_name",
    "account_id",
    "account_type",
    "service_desk",
    "status",
    "action",
    "reason",
)
ANSI_RESET = "\033[0m"
ANSI_RED = "\033[31m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"


class ApiError(Exception):
    def __init__(self, api: str, message: str, status: int | None = None):
        super().__init__(message)
        self.api = api
        self.status = status


def _print_status(label: str, message: str, color: str, *, stream: Any) -> None:
    if "NO_COLOR" not in os.environ and stream.isatty():
        label = f"{color}{label}{ANSI_RESET}"
    print(f"{label}: {message}", file=stream)


def warn(message: str) -> None:
    _print_status("WARN", message, ANSI_YELLOW, stream=sys.stderr)


def success(message: str) -> None:
    _print_status("SUCCESS", message, ANSI_GREEN, stream=sys.stdout)


def log_error(message: str) -> None:
    _print_status("ERROR", message, ANSI_RED, stream=sys.stderr)


def _retry_delay(attempt: int, response: urllib.error.HTTPError | None = None) -> float:
    if response is not None and response.headers is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds):
                    return min(max(seconds, 0), 30)
            except ValueError:
                pass
    return min(0.5 * (2**attempt), 5)


def request_json(
    url: str,
    *,
    api: str,
    headers: dict[str, str],
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any]:
    request_headers = {"Accept": "application/json", **headers}
    payload = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers=request_headers, method=method
    )
    retryable = method.upper() == "GET"
    for attempt in range(MAX_READ_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_body = response.read()
            break
        except urllib.error.HTTPError as http_error:
            if (
                retryable
                and http_error.code in RETRYABLE_STATUSES
                and attempt < MAX_READ_RETRIES
            ):
                time.sleep(_retry_delay(attempt, http_error))
                continue
            raise ApiError(api, f"HTTP {http_error.code}", http_error.code) from None
        except (urllib.error.URLError, TimeoutError) as request_error:
            if retryable and attempt < MAX_READ_RETRIES:
                time.sleep(_retry_delay(attempt))
                continue
            reason = (
                request_error.reason
                if isinstance(request_error, urllib.error.URLError)
                else request_error
            )
            raise ApiError(api, f"request failed: {reason}") from None

    if not response_body:
        return {}
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise ApiError(api, f"server returned invalid JSON: {error}") from None
    if not isinstance(result, (dict, list)):
        raise ApiError(api, "server returned an unexpected response")
    return result


def _find_list_of_dicts(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            return value
        return None
    if isinstance(value, dict):
        for key in (
            "values",
            "data",
            "items",
            "results",
            "contactResults",
            "contacts",
        ):
            candidate = value.get(key)
            found = _find_list_of_dicts(candidate)
            if found is not None:
                return found
        for nested in value.values():
            found = _find_list_of_dicts(nested)
            if found is not None:
                return found
    return None


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

        if isinstance(page, list):
            page_values = page
        else:
            page_values = _find_list_of_dicts(page)

        if not isinstance(page_values, list):
            keys = sorted(page.keys()) if isinstance(page, dict) else type(page).__name__
            raise ApiError(
                api,
                "response did not contain a customer/user list; "
                f"received {type(page).__name__} with keys={keys}",
            )

        if not isinstance(page_values, list):
            keys = sorted(page.keys()) if isinstance(page, dict) else type(page).__name__
            raise ApiError(
                api,
                "response did not contain a customer/user list; "
                f"received {type(page).__name__} with keys={keys}",
            )
        if not all(isinstance(item, dict) for item in page_values):
            raise ApiError(api, "response contained an invalid list item")
        values.extend(page_values)

        links = page.get("links") if isinstance(page, dict) else None
        if isinstance(links, dict):
            next_url = links.get("next")
            if not next_url:
                break
            next_url = urllib.parse.urljoin(current_url, next_url)
            parsed_next_url = urllib.parse.urlparse(next_url)
            if next_link_host and (
                parsed_next_url.scheme != "https"
                or parsed_next_url.netloc != next_link_host
            ):
                raise ApiError(api, "pagination link points to an unexpected host")
            current_url = next_url
            continue

        if isinstance(page, dict) and page.get("isLastPage") is True:
            break
        if not page_values:
            break
        start = page.get("start") if isinstance(page, dict) else None
        limit = page.get("limit") if isinstance(page, dict) else None
        if isinstance(start, int) and isinstance(limit, int) and limit > 0:
            if len(page_values) < limit:
                break
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
        if isinstance(page, dict) and page.get("next"):
            next_url = urllib.parse.urljoin(current_url, str(page["next"]))
            current_url = next_url
            continue
        if isinstance(page, dict) and page.get("nextPageToken"):
            next_url = urllib.parse.urljoin(current_url, str(page["nextPageToken"]))
            current_url = next_url
            continue
        break
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
        option_names = {
            "token": "--token",
            "org": "-o/--org",
            "site": "-s/--site",
            "jira_email": "--jira-email",
            "jira_pat": "--jira-pat",
        }
        details = ", ".join(
            f"{option_names[key]} / {sources[key][1]}" for key in missing
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


def resolve_service_desk_for_project(
    config: dict[str, str], project_key: str
) -> list[dict[str, Any]]:
    project = request_json(
        f"{config['site']}/rest/api/3/project/{urllib.parse.quote(project_key, safe='')}",
        api="jira",
        headers=jira_headers(config["jira_email"], config["jira_pat"]),
    )
    project_id = project.get("id")
    desk_url = (
        f"{config['site']}/rest/servicedeskapi/project/"
        f"{urllib.parse.quote(project_key, safe='')}/serviceDesk"
    )
    try:
        desk = request_json(
            desk_url,
            api="jira",
            headers=jira_headers(config["jira_email"], config["jira_pat"]),
        )
    except ApiError:
        desk = None

    normalized: list[dict[str, Any]] = []
    if isinstance(desk, dict):
        normalized.append(
            {
                **desk,
                "projectKey": project_key,
                "projectId": desk.get("projectId") or project_id,
                "projectName": desk.get("projectName") or project.get("name") or project_key,
            }
        )
    elif isinstance(desk, list):
        for item in desk:
            if isinstance(item, dict):
                normalized.append(
                    {
                        **item,
                        "projectKey": project_key,
                        "projectId": item.get("projectId") or project_id,
                        "projectName": item.get("projectName") or item.get("name") or project_key,
                    }
                )

    if normalized:
        return normalized

    service_desks = load_service_desks(config)
    selected = [
        desk
        for desk in service_desks
        if str(desk.get("projectKey", "")).casefold() == project_key.casefold()
    ]
    if not selected:
        raise ValueError(f"no service desk found for project {project_key}")
    return selected


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


def resolve_service_desk_project_id(
    config: dict[str, str], service_desk_id: str, service_desk: dict[str, Any] | None = None
) -> str:
    if service_desk is not None:
        project_id = service_desk.get("projectId")
        if project_id not in (None, ""):
            return str(project_id)
        project = service_desk.get("project")
        if isinstance(project, dict):
            project_id = project.get("id") or project.get("projectId")
            if project_id not in (None, ""):
                return str(project_id)

    url = (
        f"{config['site']}/rest/servicedeskapi/servicedesk/"
        f"{urllib.parse.quote(service_desk_id, safe='')}"
    )
    service_desk = request_json(
        url,
        api="jira",
        headers=jira_headers(config["jira_email"], config["jira_pat"]),
    )
    project_id = service_desk.get("projectId")
    if project_id not in (None, ""):
        return str(project_id)
    project = service_desk.get("project")
    if isinstance(project, dict):
        project_id = project.get("id") or project.get("projectId")
        if project_id not in (None, ""):
            return str(project_id)

    project_key = service_desk.get("projectKey")
    if isinstance(project_key, str) and project_key:
        project = request_json(
            f"{config['site']}/rest/api/3/project/{urllib.parse.quote(project_key, safe='')}",
            api="jira",
            headers=jira_headers(config["jira_email"], config["jira_pat"]),
        )
        project_id = project.get("id")
        if project_id not in (None, ""):
            return str(project_id)

    raise ApiError("jira", "service desk does not expose a project ID for contact discovery")


def list_customers(
    config: dict[str, str], service_desk_id: str, project_id: str | None = None
) -> list[dict[str, Any]]:
    resolved_project_id = project_id or resolve_service_desk_project_id(
        config, service_desk_id
    )
    headers = jira_headers(config["jira_email"], config["jira_pat"])
    headers["X-ExperimentalApi"] = "opt-in"

    start = 0
    limit = 100
    contacts: list[dict[str, Any]] = []
    while True:
        query = urllib.parse.urlencode({"start": start, "limit": limit})
        url = (
            f"{config['site']}/rest/servicedesk/1/project/"
            f"{urllib.parse.quote(str(resolved_project_id), safe='')}/contacts?{query}"
        )
        page = request_json(url, api="jira", headers=headers)

        if isinstance(page, list):
            page_values = page
        elif isinstance(page, dict):
            page_values = None
            for key in ("contactResults", "values", "data", "items", "results", "contacts"):
                candidate = page.get(key)
                if isinstance(candidate, list):
                    page_values = candidate
                    break
                if isinstance(candidate, dict):
                    for nested_key in ("values", "items", "results", "contacts"):
                        nested = candidate.get(nested_key)
                        if isinstance(nested, list):
                            page_values = nested
                            break
                    if isinstance(page_values, list):
                        break
            if page_values is None and isinstance(page.get("contactPageMetadata"), dict):
                page_values = []
        else:
            page_values = None

        if not isinstance(page_values, list):
            keys = sorted(page.keys()) if isinstance(page, dict) else type(page).__name__
            raise ApiError(
                api="jira",
                message=(
                    "response did not contain the expected contact list; "
                    f"received {type(page).__name__} with keys={keys}"
                ),
            )

        if not page_values:
            break

        contacts.extend(page_values)
        if len(page_values) < limit:
            break
        start += len(page_values)

    return contacts


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
    account_type: str,
    org_user: dict[str, Any] | None,
    profile: dict[str, Any] | None,
) -> tuple[bool, str]:
    if account_type == "customer":
        return True, ""
    if account_type != "atlassian":
        return False, "not a person Atlassian account"
    if org_user is None:
        return False, "not found in organization directory"
    if profile is None or org_user.get("account_type") != "atlassian":
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


def normalize_customer_status(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().casefold()
    return ""


def customer_status(customer: dict[str, Any]) -> str:
    for key in (
        "status",
        "accountStatus",
        "invitationStatus",
        "customerStatus",
        "portalStatus",
        "contactStatus",
        "contact_status",
        "verificationStatus",
        "loggedInStatus",
        "logged_in_status",
        "loginStatus",
        "login_status",
    ):
        value = normalize_customer_status(customer.get(key))
        if value:
            return value

    active = customer.get("active")
    if isinstance(active, bool):
        return "active" if active else "pending_login"
    return "unknown"


def is_invited_customer(customer: dict[str, Any]) -> bool:
    status = customer_status(customer)
    if isinstance(customer.get("agent"), bool) and customer.get("agent") is True:
        return False
    invited_statuses = {
        "invited",
        "pending",
        "pending_login",
        "pendinglogin",
        "pending-login",
        "pending_invite",
        "pendinginvite",
        "pending_invitation",
        "unverified",
        "unverified_email",
        "not_verified",
        "notverified",
    }
    active_statuses = {
        "active",
        "verified",
        "logged_in",
        "loggedin",
        "accepted",
        "member",
        "enabled",
        "logged-in",
        "verified_email",
    }
    if status in invited_statuses:
        return True
    if status in active_statuses:
        return False
    active = customer.get("active")
    return isinstance(active, bool) and not active


def customer_url(config: dict[str, str], service_desk_id: str) -> str:
    return (
        f"{config['site']}/rest/servicedeskapi/servicedesk/"
        f"{urllib.parse.quote(service_desk_id, safe='')}/customer"
    )


def write_report(rows: list[dict[str, str]], output_path: str | None = None) -> Path:
    if output_path:
        path = Path(output_path)
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

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
    action = row["action"]
    colors = {"removed": ANSI_GREEN, "would remove": ANSI_YELLOW, "error": ANSI_RED}
    if action in colors and "NO_COLOR" not in os.environ and sys.stdout.isatty():
        action = f"{colors[action]}{action}{ANSI_RESET}"
    print(
        f"{row['email']} | {row['display_name']} | {row['account_id']} | "
        f"{row['account_type']} | {row['service_desk']} | {row['status']} | "
        f"{action}{': ' + row['reason'] if row['reason'] else ''}"
    )


def process(args: argparse.Namespace, config: dict[str, str]) -> int:
    if args.out:
        report_path = Path(args.out)
        if report_path.exists():
            raise ValueError(
                f"report file already exists and will not be overwritten: {args.out}"
            )
        if not report_path.parent.is_dir():
            raise ValueError(f"report directory does not exist: {report_path.parent}")
    if args.dry_run:
        warn("Dry-run mode: no service-desk access will be changed.")
    input_mode = args.input_file is not None
    input_ids = read_identifiers(args.input_file)
    excluded_ids = read_identifiers(args.exclude_file)
    if args.all_service_desks:
        desks = load_service_desks(config)
    elif args.service_desk_id:
        desk_url = (
            f"{config['site']}/rest/servicedeskapi/servicedesk/"
            f"{urllib.parse.quote(args.service_desk_id, safe='')}"
        )
        desks = [
            request_json(
                desk_url,
                api="jira",
                headers=jira_headers(config["jira_email"], config["jira_pat"]),
            )
        ]
    else:
        desks = resolve_service_desk_for_project(config, args.project_key)
    rows: list[dict[str, str]] = []
    requested_matches: set[str] = set()
    candidates: list[tuple[dict[str, str], str, str]] = []
    error_count = 0

    for desk in desks:
        desk_id = str(desk.get("id", ""))
        desk_name = str(desk.get("projectName") or desk.get("projectKey") or desk_id)
        if not desk_id:
            raise ApiError("jira", "service desk response did not contain an ID")
        customers = list_customers(
            config,
            desk_id,
            project_id=(
                str(desk.get("projectId"))
                if desk.get("projectId") not in (None, "")
                else None
            ),
        )
        for customer in customers:
            account_id = str(customer.get("identifier") or customer.get("accountId") or "")
            email = str(customer.get("email") or customer.get("emailAddress") or "")
            name = str(customer.get("displayName") or "")
            agent = customer.get("agent")
            match_keys = {key.casefold() for key in (account_id, email) if key}
            status = customer_status(customer)
            row = {
                "email": email,
                "display_name": name,
                "account_id": account_id,
                "account_type": "customer" if not agent else "agent",
                "service_desk": desk_name,
                "status": status,
                "action": "skipped",
                "reason": "",
            }
            if not account_id:
                row["action"] = "error"
                row["reason"] = "customer record has no account ID"
                error_count += 1
                rows.append(row)
                continue
            matched_inputs = match_keys & input_ids
            requested_matches.update(matched_inputs)

            if match_keys & excluded_ids:
                row["status"] = status or "not checked"
                row["reason"] = "present in exclude list"
                rows.append(row)
                continue
            if input_mode and not matched_inputs:
                row["status"] = status or "not checked"
                row["reason"] = "not present in input list"
                rows.append(row)
                continue

            if isinstance(agent, bool) and agent:
                row["status"] = status or "agent"
                row["reason"] = "contact is an agent and not a customer"
                rows.append(row)
                continue

            if input_mode:
                row["status"] = status or "not checked (input list)"
                candidates.append((row, desk_id, account_id))
            elif status == "pending_login" and is_invited_customer(customer):
                row["status"] = status or "pending_login"
                candidates.append((row, desk_id, account_id))
            else:
                row["status"] = status or "logged_in"
                row["reason"] = "contact is active or not in pending-login status"
                rows.append(row)

    if input_ids:
        missing_inputs = input_ids - requested_matches
        for identifier in sorted(missing_inputs):
            log_error(f"Input target was not found in selected service desks: {identifier}")
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
                    log_error(
                        f"{reason} for {len(batch)} customers in service desk {desk_id}"
                    )
                else:
                    for row, _, _ in batch:
                        row["action"] = "removed"
                    success(f"Removed {len(batch)} customers from service desk {desk_id}")
            rows.extend(row for row, _, _ in batch)

    for row in rows:
        print_row(row)
    report_path = write_report(rows, args.out)
    candidates_count = sum(
        row["action"] in {"would remove", "removed"}
        or (
            row["action"] == "error"
            and row["status"] in {"invited", "not checked (input list)"}
        )
        for row in rows
    )
    removed_count = sum(row["action"] == "removed" for row in rows)
    would_remove_count = sum(row["action"] == "would remove" for row in rows)
    skipped_count = sum(row["action"] == "skipped" for row in rows)
    print("Summary:")
    print(f"  Service desks processed: {len(desks)}")
    print(f"  Customers scanned: {len(rows)}")
    print(f"  Stale candidates: {candidates_count}")
    print(f"  Would remove: {would_remove_count}")
    print(f"  Removed: {removed_count}")
    print(f"  Skipped: {skipped_count}")
    print(f"  Errors: {error_count}")
    success(f"CSV report written to {report_path}")
    return 1 if error_count else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove invited JSM portal customers from service desks."
    )
    parser.add_argument(
        "--token",
        "--atlassian-token",
        dest="token",
        help="Organization API key (env: ATLASSIAN_TOKEN)",
    )
    parser.add_argument("-o", "--org", help="Organization ID (env: ATLASSIAN_ORG)")
    parser.add_argument(
        "-s",
        "--site",
        help="Atlassian site hostname or URL (env: ATLASSIAN_SITE)",
    )
    parser.add_argument(
        "--jira-email", help="Jira service account email (env: JIRA_EMAIL)"
    )
    parser.add_argument("--jira-pat", help="Jira API token (env: JIRA_PAT)")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "-d",
        "--service-desk-id",
        help="Process one service desk by ID",
    )
    target.add_argument(
        "-p",
        "--project-key",
        help="Resolve a project key to its service desk",
    )
    target.add_argument(
        "--all-service-desks",
        action="store_true",
        help="Process every available service desk",
    )
    parser.add_argument(
        "--input-file",
        metavar="PATH",
        help="CSV or text list of account IDs/emails to target explicitly",
    )
    parser.add_argument(
        "-f",
        "--exclude-file",
        "--filename",
        dest="exclude_file",
        metavar="PATH",
        help="CSV allow list of account IDs/emails to skip",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report customers without removing service-desk access",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="CSV report path (default: unique timestamped filename)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = resolve_config(args)
        return process(args, config)
    except ValueError as error:
        log_error(str(error))
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
        log_error(message)
        return 2
    except OSError as error:
        log_error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
