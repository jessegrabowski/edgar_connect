import unittest
from unittest.mock import patch
import pandas as pd
from fake_useragent import FakeUserAgent

from edgar_connect.edgar_connect import EDGARConnect


class TestEDGARConnect(unittest.TestCase):
    @patch("requests.Session.get")
    def setUp(self, mock_get):
        # Mock the requests.Session.get method to avoid actual API calls
        mock_get.return_value.status_code = 200
        mock_get.return_value.content = b""

        self.edgar_path = "tests/mock_data/"
        self.edgar_connect = EDGARConnect(
            self.edgar_path, edgar_url="tests/mock_data/mock_upstream/"
        )

    def test_initialization(self):
        self.assertEqual(self.edgar_connect.edgar_path, self.edgar_path)
        self.assertIsNotNone(self.edgar_connect.header)
        self.assertIsInstance(self.edgar_connect.user_agent, FakeUserAgent)

    def test_configure_downloader(self):
        self.edgar_connect.configure_downloader(
            target_forms="10-K", start_date="01-01-2020", end_date="12-31-2020"
        )
        self.assertEqual(self.edgar_connect.target_forms, ["10-K"])
        self.assertEqual(
            self.edgar_connect.start_date, pd.to_datetime("01-01-2020").to_period("Q")
        )
        self.assertEqual(
            self.edgar_connect.end_date, pd.to_datetime("12-31-2020").to_period("Q")
        )
        self.assertTrue(self.edgar_connect._configured)


if __name__ == "__main__":
    unittest.main()
