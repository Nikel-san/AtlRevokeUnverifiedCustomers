#!/usr/bin/env python3
"""AtlRevokeUnverifiedCustomers.py

Remove JSM portal customers with PENDING_LOGIN status from selected service
desks. Live removal is the default; use --dry-run to preview changes.

Environment variables:
- ATLASSIAN_TOKEN / ATLASSIAN_ORG: Organization directory access
- ATLASSIAN_SITE / JIRA_EMAIL / JIRA_PAT: Jira site API access
"""

import argparse
import base64
import concurrent.futures
import csv
import http.client
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ORG_API_BASE = "https://api.atlassian.com/admin/v1"
PAGE_SIZE = 100
REMOVE_BATCH_SIZE = 50
CONTACT_PAGE_SIZE = 50
PROJECT_CONTACT_PAGE_SIZE = 100
CONTACT_PAGE_WORKERS = 8
MAX_CONCURRENT_WORKERS = 32
REQUEST_TIMEOUT = 30
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
_connection_local = threading.local()
_connections: set[http.client.HTTPConnection] = set()
_connections_lock = threading.Lock()
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


def _retry_delay(attempt: int, headers: Any = None) -> float:
    if headers is not None:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds):
                    return min(max(seconds, 0), 30)
            except ValueError:
                pass
    base_delay = min(0.5 * (2**attempt), 5)
    jitter = random.uniform(0.25, 0.75)
    return base_delay + jitter


def _get_connection(
    scheme: str, host: str, port: int | None
) -> http.client.HTTPConnection:
    connections = getattr(_connection_local, "connections", None)
    if connections is None:
        connections = {}
        _connection_local.connections = connections
    key = (scheme, host, port)
    connection = connections.get(key)
    if connection is None:
        connection_type = http.client.HTTPConnection
        if scheme == "https":
            connection_type = http.client.HTTPSConnection
        connection = connection_type(host, port=port, timeout=REQUEST_TIMEOUT)
        connections[key] = connection
        with _connections_lock:
            _connections.add(connection)
    return connection


def _discard_connection(
    scheme: str, host: str, port: int | None, connection: http.client.HTTPConnection
) -> None:
    connections = getattr(_connection_local, "connections", {})
    key = (scheme, host, port)
    if connections.get(key) is connection:
        del connections[key]
    with _connections_lock:
        _connections.discard(connection)
    connection.close()


def _close_connections() -> None:
    with _connections_lock:
        connections = tuple(_connections)
        _connections.clear()
    for connection in connections:
        connection.close()
    getattr(_connection_local, "connections", {}).clear()


def request_json(
    url: str,
    *,
    api: str,
    headers: dict[str, str],
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any]:
    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ApiError(api, "request URL must be an absolute HTTP(S) URL")
    host = parsed_url.hostname
    port = parsed_url.port
    request_path = urllib.parse.urlunsplit(
        ("", "", parsed_url.path or "/", parsed_url.query, "")
    )
    request_headers = {"Accept": "application/json", **headers}
    payload: bytes | None = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    retryable = method.upper() == "GET"
    for attempt in range(MAX_READ_RETRIES + 1):
        connection = _get_connection(parsed_url.scheme, host, port)
        try:
            connection.request(
                method.upper(),
                request_path,
                body=payload,
                headers=request_headers,
            )
            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                if (
                    retryable
                    and response.status in RETRYABLE_STATUSES
                    and attempt < MAX_READ_RETRIES
                ):
                    time.sleep(_retry_delay(attempt, response.headers))
                    continue
                raise ApiError(api, f"HTTP {response.status}", response.status)
            break
        except ApiError:
            raise
        except (http.client.HTTPException, OSError) as request_error:
            _discard_connection(parsed_url.scheme, host, port, connection)
            if retryable and attempt < MAX_READ_RETRIES:
                time.sleep(_retry_delay(attempt))
                continue
            raise ApiError(api, f"request failed: {request_error}") from None

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
    missing = [
        key
        for key in ("site", "jira_email", "jira_pat")
        if not config[key].strip()
    ]
    if missing:
        option_names = {
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
    except ApiError as error:
        if error.status not in {404, 405}:
            raise
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


def _extract_contact_page_values(page: Any) -> list[Any] | None:
    if isinstance(page, list):
        return page if all(isinstance(item, dict) for item in page) else None
    if not isinstance(page, dict):
        return None

    for key in ("contactResults", "values", "data", "items", "results", "contacts"):
        candidate = page.get(key)
        if isinstance(candidate, list):
            if all(isinstance(item, dict) for item in candidate):
                return candidate
            continue
        if isinstance(candidate, dict):
            nested = _extract_contact_page_values(candidate)
            if nested is not None:
                return nested

    for value in page.values():
        if isinstance(value, list):
            if all(isinstance(item, dict) for item in value):
                return value
        elif isinstance(value, dict):
            nested = _extract_contact_page_values(value)
            if nested is not None:
                return nested
    return None


def _parse_contact_page(
    page: dict[str, Any] | list[Any],
) -> tuple[list[Any], dict[str, Any]]:
    if isinstance(page, list):
        page_values = page
        metadata: dict[str, Any] = {}
    else:
        metadata = dict(page)
        if isinstance(page.get("contactPageMetadata"), dict):
            metadata.update(page["contactPageMetadata"])
        page_values = _extract_contact_page_values(page)

    if not isinstance(page_values, list):
        keys = sorted(page.keys()) if isinstance(page, dict) else type(page).__name__
        raise ApiError(
            api="jira",
            message=(
                "response did not contain the expected contact list; "
                f"received {type(page).__name__} with keys={keys}"
            ),
        )
    return page_values, metadata


def _normalized_contact_key(value: Any) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _is_complete_contact(contact: dict[str, Any] | None) -> bool:
    if not isinstance(contact, dict) or not isinstance(contact.get("agent"), bool):
        return False
    status = customer_status(contact)
    return status in {"pending_login", "logged_in"}


def _contact_identity(contact: dict[str, Any]) -> tuple[str, ...]:
    return (
        _normalized_contact_key(contact.get("identifier")),
        _normalized_contact_key(contact.get("accountId")),
        _normalized_contact_key(contact.get("email")),
        _normalized_contact_key(contact.get("emailAddress")),
        _normalized_contact_key(contact.get("displayName")),
    )


def _index_project_contacts(
    contacts: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any] | None], ...]:
    indexes: tuple[dict[str, dict[str, Any] | None], ...] = ({}, {}, {})
    for contact in contacts:
        keys_by_type = (
            (
                _normalized_contact_key(contact.get("identifier")),
                _normalized_contact_key(contact.get("accountId")),
            ),
            (
                _normalized_contact_key(contact.get("email")),
                _normalized_contact_key(contact.get("emailAddress")),
            ),
            (_normalized_contact_key(contact.get("displayName")),),
        )
        for index, keys in zip(indexes, keys_by_type):
            for key in keys:
                if not key:
                    continue
                existing = index.get(key)
                if key not in index:
                    index[key] = contact
                elif existing != contact:
                    index[key] = None
    return indexes


def _find_contact(
    values: list[Any],
    account_id: str,
    email: str,
    display_name: str,
    *,
    accept_single_email_result: bool = False,
    match_display_name: bool = False,
) -> dict[str, Any] | None:
    contacts = [item for item in values if isinstance(item, dict)]
    if accept_single_email_result and len(contacts) == 1:
        return contacts[0]

    account_key = _normalized_contact_key(account_id)
    email_key = _normalized_contact_key(email)
    name_key = _normalized_contact_key(display_name)
    matches = [
        item
        for item in contacts
        if (
            account_key
            and account_key
            in {
                _normalized_contact_key(item.get("identifier")),
                _normalized_contact_key(item.get("accountId")),
            }
        )
        or (
            email_key
            and email_key
            in {
                _normalized_contact_key(item.get("email")),
                _normalized_contact_key(item.get("emailAddress")),
            }
        )
        or (
            match_display_name
            and name_key
            and _normalized_contact_key(item.get("displayName")) == name_key
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _list_project_contacts(
    config: dict[str, str],
    project_id: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    project_root = (
        f"{config['site']}/rest/servicedesk/1/project/"
        f"{urllib.parse.quote(project_id, safe='')}"
    )
    search_url = f"{project_root}/contacts/search"
    legacy_url = f"{project_root}/contacts"
    all_contacts: list[dict[str, Any]] = []
    seen_pages: set[tuple[str, ...]] = set()
    page = 1

    def fetch_page(url: str, *, page_number: int) -> tuple[list[dict[str, Any]], dict[str, Any] | Any]:
        response = request_json(
            f"{url}?{urllib.parse.urlencode({'query': '', 'page': page_number})}",
            api="jira",
            headers=headers,
        )
        page_values, metadata = _parse_contact_page(response)
        contacts = [item for item in page_values if isinstance(item, dict)]
        return contacts, (response if isinstance(response, dict) else metadata)

    try:
        while True:
            page_values, response = fetch_page(search_url, page_number=page)
            contacts = page_values
            page_signature = tuple(
                sorted(
                    {
                        str(item.get("identifier") or item.get("accountId") or "")
                        for item in contacts
                    }
                )
            )
            if page_signature and page_signature in seen_pages:
                raise ApiError(
                    "jira",
                    f"project contact pagination repeated a page at page={page}",
                )
            if page_signature:
                seen_pages.add(page_signature)

            all_contacts.extend(contacts)
            if not isinstance(response, dict):
                break
            if response.get("isLastPage") is True:
                break
            if not contacts:
                total = response.get("total")
                if response.get("isLastPage") is False and page == 1 and (
                    total in (None, 0) or not isinstance(total, int)
                ):
                    break
                if response.get("isLastPage") is False and not isinstance(total, int):
                    raise ApiError(
                        "jira",
                        f"project contact page at page={page} was empty but did not report a valid total",
                    )
                if isinstance(total, int) and total <= 0:
                    break
                raise ApiError(
                    "jira",
                    f"project contact page at page={page} was empty but marked as not last",
                )
            total = response.get("total")
            if isinstance(total, int) and total >= 0 and len(all_contacts) >= total:
                break
            page += 1
    except ApiError as error:
        if error.status not in {404, 405}:
            raise
        response = request_json(legacy_url, api="jira", headers=headers)
        page_values, _ = _parse_contact_page(response)
        return [item for item in page_values if isinstance(item, dict)]

    return all_contacts


def list_customers(
    config: dict[str, str],
    service_desk_id: str,
    project_id: str | None = None,
    *,
    input_ids: set[str] | None = None,
    input_filter_enabled: bool = False,
    exclude_ids: set[str] | None = None,
    contact_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    project_contact_cache: dict[
        str, tuple[dict[str, dict[str, Any] | None], ...]
    ]
    | None = None,
    workers: int = CONTACT_PAGE_WORKERS,
) -> list[dict[str, Any]]:
    resolved_project_id = project_id or resolve_service_desk_project_id(
        config, service_desk_id
    )
    headers = jira_headers(config["jira_email"], config["jira_pat"])
    headers["X-ExperimentalApi"] = "opt-in"
    is_tty = sys.stderr.isatty()
    customers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    normalized_input_ids = {item.casefold() for item in input_ids or set()}
    normalized_exclude_ids = {item.casefold() for item in exclude_ids or set()}
    start = 0
    while True:
        page_query = urllib.parse.urlencode(
            {"start": start, "limit": CONTACT_PAGE_SIZE}
        )
        page_url = (
            f"{config['site']}/rest/servicedeskapi/servicedesk/"
            f"{urllib.parse.quote(service_desk_id, safe='')}/customer?{page_query}"
        )
        response = request_json(page_url, api="jira", headers=headers)
        if not isinstance(response, dict) or not isinstance(
            response.get("values"), list
        ):
            keys = (
                sorted(response)
                if isinstance(response, dict)
                else type(response).__name__
            )
            raise ApiError(
                "jira",
                "service-desk customer response did not contain a values list; "
                f"received {keys}",
            )

        page_values = response["values"]
        if not all(isinstance(item, dict) for item in page_values):
            raise ApiError(
                "jira", "service-desk customer response contained an invalid item"
            )

        is_last_page = response.get("isLastPage")
        page_signature = tuple(
            sorted(
                account_id
                for account_id in (
                    str(
                        item.get("accountId") or item.get("identifier") or ""
                    )
                    for item in page_values
                )
                if account_id
            )
        )
        page_can_repeat = is_last_page is False or (
            is_last_page is not True and len(page_values) >= CONTACT_PAGE_SIZE
        )
        if page_signature and page_can_repeat and page_signature in seen_pages:
            raise ApiError(
                "jira",
                f"service-desk customer pagination repeated a page at start={start}",
            )
        if page_signature:
            seen_pages.add(page_signature)

        for customer in page_values:
            account_id = str(
                customer.get("accountId") or customer.get("identifier") or ""
            )
            account_key = account_id.casefold()
            if account_key and account_key in seen_ids:
                continue
            if account_key:
                seen_ids.add(account_key)
            match_keys = {
                key.casefold()
                for key in (
                    account_id,
                    str(customer.get("email") or customer.get("emailAddress") or ""),
                )
                if key
            }
            filter_reason = (
                "excluded by file"
                if match_keys & normalized_exclude_ids
                else "not in input file"
                if input_filter_enabled
                and not (match_keys & normalized_input_ids)
                else ""
            )
            canonical_account_id = str(
                customer.get("accountId") or customer.get("identifier") or ""
            )
            customers.append(
                {
                    **customer,
                    "accountId": canonical_account_id,
                    "customerAccountId": canonical_account_id,
                    "_filter_reason": filter_reason,
                }
            )

        if is_tty:
            sys.stderr.write(f"\rScanning customers: {len(customers)} fetched...")
            sys.stderr.flush()
        elif len(customers) and len(customers) % 500 == 0:
            sys.stderr.write(f"Scanning customers: {len(customers)} fetched.\n")

        if is_last_page is True:
            break
        if not page_values:
            if is_last_page is False:
                raise ApiError(
                    "jira",
                    f"service-desk customer page at start={start} was empty "
                    "but marked as not last",
                )
            break
        if is_last_page is not False and len(page_values) < CONTACT_PAGE_SIZE:
            break
        start += len(page_values)

    if is_tty:
        sys.stderr.write("\n")
        sys.stderr.flush()

    contact_cache = contact_cache if contact_cache is not None else {}
    project_contact_cache = (
        project_contact_cache if project_contact_cache is not None else {}
    )
    customers_to_enrich = [
        customer for customer in customers if not customer.get("_filter_reason")
    ]
    if not customers_to_enrich:
        return customers

    pending_customers: list[dict[str, Any]] = []
    enriched_by_id: dict[str, dict[str, Any]] = {}
    for customer in customers_to_enrich:
        account_id = str(
            customer.get("accountId") or customer.get("identifier") or ""
        )
        if not account_id:
            enriched_by_id[""] = customer
            continue
        key = (str(resolved_project_id), account_id        )
        if key in contact_cache:
            cached_contact = contact_cache[key]
            if _is_complete_contact(cached_contact):
                enriched_by_id[account_id.casefold()] = _merge_customer_contact(
                    customer, cached_contact
                )
                continue
            contact_cache.pop(key)
        pending_customers.append(customer)

    contact_indexes = project_contact_cache.get(str(resolved_project_id))
    if pending_customers and contact_indexes is None:
        try:
            project_contacts = _list_project_contacts(
                config, str(resolved_project_id), headers
            )
        except ApiError as error:
            if error.status in {401, 403}:
                raise
            warn(
                f"Bulk contact discovery failed ({error}); "
                "falling back to contact searches."
            )
            project_contacts = []
        else:
            project_contact_cache[str(resolved_project_id)] = _index_project_contacts(
                project_contacts
            )
        contact_indexes = _index_project_contacts(project_contacts)
        project_contact_cache[str(resolved_project_id)] = contact_indexes
        if not project_contacts:
            warn(
                "Bulk contact discovery returned no contacts; "
                "falling back to contact searches."
            )

    def fetch_contact_details(customer: dict[str, Any]) -> dict[str, Any]:
        account_id = str(
            customer.get("accountId") or customer.get("identifier") or ""
        )
        if not account_id:
            return customer
        key = (str(resolved_project_id), account_id)
        email = str(customer.get("emailAddress") or customer.get("email") or "")
        display_name = str(customer.get("displayName") or "")
        is_customer_contact = is_portal_customer_id(account_id) or str(
            customer.get("accountType") or customer.get("account_type") or ""
        ).casefold() == "customer"
        account_key = _normalized_contact_key(account_id)
        email_key = _normalized_contact_key(email)
        name_key = _normalized_contact_key(display_name)
        contact = None
        if contact_indexes is not None:
            for index, lookup_key in zip(
                contact_indexes, (account_key, email_key, name_key)
            ):
                if lookup_key and lookup_key in index:
                    contact = index[lookup_key]
                    if contact is not None:
                        break
        if not _is_complete_contact(contact):
            contact = None

        def find_contact_by_query(
            query: str,
            *,
            is_email_query: bool,
            is_display_name_query: bool = False,
        ) -> dict[str, Any] | None:
            def request_search_page(page: int) -> Any:
                contact_url = (
                    f"{config['site']}/rest/servicedesk/1/project/"
                    f"{urllib.parse.quote(str(resolved_project_id), safe='')}/contacts/search"
                )
                params = urllib.parse.urlencode({"query": query, "page": page})
                try:
                    return request_json(
                        f"{contact_url}?{params}",
                        api="jira",
                        headers=headers,
                    )
                except ApiError as error:
                    if error.status not in {404, 405}:
                        raise
                    legacy_url = (
                        f"{config['site']}/rest/servicedesk/1/project/"
                        f"{urllib.parse.quote(str(resolved_project_id), safe='')}/contacts?"
                        f"{urllib.parse.urlencode({'query': query})}"
                    )
                    return request_json(legacy_url, api="jira", headers=headers)

            for page in range(10):
                try:
                    contact_response = request_search_page(page)
                    contact_values, _ = _parse_contact_page(contact_response)
                except ApiError as error:
                    if error.status in {401, 403}:
                        raise
                    warn(
                        f"Individual contact lookup failed ({error}); "
                        "the customer will remain unverifiable unless another lookup succeeds."
                    )
                    return None
                result = _find_contact(
                    contact_values,
                    account_id,
                    email if is_email_query else "",
                    display_name,
                    accept_single_email_result=is_email_query,
                    match_display_name=is_display_name_query,
                )
                if result is not None:
                    return result if _is_complete_contact(result) else None
                if isinstance(contact_response, dict):
                    is_last_page = contact_response.get("isLastPage")
                    if is_last_page is True:
                        break
                    if not contact_values:
                        break
                    if "isLastPage" not in contact_response and "page" not in contact_response and "total" not in contact_response:
                        break
                    total = contact_response.get("total")
                    if isinstance(total, int) and total >= 0 and page + 1 >= math.ceil(total / max(1, len(contact_values))):
                        break
                else:
                    break
                if not contact_values:
                    break
            return None

        if contact is None and not is_portal_customer_id(account_id):
            contact = find_contact_by_query(account_id, is_email_query=False)
        if contact is None and email:
            contact = find_contact_by_query(email, is_email_query=True)
        if (
            contact is None
            and display_name
            and _normalized_contact_key(display_name)
            != _normalized_contact_key(email)
        ):
            contact = find_contact_by_query(
                display_name,
                is_email_query=False,
                is_display_name_query=True,
            )

        if contact is None:
            fallback_contact = {
                "accountType": "customer"
                if is_customer_contact
                else (customer.get("accountType") or "unknown"),
                "loggedInStatus": "UNKNOWN",
                "agent": None,
            }
            return _merge_customer_contact(customer, fallback_contact)
        contact_cache[key] = contact
        return _merge_customer_contact(customer, contact)

    contacts: list[dict[str, Any]] = []
    worker_count = min(
        MAX_CONCURRENT_WORKERS, max(1, int(workers))
    )
    if pending_customers:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(worker_count, len(pending_customers))
        ) as executor:
            chunk_size = max(1, worker_count * 4)
            for offset in range(0, len(pending_customers), chunk_size):
                batch = pending_customers[offset : offset + chunk_size]
                for contact in executor.map(fetch_contact_details, batch):
                    contacts.append(contact)
                    customer_id = str(
                        contact.get("accountId")
                        or contact.get("customerAccountId")
                        or ""
                    ).casefold()
                    enriched_by_id[customer_id] = contact
                    if is_tty:
                        sys.stderr.write(
                            "\rResolving contact details: "
                            f"{len(contacts)}/{len(pending_customers)}..."
                        )
                        sys.stderr.flush()
                    elif len(contacts) % 500 == 0:
                        sys.stderr.write(
                            f"Resolved contact details: {len(contacts)}/"
                            f"{len(pending_customers)}.\n"
                        )
    if is_tty and customers:
        sys.stderr.write("\n")
        sys.stderr.flush()
    return [
        customer
        if customer.get("_filter_reason")
        else enriched_by_id.get(
            str(customer.get("accountId") or customer.get("identifier") or "").casefold(),
            customer,
        )
        for customer in customers
    ]


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


def is_portal_customer_id(account_id: str) -> bool:
    return bool(account_id) and account_id.casefold().startswith("qm:")


def has_issues(config: dict[str, str], account_id: str) -> bool | None:
    if not account_id or is_portal_customer_id(account_id):
        return False
    query = f'reporter = "{account_id}" OR creator = "{account_id}"'
    encoded_query = urllib.parse.quote(query)
    try:
        response = request_json(
            f"{config['site']}/rest/api/3/search?maxResults=1&jql={encoded_query}",
            api="jira",
            headers=jira_headers(config["jira_email"], config["jira_pat"]),
        )
    except ApiError as error:
        if error.status == 400:
            return False
        if error.status in {401, 403}:
            raise
        return None
    except (OSError, http.client.HTTPException):
        return None
    if not isinstance(response, dict):
        return None
    total = response.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return None
    return total > 0


def has_issues_for_accounts(
    config: dict[str, str], account_ids: list[str], workers: int = CONTACT_PAGE_WORKERS
) -> dict[str, bool | None]:
    if not account_ids:
        return {}
    results: dict[str, bool | None] = {}
    worker_count = min(
        len(account_ids),
        MAX_CONCURRENT_WORKERS,
        max(1, workers),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        chunk_size = max(1, worker_count * 4)
        for offset in range(0, len(account_ids), chunk_size):
            batch = account_ids[offset : offset + chunk_size]
            futures = {
                executor.submit(has_issues, config, account_id): account_id
                for account_id in batch
            }
            for future in concurrent.futures.as_completed(futures):
                account_id = futures[future]
                try:
                    result = future.result()
                    if result is True or result is False or result is None:
                        results[account_id] = result
                    else:
                        warn(
                            f"Issue verification returned an invalid result for "
                            f"{account_id}; the customer will be skipped."
                        )
                        results[account_id] = None
                except ApiError as error:
                    if error.status in {401, 403}:
                        raise
                    warn(
                        f"Issue verification failed for {account_id} ({error}); "
                        "the customer will be skipped."
                    )
                    results[account_id] = None
                except Exception as error:
                    warn(
                        f"Issue verification failed for {account_id} ({error}); "
                        "the customer will be skipped."
                    )
                    results[account_id] = None
    return results


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


CUSTOMER_STATUS_ALIASES = {
    "pendinglogin": "pending_login",
    "pending_login": "pending_login",
    "has_pending_login": "pending_login",
    "haspendinglogin": "pending_login",
    "loggedin": "logged_in",
    "logged_in": "logged_in",
    "has_logged_in": "logged_in",
    "hasloggedin": "logged_in",
}


def normalize_customer_status(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_]+", "_", normalized)
    normalized = normalized.strip("_")
    return CUSTOMER_STATUS_ALIASES.get(normalized, normalized)


def customer_status(customer: dict[str, Any]) -> str:
    if not isinstance(customer, dict):
        return ""
    for key in (
        "loggedInStatus",
        "logged_in_status",
        "loginStatus",
        "login_status",
        "status",
    ):
        status = normalize_customer_status(customer.get(key))
        if status:
            return status
    return ""


def customer_status_label(status: str) -> str:
    normalized = normalize_customer_status(status)
    return {
        "pending_login": "PENDING_LOGIN",
        "logged_in": "LOGGED_IN",
    }.get(normalized, normalized or "unknown")


def _merge_customer_contact(
    customer: dict[str, Any], contact: dict[str, Any]
) -> dict[str, Any]:
    account_id = str(
        customer.get("accountId")
        or customer.get("customerAccountId")
        or customer.get("identifier")
        or ""
    )
    merged = {
        **customer,
        **{
            key: value
            for key, value in contact.items()
            if key not in {"accountId", "identifier", "customerAccountId"}
        },
        "accountId": account_id,
        "customerAccountId": account_id,
    }
    contact_agent = contact.get("agent")
    merged["agent"] = contact_agent if isinstance(contact_agent, bool) else None
    merged["contactAgentVerified"] = isinstance(contact_agent, bool)
    contact_identifier = contact.get("identifier") or contact.get("accountId")
    if contact_identifier:
        merged["contactIdentifier"] = str(contact_identifier)
    return merged


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
    eligible_candidates: list[tuple[dict[str, str], str, str]] = []
    error_count = 0
    org_users: dict[str, dict[str, Any]] | None = None
    contact_cache: dict[tuple[str, str], dict[str, Any]] = {}
    project_contact_cache: dict[
        str, tuple[dict[str, dict[str, Any] | None], ...]
    ] = {}
    contact_workers = min(
        MAX_CONCURRENT_WORKERS,
        max(
            1,
            args.workers if args.contact_workers is None else args.contact_workers,
        ),
    )
    issue_workers = min(
        MAX_CONCURRENT_WORKERS,
        max(1, args.workers if args.issue_workers is None else args.issue_workers),
    )

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
            input_ids=input_ids,
            input_filter_enabled=input_mode,
            exclude_ids=excluded_ids,
            contact_cache=contact_cache,
            project_contact_cache=project_contact_cache,
            workers=contact_workers,
        )
        for customer in customers:
            account_id = str(
                customer.get("accountId") or customer.get("customerAccountId") or ""
            )
            email = str(customer.get("email") or customer.get("emailAddress") or "")
            name = str(customer.get("displayName") or "")
            agent = customer.get("agent")
            agent_is_verified = customer.get("contactAgentVerified") is True
            match_keys = {key.casefold() for key in (account_id, email) if key}
            requested_matches.update(match_keys & input_ids)
            status = customer_status(customer)
            row = {
                "email": email,
                "display_name": name,
                "account_id": account_id,
                "account_type": "unknown",
                "service_desk": desk_name,
                "status": customer_status_label(status),
                "action": "skipped",
                "reason": "",
            }
            if not account_id:
                row["action"] = "error"
                row["reason"] = "customer record has no account ID"
                error_count += 1
                rows.append(row)
                continue
            filter_reason = customer.get("_filter_reason") or (
                "excluded by file"
                if match_keys & excluded_ids
                else "not in input file"
                if input_mode and not (match_keys & input_ids)
                else ""
            )
            if filter_reason:
                row["reason"] = str(filter_reason)
                rows.append(row)
                continue

            if not agent_is_verified or agent is not False:
                if agent is True:
                    row["account_type"] = "agent"
                elif is_portal_customer_id(account_id) or str(
                    customer.get("accountType")
                    or customer.get("account_type")
                    or ""
                ).casefold() == "customer":
                    row["account_type"] = "customer"
                row["reason"] = (
                    "contact is an agent and not a customer"
                    if agent is True
                    else "contact agent status is missing or invalid"
                )
                rows.append(row)
                continue

            account_type = (
                "customer"
                if is_portal_customer_id(account_id)
                or str(
                    customer.get("accountType")
                    or customer.get("account_type")
                    or ""
                ).casefold()
                == "customer"
                else "atlassian"
                if str(
                    customer.get("accountType")
                    or customer.get("account_type")
                    or ""
                ).casefold()
                == "atlassian"
                else "unknown"
            )
            row["account_type"] = account_type

            if status != "pending_login":
                row["reason"] = (
                    "customer has logged in / active"
                    if status == "logged_in"
                    else "contact is not in PENDING_LOGIN status"
                )
                rows.append(row)
                continue

            if account_type != "customer":
                profile = jira_user_profile(config, account_id)
                profile_type = str(
                    profile.get("accountType") or profile.get("account_type") or ""
                ).casefold()
                org_user = None
                if profile_type == "atlassian":
                    if not config.get("org") or not config.get("token"):
                        row["reason"] = (
                            "Organization validation unavailable: credentials "
                            "are missing"
                        )
                        rows.append(row)
                        continue
                    if org_users is None:
                        org_users = load_org_users(config)
                    org_user = org_users.get(account_id.casefold())
                is_portal, portal_reason = is_portal_only(
                    profile_type, org_user, profile
                )
                if not is_portal:
                    row["reason"] = (
                        portal_reason or "account is not eligible for removal"
                    )
                    rows.append(row)
                    continue
                row["account_type"] = profile_type

            eligible_candidates.append((row, desk_id, account_id))

    issue_results = has_issues_for_accounts(
        config,
        [account_id for _, _, account_id in eligible_candidates],
        workers=issue_workers,
    )
    candidates: list[tuple[dict[str, str], str, str]] = []
    for row, desk_id, account_id in eligible_candidates:
        issue_status = issue_results.get(account_id)
        if issue_status is True:
            row["reason"] = "customer has reported or created issues"
            rows.append(row)
        elif issue_status is False:
            candidates.append((row, desk_id, account_id))
        else:
            row["reason"] = "customer issue history could not be verified"
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
        total_revocations = len(desk_candidates)
        processed_revocations = 0
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
                    processed_revocations += len(batch)
                    if sys.stderr.isatty():
                        sys.stderr.write(
                            f"\rRevoking access: {processed_revocations}/{total_revocations}..."
                        )
                        sys.stderr.flush()
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
        if sys.stderr.isatty():
            sys.stderr.write("\n")
            sys.stderr.flush()

    for row in rows:
        print_row(row)
    report_path = write_report(rows, args.out)
    candidates_count = len(candidates)
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


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove PENDING_LOGIN JSM portal customers from service desks."
        )
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
        help="CSV or text list limiting eligible PENDING_LOGIN customer IDs/emails",
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
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=CONTACT_PAGE_WORKERS,
        help="Backward-compatible default concurrency for contacts and issue checks",
    )
    parser.add_argument(
        "--contact-workers",
        type=positive_int,
        help="Maximum concurrent contact-detail requests (default: --workers)",
    )
    parser.add_argument(
        "--issue-workers",
        type=positive_int,
        help="Maximum concurrent issue-history requests (default: --workers)",
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
    finally:
        _close_connections()


if __name__ == "__main__":
    raise SystemExit(main())
