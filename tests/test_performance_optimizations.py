import threading
import unittest
import urllib.parse
from unittest import mock

import AtlRevokeUnverifiedCustomers as app


class PerformanceOptimizationTests(unittest.TestCase):
    def tearDown(self) -> None:
        app._close_connections()

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
        contact_queries: list[str] = []
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

            self.assertIn(
                "/rest/servicedesk/1/project/34/contacts", parsed_url.path
            )
            account_id = params["query"][0]
            self.assertNotIn("start", params)
            self.assertNotIn("limit", params)
            with lock:
                contact_queries.append(account_id)
            return {
                "contactResults": [
                    {
                        "identifier": account_id,
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

        self.assertEqual(page_starts, [0, 50, 100])
        self.assertCountEqual(contact_queries, [f"user-{index}" for index in range(103)])
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
        self.assertFalse(contacts[0]["agent"])

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
            self.assertEqual(
                urllib.parse.parse_qs(parsed_url.query), {"query": [account_id]}
            )
            return {
                "contactResults": [
                    {
                        "identifier": account_id,
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
        self.assertTrue(contacts[0]["agent"])

    def test_has_issues_skips_portal_customer_ids(self) -> None:
        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }

        with mock.patch.object(app, "request_json") as request_json:
            self.assertFalse(app.has_issues(config, "qm:test-portal-account"))
            request_json.assert_not_called()

    def test_is_portal_only_allows_customer_without_org_entry(self) -> None:
        self.assertEqual(app.is_portal_only("customer", None, None), (True, ""))


if __name__ == "__main__":
    unittest.main()
