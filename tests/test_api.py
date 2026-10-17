"""Retry and backoff for USAJOBS requests, with sleep mocked out."""

import io
import json
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError, URLError

from usajobs_monitor import api

PAYLOAD = {"SearchResult": {"SearchResultCountAll": 0, "SearchResultItems": []}}


def ok():
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(PAYLOAD).encode()
    return response


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(api.USAJOBS_SEARCH_URL, code, "error", headers, io.BytesIO(b"body text"))


class RetryTests(unittest.TestCase):
    def fetch(self, *outcomes):
        with mock.patch.object(api, "urlopen", side_effect=list(outcomes)) as urlopen, \
             mock.patch.object(api.time, "sleep") as sleep:
            try:
                return api.fetch_usajobs({"Keyword": "x"}, "e@example.com", "key"), urlopen, sleep
            except RuntimeError as exc:
                return exc, urlopen, sleep

    def test_success_needs_no_retry(self):
        result, urlopen, sleep = self.fetch(ok())
        self.assertEqual((result, urlopen.call_count, sleep.call_count), (PAYLOAD, 1, 0))

    def test_network_error_and_5xx_retry_with_exponential_backoff(self):
        result, urlopen, sleep = self.fetch(URLError("dns"), http_error(503), http_error(500), ok())
        self.assertEqual(result, PAYLOAD)
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0, 8.0])

    def test_429_honors_retry_after_with_cap(self):
        result, _, sleep = self.fetch(http_error(429, "7"), http_error(429, "9999"), ok())
        self.assertEqual(result, PAYLOAD)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [7.0, api.MAX_RETRY_AFTER])

    def test_client_error_is_not_retried(self):
        result, urlopen, sleep = self.fetch(http_error(401))
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("HTTP 401", str(result))
        self.assertEqual((urlopen.call_count, sleep.call_count), (1, 0))

    def test_gives_up_after_max_attempts(self):
        result, urlopen, sleep = self.fetch(*[URLError("down")] * api.MAX_ATTEMPTS)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("Unable to reach USAJOBS API", str(result))
        self.assertEqual((urlopen.call_count, sleep.call_count), (api.MAX_ATTEMPTS, api.MAX_ATTEMPTS - 1))

    def test_request_headers_and_empty_params(self):
        with mock.patch.object(api, "urlopen", return_value=ok()) as urlopen:
            api.fetch_usajobs({"Keyword": "budget", "RemunerationMinimumAmount": None}, "e@example.com", "key")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization-key"), "key")
        self.assertEqual(request.get_header("User-agent"), "e@example.com")
        self.assertNotIn("Remuneration", request.full_url)


if __name__ == "__main__":
    unittest.main()
