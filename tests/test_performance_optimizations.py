import threading
import tempfile
import unittest
import urllib.parse
from unittest import mock

import AtlRevokeUnverifiedCustomers as app


class PerformanceOptimizationTests(unittest.TestCase):
    def tearDown(self) -> None:
        app._close_connections()

    def run_process_with_customers(
        self,
        customers: list[dict],
        issue_results: dict[str, bool | None],
        *,
        config: dict[str, str] | None = None,
        profile: dict | None = None,
        org_users: dict | None = None,
        dry_run: bool = True,
        agent_provenance: bool = True,
    ) -> tuple[int, list[dict[str, str]], mock.Mock, mock.Mock, mock.Mock]:
        argv = ["-d", "12"]
        if dry_run:
            argv.append("--dry-run")
        args = app.parse_args(argv)
        config = config or {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        report_rows: list[dict[str, str]] = []
        customer_records = [dict(customer) for customer in customers]
        if agent_provenance:
            for customer in customer_records:
                if isinstance(customer.get("agent"), bool):
                    customer["contactAgentVerified"] = True

        with (
            mock.patch.object(
                app,
                "request_json",
                return_value={"id": "12", "projectId": "34", "projectName": "HELP"},
            ) as request_json,
            mock.patch.object(app, "list_customers", return_value=customer_records),
            mock.patch.object(
                app,
                "write_report",
                side_effect=lambda rows, output=None: (
                    report_rows.extend(rows) or app.Path("report.csv")
                ),
            ),
            mock.patch.object(
                app, "has_issues_for_accounts", return_value=issue_results
            ) as issue_check,
            mock.patch.object(
                app, "load_org_users", return_value=org_users or {}
            ) as org_loader,
            mock.patch.object(
                app, "jira_user_profile", return_value=profile or {}
            ),
        ):
            result = app.process(args, config)

        return result, report_rows, request_json, issue_check, org_loader

    def test_request_json_reuses_https_connection(self) -> None:
        response = mock.Mock(status=200)
        response.read.return_value = b'{"ok": true}'
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch.object(
            app.http.client, "HTTPSConnection", return_value=connection
        ) as create:
            for _ in range(2):
                result = app.request_json(
                    "https://example.test/resource",
                    api="test",
                    headers={},
                )

        self.assertEqual(result, {"ok": True})
        create.assert_called_once_with("example.test", port=None, timeout=30)
        self.assertEqual(connection.request.call_count, 2)

    def test_project_contact_index_is_reused_between_service_desks(self) -> None:
        requests: list[str] = []
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        contact_cache: dict[tuple[str, str], dict] = {}
        project_cache: dict[str, tuple[dict[str, dict | None], ...]] = {}

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed = urllib.parse.urlsplit(url)
            requests.append(url)
            if "/servicedeskapi/servicedesk/" in parsed.path:
                desk_id = parsed.path.split("/")[-2]
                account_id = f"qm:{desk_id}"
                return {
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": f"{desk_id}@example.test",
                        }
                    ],
                }
            if parsed.path.endswith("/contacts"):
                return {
                    "contactResults": [
                        {
                            "identifier": "qm:12",
                            "email": "12@example.test",
                            "loggedInStatus": "PENDING_LOGIN",
                            "agent": False,
                        },
                        {
                            "identifier": "qm:13",
                            "email": "13@example.test",
                            "loggedInStatus": "PENDING_LOGIN",
                            "agent": False,
                        },
                    ]
                }
            raise AssertionError(f"unexpected individual lookup: {url}")

        with mock.patch.object(app, "request_json", side_effect=fake_request):
            first = app.list_customers(
                config,
                "12",
                project_id="34",
                contact_cache=contact_cache,
                project_contact_cache=project_cache,
            )
            second = app.list_customers(
                config,
                "13",
                project_id="34",
                contact_cache=contact_cache,
                project_contact_cache=project_cache,
            )

        bulk_requests = [
            url for url in requests if urllib.parse.urlsplit(url).path.endswith("/contacts")
        ]
        self.assertEqual(len(bulk_requests), 1)
        self.assertEqual([first[0]["accountId"], second[0]["accountId"]], ["qm:12", "qm:13"])
        self.assertEqual(first[0]["loggedInStatus"], "PENDING_LOGIN")
        self.assertEqual(second[0]["loggedInStatus"], "PENDING_LOGIN")
        self.assertFalse(any("query=" in url for url in requests))

    def test_project_contact_indexes_are_separate_by_project(self) -> None:
        bulk_projects: list[str] = []
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        project_cache: dict[str, tuple[dict[str, dict | None], ...]] = {}

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed = urllib.parse.urlsplit(url)
            if "/servicedeskapi/servicedesk/" in parsed.path:
                project_id = parsed.path.split("/")[-2]
                return {
                    "isLastPage": True,
                    "values": [{"accountId": f"qm:{project_id}"}],
                }
            project_id = parsed.path.split("/")[-2]
            bulk_projects.append(project_id)
            return {
                "contactResults": [
                    {
                        "identifier": f"qm:{project_id}",
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                ]
            }

        with mock.patch.object(app, "request_json", side_effect=fake_request):
            for project_id in ("project-a", "project-b"):
                app.list_customers(
                    config,
                    project_id,
                    project_id=project_id,
                    project_contact_cache=project_cache,
                )

        self.assertEqual(bulk_projects, ["project-a", "project-b"])
        self.assertEqual(set(project_cache), {"project-a", "project-b"})

    def test_unresolved_portal_contact_uses_individual_email_lookup(self) -> None:
        email = "unresolved@example.test"
        queries: list[str | None] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed = urllib.parse.urlsplit(url)
            if "/servicedeskapi/servicedesk/" in parsed.path:
                return {
                    "isLastPage": True,
                    "values": [{"accountId": "qm:unresolved", "emailAddress": email}],
                }
            query = urllib.parse.parse_qs(parsed.query).get("query", [None])[0]
            queries.append(query)
            if query is None:
                return {"contactResults": []}
            return {
                "contactResults": [
                    {
                        "identifier": "contact-id",
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                ]
            }

        with mock.patch.object(app, "request_json", side_effect=fake_request):
            customers = app.list_customers(
                {
                    "site": "https://example.test",
                    "jira_email": "user@example.test",
                    "jira_pat": "not-used",
                },
                "12",
                project_id="34",
            )

        self.assertEqual(queries, [None, email])
        self.assertEqual(customers[0]["accountId"], "qm:unresolved")
        self.assertEqual(customers[0]["contactIdentifier"], "contact-id")

    def test_individual_contact_cache_hit_avoids_project_bulk_request(self) -> None:
        requests: list[str] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            requests.append(url)
            parsed = urllib.parse.urlsplit(url)
            if "/servicedeskapi/servicedesk/" in parsed.path:
                return {
                    "isLastPage": True,
                        "values": [
                            {
                                "accountId": "qm:cached",
                                "emailAddress": "cached@example.test",
                            }
                        ],
                    }
            if parsed.path.endswith("/contacts"):
                    query = urllib.parse.parse_qs(parsed.query).get("query", [None])[0]
                    if query is None:
                        return {"contactResults": []}
                    self.assertEqual(query, "cached@example.test")
                    return {
                        "contactResults": [
                            {
                                "identifier": "contact-id",
                                "loggedInStatus": "PENDING_LOGIN",
                                "agent": False,
                            }
                        ]
                    }
            raise AssertionError(f"unexpected request: {url}")

        contact_cache: dict[tuple[str, str], dict] = {}
        project_cache: dict[str, tuple[dict[str, dict | None], ...]] = {}
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            for _ in range(2):
                app.list_customers(
                    {
                        "site": "https://example.test",
                        "jira_email": "user@example.test",
                        "jira_pat": "not-used",
                    },
                    "12",
                    project_id="34",
                    contact_cache=contact_cache,
                    project_contact_cache=project_cache,
                )

        self.assertEqual(
            sum(
                urllib.parse.urlsplit(url).path.endswith("/contacts")
                for url in requests
            ),
            2,
        )
        contact_queries = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("query")
            for url in requests
            if urllib.parse.urlsplit(url).path.endswith("/contacts")
        ]
        self.assertEqual(contact_queries, [None, ["cached@example.test"]])
        self.assertEqual(len(requests), 4)

    def test_public_customer_pages_are_enriched_with_contact_details(self) -> None:
        page_starts: list[int] = []
        contact_page_starts: list[int] = []
        lock = threading.Lock()

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            params = urllib.parse.parse_qs(parsed_url.query)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                start = int(params["start"][0])
                with lock:
                    page_starts.append(start)
                end = min(start + 50, 103)
                return {
                    "start": start,
                    "limit": 50,
                    "isLastPage": end == 103,
                    "values": [
                        {
                            "accountId": f"user-{index}",
                            "emailAddress": f"user-{index}@example.test",
                        }
                        for index in range(start, end)
                    ],
                }

            self.assertEqual(
                parsed_url.path, "/rest/servicedesk/1/project/34/contacts"
            )
            start = int(params.get("start", ["0"])[0])
            with lock:
                contact_page_starts.append(start)
            end = 103
            return {
                "contactResults": [
                    {
                        "identifier": f"user-{index}",
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                    for index in range(start, end)
                ],
                "contactPageMetadata": {
                    "start": start,
                    "total": 103,
                },
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(page_starts, [0, 50, 100])
        self.assertEqual(contact_page_starts, [0])
        self.assertEqual(len(contacts), 103)
        self.assertTrue(all(contact["agent"] is False for contact in contacts))
        self.assertTrue(
            all(contact["loggedInStatus"] == "PENDING_LOGIN" for contact in contacts)
        )

    def test_repeated_public_customer_page_fails_instead_of_truncating_scan(self) -> None:
        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            return {
                "start": 0,
                "limit": 50,
                "isLastPage": False,
                "values": [{"accountId": f"user-{index}"} for index in range(50)],
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            with self.assertRaisesRegex(app.ApiError, "pagination repeated a page"):
                app.list_customers(config, "12", project_id="34")

    def test_empty_input_file_filters_every_customer_without_contact_enrichment(self) -> None:
        page_starts: list[int] = []
        contact_requests: list[str] = []

        def fake_request(
            url: str,
            *,
            api: str,
            headers: dict[str, str],
            method: str = "GET",
            body: dict | None = None,
        ) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if parsed_url.path == "/rest/servicedeskapi/servicedesk/12":
                return {"id": "12", "projectId": "34", "projectName": "HELP"}
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                start = int(urllib.parse.parse_qs(parsed_url.query)["start"][0])
                page_starts.append(start)
                end = min(start + 50, 51)
                return {
                    "isLastPage": end == 51,
                    "values": [
                        {
                            "accountId": f"qm:customer-{index}",
                            "emailAddress": f"user-{index}@example.test",
                        }
                        for index in range(start, end)
                    ],
                }
            contact_requests.append(url)
            raise AssertionError("filtered customers must not trigger contact lookups")

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = app.Path(temp_dir) / "empty.txt"
            input_path.write_text("", encoding="utf-8")
            args = app.parse_args(
                ["-d", "12", "--input-file", str(input_path), "--dry-run"]
            )
            report_rows: list[dict[str, str]] = []
            with (
                mock.patch.object(app, "request_json", side_effect=fake_request),
                mock.patch.object(
                    app,
                    "write_report",
                    side_effect=lambda rows, output=None: (
                        report_rows.extend(rows) or app.Path("report.csv")
                    ),
                ),
            ):
                result = app.process(
                    args,
                    {
                        "site": "https://example.test",
                        "jira_email": "user@example.test",
                        "jira_pat": "not-used",
                    },
                )

        self.assertEqual(result, 0)
        self.assertEqual(page_starts, [0, 50])
        self.assertEqual(len(report_rows), 51)
        self.assertTrue(
            all(row["reason"] == "not in input file" for row in report_rows)
        )
        self.assertEqual(contact_requests, [])

    def test_excluded_and_nonmatching_customers_are_reported_without_missing_target(self) -> None:
        def fake_request(
            url: str,
            *,
            api: str,
            headers: dict[str, str],
            method: str = "GET",
            body: dict | None = None,
        ) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if parsed_url.path == "/rest/servicedeskapi/servicedesk/12":
                return {"id": "12", "projectId": "34", "projectName": "HELP"}
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": "qm:excluded",
                            "emailAddress": "excluded@example.test",
                        },
                        {
                            "accountId": "qm:other",
                            "emailAddress": "other@example.test",
                        },
                    ],
                }
            raise AssertionError("filtered customers must not trigger contact lookups")

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = app.Path(temp_dir) / "input.txt"
            exclude_path = app.Path(temp_dir) / "exclude.txt"
            input_path.write_text("qm:excluded\n", encoding="utf-8")
            exclude_path.write_text("qm:excluded\n", encoding="utf-8")
            args = app.parse_args(
                [
                    "-d",
                    "12",
                    "--input-file",
                    str(input_path),
                    "--exclude-file",
                    str(exclude_path),
                    "--dry-run",
                ]
            )
            report_rows: list[dict[str, str]] = []
            with (
                mock.patch.object(app, "request_json", side_effect=fake_request),
                mock.patch.object(
                    app,
                    "write_report",
                    side_effect=lambda rows, output=None: (
                        report_rows.extend(rows) or app.Path("report.csv")
                    ),
                ),
                mock.patch.object(app, "log_error") as log_error,
            ):
                result = app.process(
                    args,
                    {
                        "site": "https://example.test",
                        "jira_email": "user@example.test",
                        "jira_pat": "not-used",
                    },
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            [row["reason"] for row in report_rows],
            ["excluded by file", "not in input file"],
        )
        log_error.assert_not_called()

    def test_project_contacts_keep_first_page_when_endpoint_has_more_pages(self) -> None:
        contact = {
            "identifier": "account-1",
            "loggedInStatus": "PENDING_LOGIN",
            "agent": False,
        }
        config = {"site": "https://example.test"}

        with mock.patch.object(
            app,
            "request_json",
            return_value={"contactResults": [contact], "isLastPage": False},
        ) as request_json:
            contacts = app._list_project_contacts(config, "34", {})
        self.assertEqual(contacts, [contact])
        request_json.assert_called_once()
        self.assertNotIn("start=", request_json.call_args.args[0])

    def test_portal_customer_without_contact_record_is_not_assumed_pending_login(self) -> None:
        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedesk/1/project/34/contacts" in parsed_url.path:
                raise app.ApiError("jira", "HTTP 404", 404)
            return {
                "start": 0,
                "limit": 50,
                "isLastPage": True,
                "values": [
                    {
                        "accountId": "qm:test-portal-account",
                        "identifier": "qm:test-portal-account",
                        "emailAddress": "portal@example.test",
                    }
                ],
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["loggedInStatus"], "UNKNOWN")
        self.assertIsNone(contacts[0]["agent"])

    def test_portal_customer_contact_record_provides_its_status(self) -> None:
        account_id = "qm:test-portal-account"

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "start": 0,
                    "limit": 50,
                    "isLastPage": True,
                    "values": [{"accountId": account_id}],
                }

            self.assertEqual(
                parsed_url.path, "/rest/servicedesk/1/project/34/contacts"
            )
            self.assertNotIn("query", urllib.parse.parse_qs(parsed_url.query))
            return {
                "contactResults": [
                    {
                        "identifier": account_id,
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                ],
                "isLastPage": True,
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["loggedInStatus"], "PENDING_LOGIN")
        self.assertFalse(contacts[0]["agent"])

    def test_portal_customer_bulk_contact_matches_by_unique_display_name(self) -> None:
        account_id = "qm:test-portal-account"

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "start": 0,
                    "limit": 50,
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": "portal@example.test",
                            "displayName": "Portal Customer",
                        }
                    ],
                }

            params = urllib.parse.parse_qs(parsed_url.query)
            self.assertNotIn("query", params)
            return {
                "contactResults": [
                    {
                        "identifier": "712020:cloud-account",
                        "displayName": "Portal Customer",
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                ],
                "isLastPage": True,
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["loggedInStatus"], "PENDING_LOGIN")
        self.assertFalse(contacts[0]["agent"])

    def test_incomplete_bulk_contact_retries_individual_lookup(self) -> None:
        account_id = "qm:incomplete-bulk"
        email = "portal@example.test"
        for missing_field in ("agent", "loggedInStatus"):
            with self.subTest(missing_field=missing_field):
                queries: list[str] = []

                def fake_request(
                    url: str, *, api: str, headers: dict[str, str]
                ) -> dict:
                    parsed_url = urllib.parse.urlsplit(url)
                    if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                        return {
                            "isLastPage": True,
                            "values": [
                                {
                                    "accountId": account_id,
                                    "emailAddress": email,
                                    "displayName": "Portal Customer",
                                }
                            ],
                        }

                    params = urllib.parse.parse_qs(parsed_url.query)
                    if not params:
                        bulk_contact = {
                            "identifier": account_id,
                            "loggedInStatus": "PENDING_LOGIN",
                            "agent": False,
                        }
                        del bulk_contact[missing_field]
                        return {"contactResults": [bulk_contact]}

                    query = params["query"][0]
                    queries.append(query)
                    return {
                        "contactResults": [
                            {
                                "identifier": "contact-id",
                                "loggedInStatus": "PENDING_LOGIN",
                                "agent": False,
                            }
                        ]
                    }

                with mock.patch.object(app, "request_json", side_effect=fake_request):
                    contacts = app.list_customers(
                        {
                            "site": "https://example.test",
                            "jira_email": "user@example.test",
                            "jira_pat": "not-used",
                        },
                        "12",
                        project_id="34",
                    )

                self.assertEqual(queries, [email])
                self.assertEqual(contacts[0]["loggedInStatus"], "PENDING_LOGIN")
                self.assertIs(contacts[0]["agent"], False)
                self.assertTrue(contacts[0]["contactAgentVerified"])

    def test_incomplete_individual_contact_is_not_cached_as_resolved(self) -> None:
        account_id = "qm:incomplete-individual"
        email = "portal@example.test"
        queries: list[str] = []
        contact_cache: dict[tuple[str, str], dict] = {}

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": email,
                            "displayName": "Portal Customer",
                        }
                    ],
                }

            params = urllib.parse.parse_qs(parsed_url.query)
            if not params:
                return {
                    "contactResults": [
                        {
                            "identifier": account_id,
                            "loggedInStatus": "PENDING_LOGIN",
                        }
                    ]
                }
            queries.append(params["query"][0])
            return {
                "contactResults": [
                    {
                        "identifier": "contact-id",
                        "loggedInStatus": "PENDING_LOGIN",
                    }
                ]
            }

        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(
                {
                    "site": "https://example.test",
                    "jira_email": "user@example.test",
                    "jira_pat": "not-used",
                },
                "12",
                project_id="34",
                contact_cache=contact_cache,
            )

        self.assertEqual(queries, [email, "Portal Customer"])
        self.assertNotIn(("34", account_id), contact_cache)
        self.assertEqual(contacts[0]["loggedInStatus"], "UNKNOWN")
        self.assertIsNone(contacts[0]["agent"])
        self.assertFalse(contacts[0]["contactAgentVerified"])

    def test_http_500_after_incomplete_bulk_contact_remains_ineligible(self) -> None:
        account_id = "qm:unverifiable-after-500"
        email = "portal@example.test"

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": email,
                            "displayName": "Portal Customer",
                        }
                    ],
                }

            params = urllib.parse.parse_qs(parsed_url.query)
            if not params:
                return {
                    "contactResults": [
                        {
                            "identifier": account_id,
                            "loggedInStatus": "PENDING_LOGIN",
                        }
                    ]
                }
            raise app.ApiError("jira", "HTTP 500", 500)

        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(
                {
                    "site": "https://example.test",
                    "jira_email": "user@example.test",
                    "jira_pat": "not-used",
                },
                "12",
                project_id="34",
            )

        result, rows, _, _, _ = self.run_process_with_customers(
            contacts, {account_id: False}
        )
        self.assertEqual(result, 0)
        self.assertEqual(rows[0]["action"], "skipped")
        self.assertEqual(
            rows[0]["reason"], "contact agent status is missing or invalid"
        )

    def test_portal_email_lookup_accepts_single_redacted_result(self) -> None:
        account_id = "qm:test-portal-account"
        email = "portal@example.test"
        queries: list[str] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "start": 0,
                    "limit": 50,
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": email,
                            "displayName": "Portal Customer",
                        }
                    ],
                }

            params = urllib.parse.parse_qs(parsed_url.query)
            if "query" not in params:
                return {"contactResults": []}
            queries.append(params["query"][0])
            self.assertEqual(params["query"], [email])
            return {
                "contactResults": [
                    {
                        "identifier": "712020:cloud-account",
                        "displayName": "Portal Customer",
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                ]
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(queries, [email])
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["loggedInStatus"], "PENDING_LOGIN")
        self.assertFalse(contacts[0]["agent"])

    def test_customer_type_without_contact_record_is_not_assumed_pending_login(self) -> None:
        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedesk/1/project/34/contacts" in parsed_url.path:
                raise app.ApiError("jira", "HTTP 404", 404)
            return {
                "start": 0,
                "limit": 50,
                "isLastPage": True,
                "values": [
                    {
                        "accountId": "customer-42",
                        "identifier": "customer-42",
                        "emailAddress": "sink@example.test",
                        "accountType": "customer",
                    }
                ],
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["loggedInStatus"], "UNKNOWN")
        self.assertIsNone(contacts[0]["agent"])

    def test_customer_contact_falls_back_to_email_after_account_id_miss(self) -> None:
        account_id = "customer-42"
        email = "customer@example.test"
        contact_queries: list[str] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "start": 0,
                    "limit": 50,
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": email,
                            "accountType": "customer",
                        }
                    ],
                }

            params = urllib.parse.parse_qs(parsed_url.query)
            if "query" not in params:
                return {"contactResults": []}
            query = params["query"][0]
            contact_queries.append(query)
            if query == account_id:
                return {"contactResults": []}
            self.assertEqual(query, email)
            return {
                "contactResults": [
                    {
                        "identifier": account_id,
                        "email": email,
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                ]
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(contact_queries, [account_id, email])
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["loggedInStatus"], "PENDING_LOGIN")
        self.assertFalse(contacts[0]["agent"])

    def test_portal_customer_falls_back_to_display_name_when_email_misses(self) -> None:
        account_id = "qm:portal-customer"
        email = "customer@example.test"
        display_name = "Portal Customer"
        queries: list[str] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": email,
                            "displayName": display_name,
                        }
                    ],
                }

            params = urllib.parse.parse_qs(parsed_url.query)
            query = params.get("query", [None])[0]
            if query is None:
                return {"contactResults": []}
            queries.append(query)
            if query == email:
                return {"contactResults": []}
            self.assertEqual(query, display_name)
            return {
                "contactResults": [
                    {
                        "identifier": "contact-side-id",
                        "email": "redacted@example.test",
                        "displayName": display_name,
                        "loggedInStatus": "LOGGED_IN",
                        "agent": False,
                    }
                ]
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            customers = app.list_customers(config, "12", project_id="34")

        self.assertEqual(queries, [email, display_name])
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0]["accountId"], account_id)
        self.assertEqual(customers[0]["contactIdentifier"], "contact-side-id")
        self.assertEqual(customers[0]["loggedInStatus"], "LOGGED_IN")
        self.assertIs(customers[0]["agent"], False)
        self.assertTrue(customers[0]["contactAgentVerified"])

    def test_ambiguous_display_name_lookup_stays_unverified(self) -> None:
        account_id = "qm:ambiguous"
        email = "ambiguous@example.test"
        display_name = "Shared Name"

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": account_id,
                            "emailAddress": email,
                            "displayName": display_name,
                        }
                    ],
                }
            query = urllib.parse.parse_qs(parsed_url.query).get("query", [None])[0]
            if query is None or query == email:
                return {"contactResults": []}
            return {
                "contactResults": [
                    {
                        "identifier": "contact-1",
                        "displayName": display_name,
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    },
                    {
                        "identifier": "contact-2",
                        "displayName": display_name,
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    },
                ]
            }

        with mock.patch.object(app, "request_json", side_effect=fake_request):
            customers = app.list_customers(
                {
                    "site": "https://example.test",
                    "jira_email": "user@example.test",
                    "jira_pat": "not-used",
                },
                "12",
                project_id="34",
            )

        self.assertEqual(customers[0]["loggedInStatus"], "UNKNOWN")
        self.assertIsNone(customers[0]["agent"])
        self.assertFalse(customers[0]["contactAgentVerified"])

    def test_transient_contact_query_failure_does_not_abort_customer_processing(self) -> None:
        unresolved_email = "unavailable@example.test"
        resolved_email = "available@example.test"
        requested_queries: list[str] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": "qm:unavailable",
                            "emailAddress": unresolved_email,
                        },
                        {
                            "accountId": "qm:available",
                            "emailAddress": resolved_email,
                        },
                    ],
                }
            if parsed_url.path.endswith("/contacts") and not parsed_url.query:
                raise app.ApiError("jira", "HTTP 500", 500)

            query = urllib.parse.parse_qs(parsed_url.query)["query"][0]
            requested_queries.append(query)
            if query == unresolved_email:
                raise app.ApiError("jira", "HTTP 500", 500)
            if query == resolved_email:
                return {
                    "contactResults": [
                        {
                            "identifier": "contact-available",
                            "loggedInStatus": "PENDING_LOGIN",
                            "agent": False,
                        }
                    ]
                }
            self.fail(f"unexpected contact query: {query}")

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            customers = app.list_customers(config, "12", project_id="34")

        self.assertEqual(requested_queries, [unresolved_email, resolved_email])
        self.assertEqual(len(customers), 2)
        unresolved = next(
            customer for customer in customers if customer["accountId"] == "qm:unavailable"
        )
        resolved = next(
            customer for customer in customers if customer["accountId"] == "qm:available"
        )
        self.assertEqual(unresolved["loggedInStatus"], "UNKNOWN")
        self.assertIsNone(unresolved["agent"])
        self.assertFalse(unresolved["contactAgentVerified"])
        self.assertEqual(resolved["loggedInStatus"], "PENDING_LOGIN")
        self.assertIs(resolved["agent"], False)
        self.assertTrue(resolved["contactAgentVerified"])

    def test_unresolvable_atlassian_customer_is_skipped_without_crashing(self) -> None:
        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedesk/1/project/34/contacts" in parsed_url.path:
                raise app.ApiError("jira", "HTTP 404", 404)
            return {
                "start": 0,
                "limit": 50,
                "isLastPage": True,
                "values": [
                    {
                        "accountId": "atlassian-user-1",
                        "identifier": "atlassian-user-1",
                        "emailAddress": "user@example.test",
                        "accountType": "atlassian",
                    }
                ],
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["loggedInStatus"], "UNKNOWN")
        self.assertIsNone(contacts[0]["agent"])

    def test_customer_account_id_survives_contact_identifier_mismatch(self) -> None:
        original_account_id = "qm:123"
        contact_identifier = "712020:456"
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            parsed_url = urllib.parse.urlsplit(url)
            if "/rest/servicedeskapi/servicedesk/12/customer" in parsed_url.path:
                return {
                    "start": 0,
                    "limit": 50,
                    "isLastPage": True,
                    "values": [
                        {
                            "accountId": original_account_id,
                            "customerAccountId": contact_identifier,
                            "emailAddress": "portal@example.test",
                            "displayName": "Portal Customer",
                        }
                    ],
                }
            return {
                "contactResults": [
                    {
                        "identifier": contact_identifier,
                        "displayName": "Portal Customer",
                        "loggedInStatus": "PENDING_LOGIN",
                        "agent": False,
                    }
                ],
                "isLastPage": True,
            }

        with mock.patch.object(app, "request_json", side_effect=fake_request):
            customers = app.list_customers(config, "12", project_id="34")

        self.assertEqual(customers[0]["accountId"], original_account_id)
        self.assertEqual(customers[0]["customerAccountId"], original_account_id)
        self.assertEqual(customers[0]["contactIdentifier"], contact_identifier)
        self.assertTrue(customers[0]["contactAgentVerified"])
        self.assertIs(customers[0]["agent"], False)

        args = app.parse_args(["-d", "12"])
        deletion_calls: list[dict] = []
        report_rows: list[dict[str, str]] = []

        def capture_request(
            url: str,
            *,
            api: str,
            headers: dict[str, str],
            method: str = "GET",
            body: dict | None = None,
        ) -> dict:
            if method == "DELETE":
                deletion_calls.append(body or {})
            return {"id": "12", "projectId": "34", "projectName": "HELP"}

        with (
            mock.patch.object(app, "request_json", side_effect=capture_request),
            mock.patch.object(app, "list_customers", return_value=customers),
            mock.patch.object(app, "load_org_users") as org_loader,
            mock.patch.object(
                app,
                "write_report",
                side_effect=lambda rows, output=None: (
                    report_rows.extend(rows) or app.Path("report.csv")
                ),
            ),
        ):
            result = app.process(args, config)

        self.assertEqual(result, 0)
        self.assertEqual(deletion_calls, [{"accountIds": [original_account_id]}])
        self.assertEqual(report_rows[0]["account_id"], original_account_id)
        org_loader.assert_not_called()

    def test_has_issues_three_state_results_and_jql_contract(self) -> None:
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        account_id = "712020:account"

        for response, expected in (
            ({"total": 0}, False),
            ({"total": 1}, True),
            ({"total": -1}, None),
            ({"issues": []}, None),
            ({"issues": [{}]}, None),
            ({"total": False}, None),
            ({"unknown": True}, None),
        ):
            with self.subTest(response=response):
                with mock.patch.object(
                    app, "request_json", return_value=response
                ) as request_json:
                    self.assertIs(app.has_issues(config, account_id), expected)
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(request_json.call_args.args[0]).query
                )
                self.assertEqual(query["maxResults"], ["1"])
                self.assertEqual(
                    query["jql"],
                    [f'reporter = "{account_id}" OR creator = "{account_id}"'],
                )

        for status, expected in (
            (400, False),
            (429, None),
            (500, None),
        ):
            with self.subTest(status=status):
                with mock.patch.object(
                    app,
                    "request_json",
                    side_effect=app.ApiError("jira", f"HTTP {status}", status),
                ):
                    self.assertIs(app.has_issues(config, account_id), expected)

        with mock.patch.object(
            app,
            "request_json",
            side_effect=app.ApiError("jira", "request failed: connection reset"),
        ):
            self.assertIsNone(app.has_issues(config, account_id))

        for status in (401, 403):
            with self.subTest(status=status):
                with mock.patch.object(
                    app,
                    "request_json",
                    side_effect=app.ApiError("jira", f"HTTP {status}", status),
                ):
                    with self.assertRaisesRegex(app.ApiError, f"HTTP {status}"):
                        app.has_issues(config, account_id)

    def test_issue_authorization_failure_exits_with_code_two(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                config = {
                    "site": "https://example.test",
                    "jira_email": "user@example.test",
                    "jira_pat": "not-used",
                }

                def check_issues(args: app.argparse.Namespace, resolved: dict) -> int:
                    return int(
                        app.has_issues_for_accounts(resolved, ["712020:account"]) is None
                    )

                with (
                    mock.patch.object(app, "resolve_config", return_value=config),
                    mock.patch.object(
                        app,
                        "request_json",
                        side_effect=app.ApiError(
                            "jira", f"HTTP {status}", status
                        ),
                    ),
                    mock.patch.object(app, "process", side_effect=check_issues),
                ):
                    self.assertEqual(app.main(["-d", "12"]), 2)

    def test_issue_batch_records_unexpected_failures_as_unverifiable(self) -> None:
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }

        def fail_issue_check(config: dict[str, str], account_id: str) -> bool:
            if account_id == "account-api-error":
                raise app.ApiError("jira", "HTTP 500", 500)
            raise RuntimeError("unexpected failure")

        with mock.patch.object(app, "has_issues", side_effect=fail_issue_check):
            results = app.has_issues_for_accounts(
                config, ["account-api-error", "account-unexpected"]
            )
        self.assertEqual(
            results,
            {"account-api-error": None, "account-unexpected": None},
        )

    def test_issue_batch_rejects_non_boolean_results(self) -> None:
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "has_issues", return_value="no") as has_issues:
            results = app.has_issues_for_accounts(config, ["account-1"])
        self.assertEqual(results, {"account-1": None})
        has_issues.assert_called_once_with(config, "account-1")

    def test_non_portal_candidates_require_issue_verification(self) -> None:
        base = {
            "accountId": "712020:customer",
            "accountType": "customer",
            "emailAddress": "customer@example.test",
            "agent": False,
            "loggedInStatus": "PENDING_LOGIN",
        }
        for issues, action, reason in (
            (False, "would remove", ""),
            (True, "skipped", "customer has reported or created issues"),
            (None, "skipped", "customer issue history could not be verified"),
        ):
            with self.subTest(issues=issues):
                result, rows, _, issue_check, _ = self.run_process_with_customers(
                    [base], {base["accountId"]: issues}
                )
                self.assertEqual(result, 0)
                self.assertEqual(rows[0]["action"], action)
                self.assertEqual(rows[0]["reason"], reason)
                issue_check.assert_called_once_with(
                    mock.ANY, [base["accountId"]], workers=app.CONTACT_PAGE_WORKERS
                )

    def test_unavailable_issue_verification_never_issues_delete(self) -> None:
        customer = {
            "accountId": "712020:unverifiable",
            "accountType": "customer",
            "agent": False,
            "loggedInStatus": "PENDING_LOGIN",
        }
        result, rows, request_json, _, _ = self.run_process_with_customers(
            [customer], {customer["accountId"]: None}, dry_run=False
        )
        self.assertEqual(result, 0)
        self.assertEqual(rows[0]["action"], "skipped")
        self.assertEqual(
            rows[0]["reason"], "customer issue history could not be verified"
        )
        self.assertEqual(request_json.call_count, 1)

    def test_candidate_requires_issue_result_to_be_explicitly_false(self) -> None:
        customer = {
            "accountId": "qm:unexpected-issue-result",
            "agent": False,
            "loggedInStatus": "PENDING_LOGIN",
        }
        result, rows, request_json, _, _ = self.run_process_with_customers(
            [customer], {"qm:unexpected-issue-result": "false"}
        )
        self.assertEqual(result, 0)
        self.assertEqual(rows[0]["action"], "skipped")
        self.assertEqual(
            rows[0]["reason"], "customer issue history could not be verified"
        )
        self.assertFalse(
            any(
                call.kwargs.get("method") == "DELETE"
                for call in request_json.call_args_list
            )
        )

    def test_unknown_invitation_status_never_becomes_candidate(self) -> None:
        customer = {
            "accountId": "qm:unknown-status",
            "accountType": "customer",
            "agent": False,
            "loggedInStatus": "UNKNOWN",
        }
        result, rows, _, issue_check, _ = self.run_process_with_customers(
            [customer], {"qm:unknown-status": False}
        )
        self.assertEqual(result, 0)
        self.assertEqual(rows[0]["action"], "skipped")
        self.assertEqual(rows[0]["reason"], "contact is not in PENDING_LOGIN status")
        issue_check.assert_called_once_with(
            mock.ANY, [], workers=app.CONTACT_PAGE_WORKERS
        )

    def test_only_portal_customers_need_no_organization_credentials(self) -> None:
        customer = {
            "accountId": "qm:portal",
            "accountType": "customer",
            "agent": False,
            "loggedInStatus": "PENDING_LOGIN",
        }
        result, rows, request_json, issue_check, org_loader = (
            self.run_process_with_customers([customer], {"qm:portal": False})
        )
        self.assertEqual(result, 0)
        self.assertEqual(rows[0]["action"], "would remove")
        self.assertEqual(request_json.call_count, 1)
        issue_check.assert_called_once_with(
            mock.ANY, ["qm:portal"], workers=app.CONTACT_PAGE_WORKERS
        )
        org_loader.assert_not_called()

    def test_org_directory_loads_lazily_once_for_non_portal_accounts(self) -> None:
        customers = [
            {
                "accountId": account_id,
                "accountType": "atlassian",
                "agent": False,
                "loggedInStatus": "PENDING_LOGIN",
            }
            for account_id in ("standard-a", "standard-b")
        ]
        profile = {
            "accountType": "atlassian",
            "applicationRoles": {"size": 0, "items": []},
            "groups": {"size": 0, "items": []},
        }
        org_users = {
            account_id: {
                "account_type": "atlassian",
                "access_billable": False,
                "product_access": [],
            }
            for account_id in ("standard-a", "standard-b")
        }
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
            "org": "org-1",
            "token": "token-1",
        }

        result, rows, _, issue_check, org_loader = self.run_process_with_customers(
            customers,
            {"standard-a": False, "standard-b": False},
            config=config,
            profile=profile,
            org_users=org_users,
        )
        self.assertEqual(result, 0)
        self.assertEqual([row["action"] for row in rows], ["would remove"] * 2)
        org_loader.assert_called_once_with(config)
        issue_check.assert_called_once_with(
            config,
            ["standard-a", "standard-b"],
            workers=app.CONTACT_PAGE_WORKERS,
        )

    def test_missing_org_credentials_skip_account_and_continue(self) -> None:
        customers = [
            {
                "accountId": "standard-account",
                "agent": False,
                "contactAgentVerified": True,
                "loggedInStatus": "PENDING_LOGIN",
            },
            {
                "accountId": "qm:portal",
                "accountType": "customer",
                "agent": False,
                "contactAgentVerified": True,
                "loggedInStatus": "PENDING_LOGIN",
            },
        ]
        profile = {
            "accountType": "atlassian",
            "applicationRoles": {"size": 0, "items": []},
            "groups": {"size": 0, "items": []},
        }
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        result, rows, _, issue_check, org_loader = self.run_process_with_customers(
            customers,
            {"qm:portal": False},
            config=config,
            profile=profile,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            [(row["account_id"], row["action"]) for row in rows],
            [
                ("standard-account", "skipped"),
                ("qm:portal", "would remove"),
            ],
        )
        self.assertIn("credentials are missing", rows[0]["reason"])
        org_loader.assert_not_called()
        issue_check.assert_called_once_with(
            config, ["qm:portal"], workers=app.CONTACT_PAGE_WORKERS
        )

    def test_resolve_config_requires_jira_but_not_organization_credentials(self) -> None:
        args = app.parse_args(
            [
                "-d",
                "12",
                "--site",
                "example.test",
                "--jira-email",
                "user@example.test",
                "--jira-pat",
                "jira-token",
            ]
        )
        with mock.patch.dict(
            app.os.environ,
            {"ATLASSIAN_TOKEN": "", "ATLASSIAN_ORG": ""},
            clear=False,
        ):
            config = app.resolve_config(args)
        self.assertEqual(config["site"], "https://example.test")
        self.assertEqual(config["token"], "")
        self.assertEqual(config["org"], "")

    def test_resolve_config_preserves_cli_precedence_over_environment(self) -> None:
        args = app.parse_args(
            [
                "-d",
                "12",
                "--site",
                "cli.example.test",
                "--jira-email",
                "cli@example.test",
                "--jira-pat",
                "cli-jira-token",
                "--token",
                "cli-org-token",
                "--org",
                "cli-org",
            ]
        )
        with mock.patch.dict(
            app.os.environ,
            {
                "ATLASSIAN_SITE": "env.example.test",
                "JIRA_EMAIL": "env@example.test",
                "JIRA_PAT": "env-jira-token",
                "ATLASSIAN_TOKEN": "env-org-token",
                "ATLASSIAN_ORG": "env-org",
            },
            clear=False,
        ):
            config = app.resolve_config(args)
        self.assertEqual(config["site"], "https://cli.example.test")
        self.assertEqual(config["jira_email"], "cli@example.test")
        self.assertEqual(config["jira_pat"], "cli-jira-token")
        self.assertEqual(config["token"], "cli-org-token")
        self.assertEqual(config["org"], "cli-org")

    def test_cli_aliases_and_dry_run_do_not_issue_deletes(self) -> None:
        args = app.parse_args(
            [
                "-d",
                "12",
                "--atlassian-token",
                "org-token",
                "-o",
                "org-id",
                "-s",
                "example.test",
                "--jira-email",
                "user@example.test",
                "--jira-pat",
                "jira-token",
                "--filename",
                "exclude.csv",
                "--dry-run",
            ]
        )
        self.assertEqual(args.token, "org-token")
        self.assertEqual(args.org, "org-id")
        self.assertEqual(args.exclude_file, "exclude.csv")
        self.assertTrue(args.dry_run)
        customer = {
            "accountId": "qm:dry-run",
            "accountType": "customer",
            "agent": False,
            "loggedInStatus": "PENDING_LOGIN",
        }
        result, rows, request_json, _, _ = self.run_process_with_customers(
            [customer], {"qm:dry-run": False}
        )
        self.assertEqual(result, 0)
        self.assertEqual(rows[0]["action"], "would remove")
        self.assertFalse(
            any(
                call.kwargs.get("method") == "DELETE"
                for call in request_json.call_args_list
            )
        )

    def test_independent_worker_limits_and_legacy_default_preserve_eligibility(self) -> None:
        customer = {
            "accountId": "qm:worker-test",
            "accountType": "customer",
            "agent": False,
            "contactAgentVerified": True,
            "loggedInStatus": "PENDING_LOGIN",
        }
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        for extra_args, contact_workers, issue_workers in (
            (
                ["--workers", "7", "--contact-workers", "2", "--issue-workers", "3"],
                2,
                3,
            ),
            (["--workers", "4"], 4, 4),
            (["--workers", "1000"], app.MAX_CONCURRENT_WORKERS, app.MAX_CONCURRENT_WORKERS),
        ):
            with self.subTest(extra_args=extra_args):
                args = app.parse_args(["-d", "12", "--dry-run", *extra_args])
                rows: list[dict[str, str]] = []
                with (
                    mock.patch.object(
                        app,
                        "request_json",
                        return_value={"id": "12", "projectId": "34"},
                    ),
                    mock.patch.object(
                        app, "list_customers", return_value=[customer]
                    ) as list_customers,
                    mock.patch.object(
                        app,
                        "has_issues_for_accounts",
                        return_value={"qm:worker-test": False},
                    ) as issue_check,
                    mock.patch.object(
                        app,
                        "write_report",
                        side_effect=lambda report_rows, output=None: (
                            rows.extend(report_rows) or app.Path("report.csv")
                        ),
                    ),
                ):
                    result = app.process(args, config)

                self.assertEqual(result, 0)
                self.assertEqual(rows[0]["action"], "would remove")
                self.assertEqual(list_customers.call_args.kwargs["workers"], contact_workers)
                self.assertEqual(issue_check.call_args.kwargs["workers"], issue_workers)

    def test_worker_options_require_positive_integers(self) -> None:
        for option in ("--workers", "--contact-workers", "--issue-workers"):
            for value in ("0", "-1", "invalid"):
                with self.subTest(option=option, value=value):
                    with self.assertRaises(SystemExit) as error:
                        app.parse_args(["-d", "12", option, value])
                    self.assertEqual(error.exception.code, 2)

    def test_delete_failure_is_not_retried(self) -> None:
        response = mock.Mock(status=500)
        response.read.return_value = b'{"error": "failed"}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(
            app.http.client, "HTTPSConnection", return_value=connection
        ):
            with self.assertRaisesRegex(app.ApiError, "HTTP 500"):
                app.request_json(
                    "https://example.test/resource",
                    api="jira",
                    headers={},
                    method="DELETE",
                    body={"accountIds": ["qm:123"]},
                )
        self.assertEqual(connection.request.call_count, 1)

    def test_has_issues_skips_portal_customer_ids(self) -> None:
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }

        with mock.patch.object(app, "request_json") as request_json:
            self.assertFalse(app.has_issues(config, "qm:test-portal-account"))
            request_json.assert_not_called()

    def test_has_issues_only_queries_non_portal_accounts(self) -> None:
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }

        with mock.patch.object(
            app, "request_json", return_value={"total": 1}
        ) as request_json:
            results = app.has_issues_for_accounts(
                config, ["qm:test-portal-account", "712020:standard-account"]
            )

        self.assertEqual(
            results,
            {"qm:test-portal-account": False, "712020:standard-account": True},
        )
        request_json.assert_called_once()
        query = urllib.parse.urlsplit(request_json.call_args.args[0]).query
        self.assertIn("712020%3Astandard-account", query)
        self.assertNotIn("qm%3Atest-portal-account", query)

    def test_portal_customer_safety_checks_control_removal(self) -> None:
        args = app.parse_args(["-d", "12", "--dry-run"])
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
            "token": "not-used",
            "org": "1",
        }
        for agent, action, reason in (
            (True, "skipped", "contact is an agent and not a customer"),
            (None, "skipped", "contact agent status is missing or invalid"),
            ("missing", "skipped", "contact agent status is missing or invalid"),
            (False, "would remove", ""),
        ):
            with self.subTest(agent=agent):
                report_rows: list[dict[str, str]] = []
                with (
                    mock.patch.object(
                        app,
                        "request_json",
                        return_value={
                            "id": "12",
                            "projectId": "34",
                            "projectName": "HELP",
                        },
                    ),
                    mock.patch.object(app, "load_org_users", return_value={}),
                    mock.patch.object(
                        app,
                        "list_customers",
                        return_value=[
                            {
                                **{
                                    "accountId": "qm:agent-contact",
                                    "email": "agent@example.test",
                                    "displayName": "Agent",
                                    "loggedInStatus": "PENDING_LOGIN",
                                    "contactAgentVerified": True,
                                },
                                **(
                                    {}
                                    if agent == "missing"
                                    else {"agent": agent}
                                ),
                            }
                        ],
                    ),
                    mock.patch.object(
                        app,
                        "write_report",
                        side_effect=lambda rows, output=None: (
                            report_rows.extend(rows) or app.Path("report.csv")
                        ),
                    ),
                    mock.patch.object(
                        app,
                        "has_issues_for_accounts",
                        side_effect=lambda config, ids, workers: {
                            account_id: False for account_id in ids
                        },
                    ) as has_issues,
                ):
                    result = app.process(args, config)

                self.assertEqual(result, 0)
                self.assertEqual(len(report_rows), 1)
                self.assertEqual(report_rows[0]["action"], action)
                self.assertEqual(report_rows[0]["reason"], reason)
                account_ids = (
                    ["qm:agent-contact"] if agent is False else []
                )
                has_issues.assert_called_once_with(
                    config, account_ids, workers=args.workers
                )

    def test_synthesized_agent_false_without_contact_provenance_is_skipped(self) -> None:
        customer = {
            "accountId": "qm:synthesized-agent",
            "accountType": "customer",
            "agent": False,
            "loggedInStatus": "PENDING_LOGIN",
        }
        result, rows, _, issue_check, _ = self.run_process_with_customers(
            [customer], {"qm:synthesized-agent": False}, agent_provenance=False
        )
        self.assertEqual(result, 0)
        self.assertEqual(rows[0]["action"], "skipped")
        self.assertEqual(
            rows[0]["reason"], "contact agent status is missing or invalid"
        )
        issue_check.assert_called_once_with(mock.ANY, [], workers=app.CONTACT_PAGE_WORKERS)

    def test_is_portal_only_allows_customer_without_org_entry(self) -> None:
        self.assertEqual(app.is_portal_only("customer", None, None), (True, ""))


if __name__ == "__main__":
    unittest.main()
