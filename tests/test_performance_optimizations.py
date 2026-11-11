import threading
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

        with (
            mock.patch.object(
                app,
                "request_json",
                return_value={"id": "12", "projectId": "34", "projectName": "HELP"},
            ) as request_json,
            mock.patch.object(app, "list_customers", return_value=customers),
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
            start = int(params["start"][0])
            with lock:
                contact_page_starts.append(start)
            end = min(start + app.PROJECT_CONTACT_PAGE_SIZE, 103)
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
        self.assertEqual(contact_page_starts, [0, 100])
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

    def test_repeated_project_contact_page_fails(self) -> None:
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
        ):
            with self.assertRaisesRegex(app.ApiError, "pagination repeated a page"):
                app._list_project_contacts(config, "34", {})

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
            ({"issues": []}, False),
            ({"issues": [{}]}, True),
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

    def test_missing_org_credentials_fail_closed_for_atlassian_account(self) -> None:
        customer = {
            "accountId": "standard-account",
            "agent": False,
            "loggedInStatus": "PENDING_LOGIN",
        }
        profile = {
            "accountType": "atlassian",
            "applicationRoles": {"size": 0, "items": []},
            "groups": {"size": 0, "items": []},
        }
        args = app.parse_args(["-d", "12", "--dry-run"])
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with (
            mock.patch.object(
                app,
                "request_json",
                return_value={"id": "12", "projectId": "34"},
            ),
            mock.patch.object(app, "list_customers", return_value=[customer]),
            mock.patch.object(app, "jira_user_profile", return_value=profile),
            mock.patch.object(app, "load_org_users") as org_loader,
        ):
            with self.assertRaisesRegex(
                ValueError, "Organization API credentials are required"
            ):
                app.process(args, config)
        org_loader.assert_not_called()

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
            any(call.kwargs.get("method") == "DELETE" for call in request_json.call_args_list)
        )

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

    def test_is_portal_only_allows_customer_without_org_entry(self) -> None:
        self.assertEqual(app.is_portal_only("customer", None, None), (True, ""))


if __name__ == "__main__":
    unittest.main()
