import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
from pathlib import Path

from edgar_connect.edgar_connect import EDGARConnect
from edgar_connect.user_agent import UserAgent
import shutil


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
        self.assertIsInstance(self.edgar_connect.user_agent, UserAgent)

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

    def test_show_download_plan(self):
        self.edgar_connect.configure_downloader(
            target_forms="10-K", start_date="01-01-2020", end_date="06-01-2020"
        )

        with self.assertLogs("edgar_connect.edgar_connect", level="INFO") as cm:
            self.edgar_connect.show_download_plan()

        self.assertIn(
            "EDGARConnect is prepared to download 1 types of filings between 2020Q1 and 2020Q2",
            cm.output[0],
        )
        self.assertIn("Number of 10-Ks: 3", cm.output[1])
        self.assertIn("=" * 30, cm.output[2])
        self.assertIn("Total files: 3", cm.output[3])
        self.assertIn(
            "Estimated download time, assuming 1s per file: 0 Days, 0 hours, 0 minutes, 3 seconds",
            cm.output[4],
        )
        self.assertIn(
            "Estimated drive space, assuming 150KB per filing: 0.00GB", cm.output[5]
        )

    @patch("requests.Session.get")
    def test_download_requested_filings(self, mock_get):
        # Mock the requests.get method to avoid actual API calls
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"Mock content"
        mock_get.return_value = mock_response

        self.edgar_connect.configure_downloader(
            target_forms="10-K", start_date="01-01-2020", end_date="06-01-2020"
        )

        self.edgar_connect.download_requested_filings()

        # Check if the mock_get was called (indicating a download attempt)
        self.assertTrue(mock_get.called)
        self.assertEqual(mock_get.call_count, 3)  # Assuming 3 filings to download

        # Verify that the directory and files were created
        form_dir = Path("tests/mock_data/10-K")
        self.assertTrue(form_dir.is_dir())

        df = pd.read_csv(
            self.edgar_connect.master_path / "2020Q1.txt", delimiter="|"
        ).query('Form_type == "10-K"')
        expected_files = df.apply(self.edgar_connect._create_new_filename, axis=1)

        for file in expected_files:
            self.assertTrue((form_dir / file).is_file())

        # Clean up the created directory and files
        shutil.rmtree(form_dir)


if __name__ == "__main__":
    unittest.main()
