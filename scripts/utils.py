import hashlib
import os
import pathlib
import re
import configparser

Print = print


class DirectoryListing:

    def __init__(self,
                 working_dir: str,
                 config_parser: configparser):

        self.cwd = os.path.abspath(working_dir)
        try:
            self.dir_download = os.path.join(self.cwd, config_parser.get('Directories', 'Download'))
            self.dir_log = os.path.join(self.cwd, config_parser.get('Directories', 'Log'))
            self.dir_cache = os.path.join(self.cwd, config_parser.get('Directories', 'Cache'))
            self.dir_temp = os.path.join(self.cwd, config_parser.get('Directories', 'Temp'))
            self.dir_source = os.path.join(self.cwd, config_parser.get('Directories', 'Source'))
        except configparser.Error as e:
            Print(f"Athena Linux: Config Parser Error: {e}")
            exit(1)

        try:
            os.access(self.cwd, os.W_OK)
            pathlib.Path(self.dir_download).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_log).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_cache).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_temp).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_source).mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            Print(f"Athena Linux: Insufficient permissions in the working directory: {e}")
            exit(1)


class BaseDistribution:
    def __init__(self, url: str, baseid: str, codename: str, version: str, arch: str):
        self.url: str = url
        self.baseid: str = baseid
        self.codename: str = codename
        self.version: str = version
        self.arch: str = arch


def download_file(url: str, filename: str) -> int:
    """Downloads file and updates progressbar in incremental manner.
        Args:
            url (str): url to download file from, protocol is prepended
            filename (str): Filename to save to, location should be writable

        Returns:
            int: -1 for failure, file_size on success
    """
    import requests
    from tqdm import tqdm
    from urllib.parse import urlsplit
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException

    file_size = 0
    downloaded = 0

    name_strip = urlsplit(url).path.split('/')[-1]
    progress_format = '{percentage:3.0f}%[{bar:30}]{n_fmt}/{total_fmt} ({rate_fmt}) - {desc}'
    try:
        response = requests.head(url)
        file_size = int(response.headers.get('content-length', 0))
        progress_bar = tqdm(desc=f"{name_strip.ljust(15, ' ')}", ncols=80, total=file_size,
                            bar_format=progress_format, unit='iB', unit_scale=True, unit_divisor=1024)
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
                        progress_bar.update(len(chunk))
    except (ConnectionError, Timeout, TooManyRedirects, HTTPError, RequestException) as e:
        Print(f"Error connecting to {url}: {e}")
        return -1
    progress_bar.clear()
    progress_bar.close()
    return file_size


def download_source(source_packages, dir_download, base_distribution: BaseDistribution):
    # base_url = "http://deb.debian.org/debian/"
    base_url = 'http://' + base_distribution.url + '/' + base_distribution.baseid + '/'

    for pkg in source_packages:
        for file in source_packages[pkg].files:
            url = base_url + source_packages[pkg].files[file]['path']
            size = source_packages[pkg].files[file]['size']
            md5 = source_packages[pkg].files[file]['md5']

            download_path = os.path.join(dir_download, file)
            md5_check = get_md5(download_path)

            # download only if there is a hash mismatch
            if md5 != md5_check:
                download_file(url, download_path)

            # Verify hash and download file size
            md5_check = get_md5(download_path)
            if md5 != md5_check:
                Print(f"ERROR: Hash mismatch for {download_path}")


def search(re_string: str, base_string: str) -> str:
    """
    Internal function to simplify re.search() execution
    Args:
        re_string: the regex to execute
        base_string: the content on which it is to be executed

    Returns:
        str: Match group, empty string on no match
    """
    _match = re.search(re_string, base_string)
    if _match is not None:
        return _match.group(1)
    return ''


def get_md5(filepath: str) -> str:
    """
    Internal function to calculate the md5 of given file
    Args:
        filepath: The file to calculate md5 hash of

    Returns:
        str: md5
    """
    md5_check = ''
    if os.path.isfile(filepath):
        # Open the file and calculate the MD5 hash
        with open(filepath, 'rb') as f:
            fdata = f.read()
            md5_check = hashlib.md5(fdata).hexdigest()

    return md5_check
