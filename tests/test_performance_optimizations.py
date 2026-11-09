import threading
import unittest
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

    def test_contact_pages_are_fetched_concurrently_and_deduplicated(self) -> None:
        barrier = threading.Barrier(2)
        requested_offsets: list[int] = []
        lock = threading.Lock()

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            offset = int(url.split("start=")[1].split("&")[0])
            with lock:
                requested_offsets.append(offset)
            if offset in {100, 150}:
                barrier.wait(timeout=2)
            start = offset
            end = {
                0: 50,
                50: 100,
                100: 150,
                150: 200,
                200: 220,
            }.get(offset, offset)
            contacts = [{"identifier": f"user-{index}"} for index in range(start, end)]
            if offset == 100:
                contacts.insert(0, {"identifier": "user-99"})
            result = {"contactResults": contacts}
            if offset == 0:
                result["contactPageMetadata"] = {"total": 220}
            return result

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertCountEqual(requested_offsets, [0, 50, 100, 150, 200])
        self.assertEqual(len(contacts), 220)
        self.assertEqual(len({contact["identifier"] for contact in contacts}), 220)

    def test_contact_pages_continue_when_first_page_metadata_claims_last_page(self) -> None:
        requested_offsets: list[int] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            offset = int(url.split("start=")[1].split("&")[0])
            requested_offsets.append(offset)
            end = {0: 50, 50: 100, 100: 103}.get(offset, offset)
            result = {
                "contactResults": [
                    {"identifier": f"user-{index}"}
                    for index in range(offset, end)
                ]
            }
            if offset == 0:
                result["contactPageMetadata"] = {
                    "total": 50,
                    "isLastPage": True,
                }
            return result

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(requested_offsets, [0, 50, 100])
        self.assertEqual(len(contacts), 103)

    def test_contact_pages_without_total_keep_sequential_fallback(self) -> None:
        requested_offsets: list[int] = []

        def fake_request(url: str, *, api: str, headers: dict[str, str]) -> dict:
            offset = int(url.split("start=")[1].split("&")[0])
            requested_offsets.append(offset)
            end = 50 if offset == 0 else 60
            return {
                "contactResults": [
                    {"identifier": f"user-{index}"}
                    for index in range(offset, end)
                ]
            }

        config = {
            "site": "https://example.test",
            "jira_email": "user@example.test",
            "jira_pat": "not-used",
        }
        with mock.patch.object(app, "request_json", side_effect=fake_request):
            contacts = app.list_customers(config, "12", project_id="34")

        self.assertEqual(requested_offsets, [0, 50])
        self.assertEqual(len(contacts), 60)


if __name__ == "__main__":
    unittest.main()
