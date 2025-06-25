import hashlib
import os
import pathlib
import re
import configparser
import argparse

from configparser import ConfigParser

global Print, Prompt, Spinner, ProgressBar, Exit

class BaseDistribution:
    def __init__(self, url: str, baseid: str, codename: str, version: str, arch: str):
        self.url: str = url
        self.baseid: str = baseid
        self.codename: str = codename
        self.version: str = version
        self.arch: str = arch

class BuildConfig:
    arch: str
    baseurl: str
    basecodename: str
    baseid: str
    baseversion: str
    build_codename: str
    build_version: str

    skip_build_test: list[str]

    working_dir: str
    config_path: str
    pkglist_path: str

    error_str: str

    def __init__(self, config_parser: ConfigParser):
        
        # Set when config is validated
        _config_valid: bool = False
        self.error_str = ''

        # let defaults be relative to current working directory
        try:
            self.working_dir = os.path.abspath(os.path.curdir)
            # self.cwd = os.path.abspath(self.working_dir)
            self.config_path = os.path.join(self.working_dir, 'config/build.conf')
            self.pkglist_path = os.path.join(self.working_dir, 'config/pkg.list')
        except os.error as e:
            self.error_str = str(e)
            return

        try:
            parser = argparse.ArgumentParser(description='Dependency Parser - Athena Linux')
            parser.add_argument('--working-dir', type=str, help='Specify Working directory', required=True, default=self.working_dir)
            parser.add_argument('--config-file', type=str, help='Specify Configs File', required=True, default=self.config_path)
            parser.add_argument('--pkg-list', type=str, help='Specify Required Pkg File', required=True, default=self.pkglist_path)
            args = parser.parse_args()
        except argparse.ArgumentError as e:
            self.error_str = str(e)
            return

        # if dirs specified, they are not relative
        try:
            self.working_dir = os.path.abspath(args.working_dir)
            self.config_path = os.path.abspath(args.config_file)
            self.pkglist_path = os.path.abspath(args.pkg_list)
        except os.error as e:
            self.error_str = str(e)
            return

        try:
            config_parser.read(self.config_path)
            self.arch = config_parser.get('Build', 'ARCH')
            self.baseurl = config_parser.get('Base', 'baseurl')
            self.basecodename = config_parser.get('Base', 'BASECODENAME')
            self.baseid = config_parser.get('Base', 'BASEID')
            self.baseversion = config_parser.get('Base', 'BASEVERSION')
            self.build_codename = config_parser.get('Build', 'CODENAME')
            self.build_version = config_parser.get('Build', 'VERSION')

            self.skip_build_test = config_parser.get('Source', 'SkipTest').split(', ')

            self.dir_download = os.path.join(self.working_dir, config_parser.get('Directories', 'Download'))
            self.dir_log = os.path.join(self.working_dir, config_parser.get('Directories', 'Log'))
            self.dir_cache = os.path.join(self.working_dir, config_parser.get('Directories', 'Cache'))
            self.dir_temp = os.path.join(self.working_dir, config_parser.get('Directories', 'Temp'))
            self.dir_source = os.path.join(self.working_dir, config_parser.get('Directories', 'Source'))
            self.dir_repo = os.path.join(self.working_dir, config_parser.get('Directories', 'Repo'))
            self.dir_config = os.path.join(self.working_dir, config_parser.get('Directories', 'Config'))
            self.dir_patch = os.path.join(self.working_dir, config_parser.get('Directories', 'Patch'))
            self.dir_image = os.path.join(self.working_dir, config_parser.get('Directories', 'Image'))
            self.dir_chroot = os.path.join(self.working_dir, config_parser.get('Directories', 'Chroot'))
            
            self.dir_patch_source = os.path.join(self.dir_patch, 'source')
            self.dir_patch_preinstall = os.path.join(self.dir_patch, 'pre-install')
            self.dir_patch_postinstall = os.path.join(self.dir_patch, 'post-install')
            self.dir_patch_empty = os.path.join(self.dir_patch, 'empty')


        except (configparser.Error, OSError) as e:
            self.error_str = str(e)
            return
        
        try:
            os.access(self.cwd, os.W_OK)


            pathlib.Path(self.dir_download).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_log).mkdir(parents=True, exist_ok=True)
            pathlib.Path(os.path.join(self.dir_log, 'build')).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_cache).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_temp).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_repo).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_patch).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_empty).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_preinstall).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_postinstall).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_image).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot).mkdir(parents=True, exist_ok=True)

        except PermissionError as e:
            Print(f"Athena Linux: Insufficient permissions in the working directory: {e}")
            exit(1)
        
        _config_valid: bool = True
    

class DirectoryListing:
    """
    DirectoryListing - Enables functional directory structure
    Attributes:
        dir_download(str):  Location for file downloads
        dir_log(str):       Location of log files
        dir_cache(str):
        dir_temp(str):
        dir_repo(str):      Location for build packages for installation
        dir_config(str):    Location for all config files
        dir_patch(str):     Location of where config patches are kept, refer to build for subdirectory structure
        dir_image(str):     Location of the output image/installation files
        dir_chroot(str):    Location where the image is built
        dir_patch_source(str): Location of where sourcecode patches are kept, refer to build for subdirectory structure
        dir_patch_preinstall(str):  Patches before building chroot
        dir_patch_postinstall(str): Patches after building chroot
        dir_patch_empty(str):

    """

    def __init__(self, working_dir: str, config_parser: configparser):

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

            self.dir_patch_source = os.path.join(self.dir_patch, 'source')
            self.dir_patch_preinstall = os.path.join(self.dir_patch, 'pre-install')
            self.dir_patch_postinstall = os.path.join(self.dir_patch, 'post-install')
            self.dir_patch_empty = os.path.join(self.dir_patch, 'empty')

        except configparser.Error as e:
            Print(f"Athena Linux: Config Parser Error: {e}")
            exit(1)

        try:
            os.access(self.cwd, os.W_OK)
            pathlib.Path(self.dir_download).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_log).mkdir(parents=True, exist_ok=True)
            pathlib.Path(os.path.join(self.dir_log, 'build')).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_cache).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_temp).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_repo).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_patch).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_empty).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_preinstall).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_postinstall).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_image).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot).mkdir(parents=True, exist_ok=True)

        except PermissionError as e:
            Print(f"Athena Linux: Insufficient permissions in the working directory: {e}")
            exit(1)





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


class Node:
    def __init__(self, value):
        self.value = value
        self.children = []

    def add_child(self, child):
        self.children.append(child)

    def remove_child(self, child):
        self.children.remove(child)


class Tree:
    def __init__(self):
        self.root = None

    def add_node(self, value, parent_value=None):
        node = Node(value)
        if parent_value is None:
            if self.root is None:
                self.root = node
            else:
                raise ValueError("Cannot add root node as it already exists")
        else:
            parent_node = self.find_node(parent_value)
            if parent_node is None:
                raise ValueError("Parent node does not exist")
            parent_node.add_child(node)
        return node

    def delete_node(self, value):
        node = self.find_node(value)
        if node is None:
            raise ValueError("Node does not exist")
        parent = self.find_parent_node(value)
        if parent is not None:
            parent.remove_child(node)
        else:
            self.root = None

    def find_node(self, value):
        return self._find_node_helper(self.root, value)

    def _find_node_helper(self, node, value):
        if node is None:
            return None
        if node.value == value:
            return node
        for child in node.children:
            result = self._find_node_helper(child, value)
            if result is not None:
                return result
        return None

    def find_parent_node(self, value):
        return self._find_parent_node_helper(None, self.root, value)

    def _find_parent_node_helper(self, parent, node, value):
        if node is None:
            return None
        if node.value == value:
            return parent
        for child in node.children:
            result = self._find_parent_node_helper(node, child, value)
            if result is not None:
                return result
        return None

    def size(self) -> int:
        return len(self.root.children)

    @property
    def is_childless(self) -> bool:
        if not self.root:
            return True
        return len(self.root.children) == 0

    @property
    def is_empty(self) -> bool:
        return self.root is None

