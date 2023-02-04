import hashlib
import logging
import os
import re
from logging import Logger

from rich.console import Console


class BaseDistribution:
    def __init__(self, url: str, baseid: str, codename: str, version: str, arch: str):
        self.url: str = url
        self.baseid: str = baseid
        self.codename: str = codename
        self.version: str = version
        self.arch: str = arch


def download_file(url: str, filename: str, con: Console, logger: Logger) -> int:
    """Downloads file and updates progressbar in incremental manner.
        Args:
            url (str): url to download file from, protocol is prepended
            filename (str): Filename to save to, location should be writable
            con (rich.console): console
            logger: (rich.logging): logger

        Returns:
            int: -1 for failure, file_size on success
    """
    import requests
    from urllib.parse import urlsplit
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException

    # download_progress = Progress(
    # TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    file_size = 0
    downloaded = 0

    logger.info(f"Downloading {urlsplit(url).path.split('/')[-1]}")
    with con.status('') as status:
        try:
            response = requests.head(url)
            file_size = int(response.headers.get('content-length', 0))
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                with open(filename, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024):

                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            percentage = int(downloaded / file_size * 100)
                            status.update(f"Download Progress... {percentage}%", spinner='dots')

        except (ConnectionError, Timeout, TooManyRedirects, HTTPError, RequestException) as e:
            logging.exception(f"Error connecting to {url}: {e}")
            return -1

    return file_size


def download_source(source_packages, dir_download, base_distribution: BaseDistribution,
                    con: Console,
                    logger: Logger):
    # base_url = "http://deb.debian.org/debian/"
    base_url = 'http://' + base_distribution.url + '/' + base_distribution.baseid + '/'

    for pkg in source_packages:
        for file in source_packages[pkg].files:
            url = base_url + source_packages[pkg].files[file]['path']
            size = source_packages[pkg].files[file]['size']
            md5 = source_packages[pkg].files[file]['md5']

            download_path = os.path.join(dir_download, file)
            if os.path.isfile(download_path):
                # Open the file and calculate the MD5 hash
                with open(download_path, 'rb') as f:
                    fdata = f.read()
                    md5_check = hashlib.md5(fdata).hexdigest()
            else:
                md5_check = ''

            if md5 != md5_check:
                download_file(url, download_path, con, logger)

            # TODO: Verify hash and download file size
            pass


def search(re_string: str, base_string: str) -> str:
    """

    :param re_string:
    :param base_string:
    :return:
    """
    _match = re.search(re_string, base_string)
    if _match is not None:
        return _match.group(1)
    return ''
