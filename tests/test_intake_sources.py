import unittest
from unittest.mock import Mock, patch

import filters
from sources import jobicy, remoteok, telegram_channels


class LocationFilterTests(unittest.TestCase):
    def test_emea_is_allowed(self):
        self.assertTrue(filters.passes_location_filter({"location": "EMEA"}))

    def test_europe_only_remains_blocked(self):
        self.assertFalse(filters.passes_location_filter({"location": "Remote, Europe only"}))


class RemoteOkTests(unittest.TestCase):
    @staticmethod
    def _response(items):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [{}, *items]
        return response

    def test_string_location_is_preserved_and_us_only_is_rejected(self):
        item = {
            "position": "Product Manager",
            "company": "Example",
            "location": "US only",
            "date": "2026-07-06",
        }
        with patch.object(remoteok, "retry", return_value=self._response([item])):
            jobs = remoteok.fetch()

        self.assertEqual(jobs[0]["location"], "US only")
        self.assertFalse(filters.passes_location_filter(jobs[0]))

    def test_empty_string_location_is_preserved(self):
        item = {
            "position": "Product Manager",
            "company": "Example",
            "location": "",
            "date": "2026-07-06",
        }
        with patch.object(remoteok, "retry", return_value=self._response([item])):
            jobs = remoteok.fetch()

        self.assertEqual(jobs[0]["location"], "")


class JobicyTests(unittest.TestCase):
    def test_company_html_entities_are_decoded(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "jobs": [{
                "jobTitle": "Product Manager",
                "companyName": "Zone &#038; Co",
                "jobGeo": "EMEA",
                "pubDate": "2026-07-06",
            }]
        }
        with patch.object(jobicy, "retry", return_value=response):
            jobs = jobicy.fetch()

        self.assertEqual(jobs[0]["company"], "Zone & Co")


class TelegramConfigTests(unittest.TestCase):
    def test_empty_channel_list_sends_alert(self):
        send_error = Mock()
        self.assertEqual(telegram_channels.check_config([], send_error), 0)

        send_error.assert_called_once()
        self.assertIn("configured_channels=0", send_error.call_args.args[0])

    def test_nonempty_channel_list_does_not_alert(self):
        send_error = Mock()
        self.assertEqual(
            telegram_channels.check_config(["@one", "@two"], send_error),
            2,
        )

        send_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
