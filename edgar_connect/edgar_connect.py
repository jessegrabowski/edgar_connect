import os
import time
import logging
from datetime import datetime as dt
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import pandas as pd
import numpy as np

from zipfile import ZipFile
from io import BytesIO
import re

import pytz
from collections import Counter
from edgar_connect.exceptions import SECServerClosedError
from edgar_connect.user_agent import UserAgent

from rich.progress import (
    Progress,
    TimeElapsedColumn,
    TimeRemainingColumn,
    BarColumn,
)
from rich.table import Table
from rich.console import Console
from pathlib import Path


_log = logging.getLogger(__name__)


class EDGARConnect:
    def __init__(
        self,
        edgar_path,
        user_agent=None,
        edgar_url="https://www.sec.gov/Archives",
        retry_kwargs=None,
        header=None,
        update_user_agent_interval=360,
    ):
        """
        A class for downloading SEC filings from the EDGAR database.

        Parameters
        ----------
        edgar_path : str or path-like
            A path where EDGARConnect will write all its output.
        user_agent : str, optional
            The SEC requests that all bots provide a User_Agent of the form:
            Sample Company Name AdminContact@<sample company domain>.com.
        edgar_url : str, optional
            The base URL of the SEC EDGAR database. Default is "https://www.sec.gov/Archives".
        retry_kwargs : dict, optional
            A dictionary of keyword arguments to pass to requests.packages.urllib3.util.retry.Retry.
            Default settings are:
                total = 8
                backoff_factor = 1
                status_forcelist = [403, 429, 500, 502, 503, 504]
                allowed_methods = ["HEAD", "GET", "OPTIONS"]
        header : dict, optional
            A dictionary of header values to pass to the Requests session. Default values are:
                User-Agent: User_Agent, or None
                Accept-Encoding: gzip, deflate
                Host: www.sec.gov
        update_user_agent_interval : int, optional
            Interval in seconds to update the User-Agent. Default is 360.

        Returns
        -------
        None

        Examples
        --------
        EDGARConnect will create and fill the following directory structure within edgar_path:

        edgar_path/
        ├── master_indexes/
        │   ├── {year}{quarter}.txt
        │   └── ...
        └── {form_name}/
            └── {Company_CIK}_{form_name}_{filing_date}_{file_name}.txt

        master_indexes is a collection of pipe-delimited ("|") tables with the following 5 columns:
            CIK, Company_Name, Form_type, Date_filed , Filename.
            Importantly, Filename is a URL pointing to the report on the EDGAR database.

            The master_indexes folder must be constructed using the download_master_indexes() method before EDGARConnect
            can batch-download filings. Downloading master_indexes requires between 1 and 2 GB of disk space.

        Once master_indexes is downloaded, individual forms over user-specified dates can be downloaded using the
        download_requested_filings() method. Note that download settings must first be set using the
        configure_downloader() method.
        """

        if retry_kwargs is None:
            retry_kwargs = dict(
                total=8,
                backoff_factor=1,
                status_forcelist=[403, 429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS"],
            )

        self.edgar_url = edgar_url
        self.user_agent = UserAgent(user_agent=user_agent)

        if header is None:
            header = {
                "User-Agent": self.user_agent.update_user_agent(),
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-us",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Host": "www.sec.gov",
            }

        self.header = header
        self.last_user_agent_change = time.time()
        self.update_user_agent_interval = update_user_agent_interval

        retry_strategy = Retry(**retry_kwargs)
        self.adapter = HTTPAdapter(max_retries=retry_strategy)
        self.http = requests.Session()
        self.http.mount("https://", self.adapter)

        self.edgar_path = edgar_path
        self.master_path = Path(self.edgar_path) / "master_indexes"
        self._check_for_required_directories()

        self.forms = dict(
            f_10k=["10-K", "10-K405", "10KSB", "10-KSB", "10KSB40"],
            f_10ka=["10-K/A", "10-K405/A", "10KSB/A", "10-KSB/A", "10KSB40/A"],
            f_10kt=["10-KT", "10KT405", "10-KT/A", "10KT405/A"],
            f_10q=["10-Q", "10QSB", "10-QSB"],
            f_10qa=["10-Q/A", "10QSB/A", "10-QSB/A"],
            f_10qt=["10-QT", "10-QT/A"],
            f_10x=[],
        )

        for key in self.forms.keys():
            if key != "f_10x":
                self.forms["f_10x"].extend(self.forms[key])

        self.start_date = None
        self.end_date = None
        self.target_forms = None
        self._configured = False
        self.time_message_displayed = False

    @staticmethod
    def _make_progress_bar():
        """
        Create a progress bar for downloading files.

        Returns
        -------
        Progress
            A progress bar object.
        """
        return Progress(
            "[progress.description]{task.description}",
            "[progress.percentage]{task.percentage:>3.0f}%",
            BarColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )

    def download_master_indexes(self, update_range=2, update_all=False):
        """
        Download the master list of filing URLs from the SEC EDGAR database.

        Parameters
        ----------
        update_range : int, optional
            Overwrite the update_range most recent local files with those from the SEC server.
            Default is 2.
        update_all : bool, optional
            If True, the program will overwrite everything stored locally with what is on the SEC server.
            Default is False.

        Returns
        -------
        None
        """

        self._check_config()

        start_date = self.start_date
        end_date = self.end_date
        n_quarters = (end_date - start_date).n + 1

        update_quarters = [end_date - i for i in range(update_range)]

        with self._make_progress_bar() as progress:
            task = progress.add_task("Downloading...", total=n_quarters)
            for i in range(n_quarters):
                next_date = start_date + i
                force_redownload = update_all or (next_date in update_quarters)

                self._update_master_index(next_date, force_redownload)

                progress.update(task, advance=1)
            progress.update(task, total=n_quarters, completed=n_quarters, refresh=True)

    def configure_downloader(
        self, target_forms, start_date="01-01-1994", end_date=None
    ):
        """
        Provide parameters for scraping EDGAR.

        .. warning::

            With default settings, EDGARConnect will download all available data 10-X family filings (including
            10-K, 10-Q, and all associated amendments) from the SEC EDGAR database. In total, this requires over
            100GB of data! It is strongly recommended that you be more selective about what you download.


        Parameters
        ----------
        target_forms : str or iterable
            Name of the forms to be downloaded (10-K, 10-Q, etc), or a list of such form names.
        start_date : str or datetime, optional
            Date to begin scraping from. Default is '01-01-1994'.
        end_date : str or datetime, optional
            Date on which to end scraping. If None, defaults to today's date.

        Returns
        -------
        None
        """

        # Check if the requested forms are keys in the forms list and grab that list if os
        if isinstance(target_forms, str):
            if target_forms.lower() in self.forms.keys():
                target_forms = self.forms[target_forms.lower()]
            elif target_forms.lower() in ["10k", "all", "everything"]:
                target_forms = self.forms["f_10x"]
            else:
                target_forms = [target_forms]

        self.target_forms = target_forms
        self.start_date = pd.to_datetime(start_date).to_period("Q")

        if end_date is None:
            end_date = dt.today()
        self.end_date = pd.to_datetime(end_date).to_period("Q")
        self._configured = True

    def _download_file_with_retries(
        self,
        target_url: str,
        out_path: str,
        new_filename: str,
        timeout: int = 30,
        max_attempts: int = 2,
    ):
        """
        Download a file from the specified URL and save it locally, retrying once on connection or timeout errors.

        This function attempts to download the content from `target_url` and writes it to `out_path`.
        It updates the user agent before making the HTTP request and handles potential timeouts or
        connection errors by retrying the request one additional time. If the download fails after two attempts,
        it logs the failure and skips saving the file.

        Parameters
        ----------
        target_url : str
            The URL from which to download the file.
        out_path : str
            The full path (including the file name) where the downloaded content will be saved.
        new_filename : str
            The new file name used for logging purposes to identify the file.
        timeout : int, optional
            The timeout in seconds for the HTTP request, by default 30.
        max_attempts: int, optional
            Maximum number of attempts to download the file. Default is 2.

        Returns
        -------
        None
            This function does not return a value. On successful download, the file is written to disk;
            otherwise, it logs an error message after the allowed retry attempts.
        """
        attempts = 0

        while attempts < max_attempts:
            try:
                self._update_user_agent()
                filing = self.http.get(target_url, headers=self.header, timeout=timeout)

                with open(out_path, "w") as file:
                    file.write(filing.content.decode("utf-8", "ignore"))
                return

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ):
                _log.info(
                    f"\nAttempt {attempts + 1} failed for {new_filename} due to timeout/connection error"
                )
                attempts += 1

        _log.info(f"\nSkipping {new_filename} after {max_attempts} failed attempts")

    def download_requested_filings(
        self,
        ignore_time_guidelines=False,
        remove_attachments=False,
        timeout: int = 30,
        max_attempts: int = 2,
    ):
        """
        Method for downloading all forms meeting the requirements set in the configure_downloader() method.

        Parameters
        ----------
        ignore_time_guidelines : bool, optional
            If True, allows downloads outside of SEC recommended times. Default is False.
        remove_attachments : bool, optional
            If True, removes embedded attachments from filings to save disk space. Default is False.
        timeout: int, optional
            Maximum number of seconds to wait for a file download before skipping it.
        max_attempts: int, optional
            Maximum number of tries to download a file before skipping it.

        Returns
        -------
        None
        """

        self._check_config()
        self._time_check(ignore_time_guidelines)

        start_date = self.start_date
        end_date = self.end_date
        n_quarters = (end_date - start_date).n + 1

        _log.info("Gathering URLS for the requested forms...")
        required_files = [
            f"{(start_date + i).year}Q{(start_date + i).quarter}.txt"
            for i in range(n_quarters)
        ]

        with self._make_progress_bar() as progress:
            for i, file_path in enumerate(required_files):
                date_str = required_files[i].split(".")[0]
                _log.info(f"Beginning scraping from {date_str}")
                self._time_check(ignore_time_guidelines)

                path = self.master_path / file_path
                df = pd.read_csv(path, delimiter="|")
                df = df.drop_duplicates()

                for form in self.target_forms:
                    out_dir = self._create_output_directory(form)

                    form_mask = df.Form_type.str.lower() == form.lower()
                    new_filenames = df[form_mask].apply(
                        self._create_new_filename, axis=1
                    )

                    all_in_master = set(new_filenames.values)
                    all_local = set(os.listdir(out_dir))

                    n_forms = len(all_in_master)

                    download_targets = np.array(list(all_in_master - all_local))
                    n_targets = len(download_targets)

                    if n_targets == 0:
                        if n_forms == 0:
                            _log.info(
                                f"{date_str} {form:<10} No filings found on EDGAR, continuing..."
                            )
                        else:
                            _log.info(
                                f"{date_str} {form:<10} All filings downloaded, continuing..."
                            )
                    else:
                        target_mask = new_filenames.isin(download_targets)
                        rows_to_query = df.reindex(new_filenames.index)[target_mask]

                        n_to_download = rows_to_query.shape[0]
                        n_already_downloaded = n_forms - n_to_download

                        _log.info(
                            f"{date_str} {form:<10} Found {n_already_downloaded} / {n_forms} locally, requesting "
                            f"the remaining {n_to_download}..."
                        )

                        task = progress.add_task(
                            f"{date_str} {form:<10}",
                            total=n_forms,
                            start=n_already_downloaded,
                        )

                        for iterrow_tuple in rows_to_query.iterrows():
                            idx, row = iterrow_tuple
                            new_filename = new_filenames[idx]
                            out_path = out_dir / new_filename

                            target_url = self.edgar_url + "/" + row["Filename"]
                            referer = target_url.replace(".txt", "-index.html")
                            self.header["Referer"] = referer
                            self._download_file_with_retries(
                                target_url=target_url,
                                out_path=out_path,
                                new_filename=new_filename,
                                timeout=timeout,
                                max_attempts=max_attempts,
                            )
                            if remove_attachments:
                                self.strip_attachments_from_filing(out_path)

                            progress.update(task, advance=1)
                        progress.update(
                            task, total=n_forms, completed=n_forms, refresh=True
                        )

    def show_available_forms(self):
        """
        Print available forms using a rich table.

        Returns
        -------
        None
        """
        table = Table(title="Available Forms")
        table.add_column("Form Key", justify="right", style="cyan", no_wrap=True)
        table.add_column("Form Names", style="magenta")

        for key, value in self.forms.items():
            table.add_row(key, ", ".join(value))

        console = Console()
        console.print(table)

    def show_download_plan(self):
        """
        Show the download plan based on the configured parameters.

        Returns
        -------
        None
        """
        self._check_config()
        self._check_all_required_indexes_are_downloaded()

        forms = np.atleast_1d(self.target_forms)
        start_date = self.start_date
        end_date = self.end_date
        n_quarters = (end_date - start_date).n + 1

        form_counter = Counter()
        required_files = [
            f"{(start_date + i).year}Q{(start_date + i).quarter}.txt"
            for i in range(n_quarters)
        ]

        for file in required_files:
            file_path = self.master_path / file
            df = pd.read_csv(file_path, delimiter="|")
            form_counter.update(df.Form_type)

        form_sum = 0

        _log.info(
            f"EDGARConnect is prepared to download {len(forms)} types of filings between {start_date} and {end_date}"
        )
        for form in forms:
            _log.info(f"\tNumber of {form}s: {form_counter[form]}")
            form_sum += form_counter[form]

        _log.info("=" * 30)
        _log.info(f"\tTotal files: {form_sum}")

        m, s = np.divmod(form_sum, 60)
        h, m = np.divmod(m, 60)
        d, h = np.divmod(h, 24)

        _log.info(
            f"Estimated download time, assuming 1s per file: {d} Days, {h} hours, {m} minutes, {s} seconds"
        )
        _log.info(
            f"Estimated drive space, assuming 150KB per filing: {form_sum * 150 * 1e-6:0.2f}GB"
        )

    def _update_user_agent(self, force_update=False):
        """
        Update the User-Agent header. If a user-agent was provided during initialization, it will not be updated.

        Parameters
        ----------
        force_update : bool, optional
            If True, forces an update of the User-Agent. Default is False.

        Returns
        -------
        None
        """
        time_to_update = (
            time.time() - self.last_user_agent_change
        ) < self.update_user_agent_interval

        if time_to_update or force_update:
            self.header["User-Agent"] = self.user_agent.update_user_agent()
            self.last_user_agent_change = time.time()

    def _check_config(self):
        """
        Check if the downloader is configured.

        Returns
        -------
        None

        Raises
        ------
        ValueError
            If the downloader is not configured.
        """
        if not self._configured:
            raise ValueError(
                "First define scrape parameters using the configure_downloader() method"
            )

    def _check_for_required_directories(self):
        """
        Check and create required directories.

        Returns
        -------
        None
        """
        self._master_paths_configured = self.master_path.is_dir()
        if not self._master_paths_configured:
            self.master_path.mkdir()

    def _check_all_required_indexes_are_downloaded(self):
        """
        Check if all required index files are downloaded.

        Returns
        -------
        None

        Raises
        ------
        ValueError
            If not all required index files are downloaded.
        """
        start_date = self.start_date
        end_date = self.end_date
        n_quarters = (end_date - start_date).n + 1

        index_files = list(self.master_path.iterdir())
        required_files = [
            f"{(start_date + i).year}Q{(start_date + i).quarter}.txt"
            for i in range(n_quarters)
        ]

        file_checks = [
            self.master_path / file in index_files for file in required_files
        ]

        if not all(file_checks):
            error = (
                "Not all requested dates have an downloaded index file, including:\n"
            )
            for i, check in enumerate(file_checks):
                if not check:
                    error += f"\t {required_files[i]}\n"
            error += "Have you run the method download_master_indexes() to sync local records with the SEC database?"
            raise ValueError(error)

    def __repr__(self):
        """
        Return a string representation of the EDGARConnect object.

        Returns
        -------
        str
            The string representation of the EDGARConnect object.
        """
        from edgar_connect import __version__

        table = Table(title="EDGARConnect Configuration")

        table.add_column("Attribute", justify="right", style="cyan", no_wrap=True)
        table.add_column("Value", style="magenta")

        table.add_row("Version", f"v{__version__}")
        table.add_row("Configured", str(self._configured))

        if self._configured:
            table.add_row("Target Forms", str(self.target_forms))
            table.add_row("Start Date", str(self.start_date))
            table.add_row("End Date", str(self.end_date))
        else:
            table.add_row("Status", "Files to be scraped have NOT been defined.")
            table.add_row(
                "Instructions",
                "Choose scraping targets using the configure_downloader() method",
            )

        console = Console()
        console.print(table)

        return ""

    def _update_master_index(self, date, force_redownload):
        """
        Update the master index for a given date.

        Parameters
        ----------
        date : datetime
            The date for which to update the master index.
        force_redownload : bool
            If True, forces a redownload of the master index.

        Returns
        -------
        None
        """
        target_year = date.year
        target_quarter = date.quarter
        target_url = f"{self.edgar_url}/edgar/full-index/{target_year}/QTR{target_quarter}/master.zip"

        out_path = self.master_path / f"{target_year}Q{target_quarter}.txt"
        file_downloaded = True

        if not out_path.is_file() or force_redownload:
            file_downloaded = False
            with open(out_path, "w") as file:
                file.write("CIK|Company_Name|Form_type|Date_filed|Filename\n")

        if not file_downloaded:
            master_zip = self.http.get(target_url, headers=self.header)
            master_list = ZipFile(BytesIO(master_zip.content))
            master_list = (
                master_list.open("master.idx")
                .read()
                .decode("utf-8", "ignore")
                .splitlines()[11:]
            )

            with open(out_path, "a") as file:
                for line in master_list:
                    file.write(line)
                    file.write("\n")

    @staticmethod
    def _get_date_from_row(row):
        """
        Get the date from a row.

        Parameters
        ----------
        row : pandas.Series
            The row from which to extract the date.

        Returns
        -------
        str
            The date as a string.
        """
        date = pd.to_datetime(row["Date_filed"]).to_period("Q")
        date_str = str(date)

        return date_str

    @staticmethod
    def _get_cik_from_row(row):
        """
        Get the CIK from a row.

        Parameters
        ----------
        row : pandas.Series
            The row from which to extract the CIK.

        Returns
        -------
        str
            The CIK as a string.
        """
        cik = row["CIK"]
        zeros = "0" * (10 - len(str(cik)))
        cik_str = zeros + str(cik)

        return cik_str

    def _create_new_filename(self, row):
        """
        Create a new filename from a row.

        Parameters
        ----------
        row : pandas.Series
            The row from which to create the filename.

        Returns
        -------
        str
            The new filename.
        """
        cik_str = self._get_cik_from_row(row)
        date_str = self._get_date_from_row(row)
        filename = row["Filename"].split("/")[-1]

        new_filename = f"{cik_str}_{date_str}_{filename}"

        return new_filename

    def _create_output_directory(self, form_type):
        """
        Create an output directory for a form type.

        Parameters
        ----------
        form_type : str
            The form type for which to create the directory.

        Returns
        -------
        str
            The path to the created directory.
        """
        dirsafe_form = form_type.replace("/", "")
        out_dir = Path(self.edgar_path) / dirsafe_form

        if not out_dir.is_dir():
            out_dir.mkdir()

        return out_dir

    @staticmethod
    def get_next_document_chunk(text, last_end_idx=0):
        """
        Get the next document chunk from the text.

        Parameters
        ----------
        text : str
            The text from which to extract the document chunk.
        last_end_idx : int, optional
            The index to start searching from. Default is 0.

        Returns
        -------
        slice
            The slice of the document chunk.
        """
        doc_start_idx = text.find(
            "<DOCUMENT>",
            last_end_idx,
        )
        doc_end_idx = text.find(r"</DOCUMENT>", doc_start_idx) + len("</DOCUMENT>")

        return slice(doc_start_idx, doc_end_idx)

    def strip_attachments_from_filing(self, filing_path):
        """
        Strip attachments from a filing.

        Parameters
        ----------
        filing_path : str
            The path to the filing from which to strip attachments.

        Returns
        -------
        None
        """
        start_idx = 0
        doc_counter = 0
        results = {}
        try:
            with open(filing_path, "r", encoding="utf-8") as file:
                text = file.read()
        except UnicodeDecodeError:
            try:
                with open(filing_path, "r") as file:
                    text = file.read()
            except:
                return

        while True:
            doc_slice = self.get_next_document_chunk(text, start_idx)
            doc = text[doc_slice]
            is_img = (
                re.search(
                    r"<FILENAME>.+\.(gif|jpg|jpeg|bmp|png|pdf|xls|xlsx|zip)", doc[:1000]
                )
                is not None
            )
            results[doc_counter] = {"slice": doc_slice, "is_img": is_img}

            start_idx = doc_slice.stop
            doc_counter += 1
            if doc_slice.start == -1:
                break

        with open(filing_path, "w", encoding="utf-8") as file:
            for i in range(len(results)):
                result = results[i]
                if not result["is_img"]:
                    doc_slice = result["slice"]
                    doc = text[doc_slice]
                    file.write(doc)

    @staticmethod
    def _check_file_dir_and_paths_exist(out_dir, out_path):
        """
        Check if the file directory and paths exist.

        Parameters
        ----------
        out_dir : str
            The output directory.
        out_path : str
            The output path.

        Returns
        -------
        bool
            True if the file exists, False otherwise.
        """
        out_dir = Path(out_dir)
        out_path = Path(out_path)
        if not out_dir.is_dir():
            out_dir.mkdir(parents=True)

        return out_path.is_file()

    @staticmethod
    def _check_time_is_SEC_recommended():
        """
        Check if the current time is within SEC recommended hours.

        Returns
        -------
        bool
            True if within SEC recommended hours, False otherwise.
        """
        sec_server_open = 21
        sec_server_close = 6
        local_time = dt.now().astimezone()
        est_timezone = pytz.timezone("US/Eastern")
        est_dt = local_time.astimezone(est_timezone)

        return est_dt.hour >= sec_server_open or est_dt.hour < sec_server_close

    def _time_check(self, ignore_time_guidelines=False):
        """
        Check if the current time is within SEC recommended hours.

        Parameters
        ----------
        ignore_time_guidelines : bool, optional
            If True, ignores SEC recommended hours. Default is False.

        Returns
        -------
        None

        Raises
        ------
        SECServerClosedError
            If the current time is outside SEC recommended hours and ignore_time_guidelines is False.
        """
        SEC_servers_open = self._check_time_is_SEC_recommended()

        if not SEC_servers_open and not ignore_time_guidelines:
            _log.warning("""SEC guidelines request batch downloads be done between 9PM and 6AM EST. If you plan to download
                     a lot of stuff, it is strongly recommended that you wait until then to begin. If your query size
                     is relatively small, or if it's big but you feel like ignoring this guidance from the good people
                     at the SEC, re-run this function with the argument:

                     ignore_time_guidelines = True""")

            raise SECServerClosedError()
