import unittest
from unittest.mock import call, patch

import requests

import earnings_consensus_telegram as batch


def response(status=200):
    result = requests.Response()
    result.status_code = status
    result._content = b"consensus data"
    result._content_consumed = True
    return result


class ConsensusRetryTests(unittest.TestCase):
    def setUp(self):
        self.get = self.enterContext(patch.object(batch.requests, "get"))
        self.sleep = self.enterContext(patch.object(batch.time, "sleep"))
        self.enterContext(patch("builtins.print"))

    def test_connection_and_timeout_then_success(self):
        self.get.side_effect = [requests.ConnectionError("closed"),
                                requests.Timeout("timeout"), response()]
        self.assertEqual(batch.fetch_consensus_html(), "consensus data")
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(5), call(15)])

    def test_transient_http_statuses_are_retried(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.get.reset_mock()
                self.sleep.reset_mock()
                self.get.side_effect = [response(status), response()]
                self.assertEqual(batch.fetch_consensus_html(), "consensus data")
                self.assertEqual(self.get.call_count, 2)
                self.sleep.assert_called_once_with(5)

    def test_exhaustion_raises_without_fourth_attempt(self):
        self.get.side_effect = requests.ConnectionError("closed")
        with self.assertRaises(requests.ConnectionError):
            batch.fetch_consensus_html()
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(5), call(15)])

    def test_access_denial_is_not_retried(self):
        self.get.return_value = response(403)
        with self.assertRaises(requests.HTTPError):
            batch.fetch_consensus_html()
        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_certificate_failure_is_not_retried(self):
        self.get.side_effect = requests.exceptions.SSLError("certificate")
        with self.assertRaises(requests.exceptions.SSLError):
            batch.fetch_consensus_html()
        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_fetch_exhaustion_never_sends_telegram(self):
        self.get.side_effect = requests.ConnectionError("closed")
        with patch.object(batch, "load_dotenv"), \
                patch.dict(batch.os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}), \
                patch("sys.argv", ["earnings_consensus_telegram.py"]), \
                patch.object(batch, "send_message") as send:
            with self.assertRaises(requests.ConnectionError):
                batch.main()
            send.assert_not_called()

    def test_telegram_connection_failure_is_not_retried(self):
        with patch.object(batch.requests, "post", side_effect=requests.ConnectionError()) as post:
            with self.assertRaisesRegex(RuntimeError, "Telegram 요청 실패"):
                batch.send_message("test", "test", "message")
            post.assert_called_once()
            self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
