import unittest
import urllib.parse
from unittest import mock

import AtlRevokeUnverifiedCustomers as app


class ResilienceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }

    def test_project_contact_pagination_preserves_partial_results_after_api_error(self) -> None:
        contact = {"identifier": "account-1", "email": "one@example.test"}
        requested_urls: list[str] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            requested_urls.append(url)
            page = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0]
            if page == "1":
                return {
                    "contactResults": [contact],
                    "total": 200,
                    "isLastPage": False,
                }
            raise app.ApiError("jira", "HTTP 404", 404)

        with (
            mock.patch.object(app, "request_json", side_effect=fake_request),
            mock.patch.object(app, "warn") as warn,
        ):
            contacts = app._list_project_contacts(
                self.config, "34", {"Authorization": "Basic token"}
            )

        self.assertEqual(contacts, [contact])
        self.assertEqual(len(requested_urls), 2)
        self.assertTrue(all("/contacts/search?" in url for url in requested_urls))
        warn.assert_called_once()

    def test_initial_project_contact_404_uses_legacy_endpoint(self) -> None:
        contact = {"identifier": "account-1", "email": "one@example.test"}

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            if "/contacts/search?" in url:
                raise app.ApiError("jira", "HTTP 404", 404)
            return {"contactResults": [contact]}

        with mock.patch.object(app, "request_json", side_effect=fake_request) as request_json:
            contacts = app._list_project_contacts(
                self.config, "34", {"Authorization": "Basic token"}
            )

        self.assertEqual(contacts, [contact])
        self.assertEqual(request_json.call_count, 2)

    def test_repeated_project_contact_page_stops_and_keeps_contacts(self) -> None:
        contact = {"identifier": "account-1", "email": "one@example.test"}

        with (
            mock.patch.object(
                app,
                "request_json",
                return_value={
                    "contactResults": [contact],
                    "total": 200,
                    "isLastPage": False,
                },
            ) as request_json,
            mock.patch.object(app, "warn") as warn,
        ):
            contacts = app._list_project_contacts(
                self.config, "34", {"Authorization": "Basic token"}
            )

        self.assertEqual(contacts, [contact])
        self.assertEqual(request_json.call_count, 2)
        warn.assert_called_once()

    def test_jql_http_400_is_unverifiable(self) -> None:
        with mock.patch.object(
            app,
            "request_json",
            side_effect=app.ApiError("jira", "HTTP 400", 400),
        ):
            results = app.has_issues_for_accounts(
                self.config, ["atlassian-account-1"], workers=1
            )

        self.assertIsNone(results["atlassian-account-1"])


if __name__ == "__main__":
    unittest.main()
