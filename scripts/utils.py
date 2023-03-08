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
            self.dir_repo = os.path.join(self.cwd, config_parser.get('Directories', 'Repo'))
            self.dir_config = os.path.join(self.cwd, config_parser.get('Directories', 'Config'))
            self.dir_patch = os.path.join(self.cwd, config_parser.get('Directories', 'Patch'))
            self.dir_image = os.path.join(self.cwd, config_parser.get('Directories', 'Image'))
            self.dir_chroot = os.path.join(self.cwd, config_parser.get('Directories', 'Chroot'))
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
            pathlib.Path(self.dir_repo).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch).mkdir(parents=True, exist_ok=True)
            pathlib.Path(os.path.join(self.dir_log, 'build')).mkdir(parents=True, exist_ok=True)
            pathlib.Path(os.path.join(self.dir_patch, 'empty')).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_image).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot).mkdir(parents=True, exist_ok=True)

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


def download_source(dependency_tree, dir_download, base_distribution: BaseDistribution):
    import requests
    from tqdm import tqdm
    from urllib.parse import urljoin
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException

    _downloaded_size = 0
    _download_size = dependency_tree.download_size

    # base_url = "http://deb.debian.org/debian/"
    base_url = 'http://' + base_distribution.url + '/' + base_distribution.baseid + '/'

    # build filelist to download - just for improved readability
    _file_list = {}
    for _pkg in dependency_tree.selected_srcs:
        _file_list.update(dependency_tree.selected_srcs[_pkg].files)

    _index = 1
    _skipped = 0
    _total = len(_file_list)

    progress_format = '{desc} {percentage:3.0f}%[{bar:30}]{n_fmt}/{total_fmt} ({rate_fmt})'
    progress_bar = tqdm(ncols=80, total=_download_size, bar_format=progress_format, unit='iB', unit_scale=True)
    for _file in _file_list:
        progress_bar.set_description_str(desc=f" ({_index}/{_total})")

        _url = urljoin(base_url, _file_list[_file]['path'])
        _md5 = _file_list[_file]['md5']
        _download_path = os.path.join(dir_download, _file)
        _md5_check = get_md5(_download_path)

        # do hash check
        if _md5 != _md5_check:
            # Failed - Lets download again
            try:

                response = requests.head(_url)
                _size = int(response.headers.get('content-length', 0))

                response = requests.get(_url, stream=True)
                if response.status_code == 200:
                    with open(_download_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=1024):
                            if chunk:
                                f.write(chunk)
                                progress_bar.update(len(chunk))
                _downloaded_size += _size

            except (ConnectionError, Timeout, TooManyRedirects, HTTPError, RequestException) as e:
                Print(f"Error connecting to {_url}: {e}")
                continue

            assert get_md5(_download_path) == _md5, f"Downloaded {_file} hash mismatch"

        else:
            _skipped += 1
            progress_bar.update(int(_file_list[_file]['size']))
            _downloaded_size += int(_file_list[_file]['size'])

        _index += 1

    progress_bar.clear()
    progress_bar.close()

    Print(f"Downloading {_total - _skipped} files, Skipped {_skipped} files")
    return _downloaded_size


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


def readfile(filename: str) -> str:
    try:
        with open(filename, 'r') as f:
            contents = f.read()
            return contents
    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)


def create_folders(folder_structure: str):
    # split the folder structure string into individual path components
    components = folder_structure.split('/')

    # iterate over the path components and create the directories
    path = '/'
    try:
        for component in components:
            if '{' in component:
                # expand the braces and create directories for each combination
                subcomponents = component.strip('{}').split(',')
                for subcomponent in subcomponents:
                    new_path = os.path.join(path, subcomponent)
                    os.makedirs(new_path, exist_ok=True)
            else:
                # add the component to the current path
                path = os.path.join(path, component)
    except Exception as e:
        Print(f"Failed to build folder structure {e}")
