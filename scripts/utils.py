import hashlib
import os
import pathlib
import re
import configparser
import argparse

import tui
from tui import Prompt, Spinner, ProgressBar
from typing import List, Optional, Any


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

    error_str: str
    config_path: str

    dir_working: str
    dir_pkglist: str
    dir_download: str
    dir_log: str
    dir_cache: str
    dir_temp: str
    dir_source: str
    dir_repo: str
    dir_config: str
    dir_patch: str
    dir_image: str
    dir_chroot: str
    
    dir_patch_source: str
    dir_patch_preinstall: str
    dir_patch_postinstall: str
    dir_patch_empty: str

    _config_valid: bool
    
    def __init__(self):

        # Set when config is validated
        self._config_valid: bool = False

        # Setting up config parsers
        config_parser = configparser.ConfigParser()
        
        self.error_str = ''

        try:
            # let defaults be relative to current working directory
            working_dir = os.path.abspath(os.path.curdir)
            config_path = os.path.join(working_dir, 'config/build.conf')
            pkglist_path = os.path.join(working_dir, 'config/pkg.list')

            parser = argparse.ArgumentParser(description='Dependency Parser - Athena Linux')
            parser.add_argument('--working-dir', type=str, help='Specify Working directory', required=False, default=working_dir)
            parser.add_argument('--config-file', type=str, help='Specify Configs File', required=False, default=config_path)
            parser.add_argument('--pkg-list', type=str, help='Specify Required Pkg File', required=False, default=pkglist_path)
            args = parser.parse_args()

            # if paths are specified, they are absolute
            self.working_dir = os.path.abspath(args.working_dir)
            self.config_path = os.path.abspath(args.config_file)
            self.pkglist_path = os.path.abspath(args.pkg_list)

            # Check if the working directory and config files are writable   
            os.access(self.config_path, os.R_OK)

        except (argparse.ArgumentError, OSError) as e:
            self.error_str = str(e)
            return

        # read config file
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

            # NOTE: The directories are relative to the working directory
            self.dir_download = os.path.join(self.working_dir, config_parser.get('Directories', 'Download'))
            self.dir_log = os.path.join(self.working_dir, config_parser.get('Directories', 'Log'))
            self.dir_cache = os.path.join(self.working_dir, config_parser.get('Directories', 'Cache'))
            self.dir_temp = os.path.join(self.working_dir, config_parser.get('Directories', 'Temp'))
            self.dir_source = os.path.join(self.working_dir, config_parser.get('Directories', 'Source'))
            self.dir_repo = os.path.join(self.working_dir, config_parser.get('Directories', 'Repo'))
            self.dir_config = os.path.join(self.working_dir, config_parser.get('Directories', 'Config'))
            self.dir_image = os.path.join(self.working_dir, config_parser.get('Directories', 'Image'))
            self.dir_chroot = os.path.join(self.working_dir, config_parser.get('Directories', 'Chroot'))
            
            self.dir_patch = os.path.join(self.working_dir, config_parser.get('Directories', 'Patch'))
            self.dir_patch_source = os.path.join(self.dir_patch, 'source')
            self.dir_patch_preinstall = os.path.join(self.dir_patch, 'pre-install')
            self.dir_patch_postinstall = os.path.join(self.dir_patch, 'post-install')
            self.dir_patch_empty = os.path.join(self.dir_patch, 'empty')

        except (configparser.Error, OSError) as e:
            self.error_str = str(e)
            return
        
        try:
            os.access(self.working_dir, os.W_OK)

            pathlib.Path(self.dir_download).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_log).mkdir(parents=True, exist_ok=True)
            
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

            os.access(self.dir_download, os.W_OK)
            os.access(self.dir_log, os.W_OK)
            os.access(self.dir_cache, os.W_OK)
            os.access(self.dir_temp, os.W_OK)
            os.access(self.dir_source, os.W_OK)
            os.access(self.dir_repo, os.W_OK)
            os.access(self.dir_patch, os.W_OK)
            os.access(self.dir_patch_empty, os.W_OK)
            os.access(self.dir_patch_source, os.W_OK)
            os.access(self.dir_patch_preinstall, os.W_OK)
            os.access(self.dir_patch_postinstall, os.W_OK)
            os.access(self.dir_image, os.W_OK)
            os.access(self.dir_chroot, os.W_OK)

            pathlib.Path(os.path.join(self.dir_log, 'build')).mkdir(parents=True, exist_ok=True)

        except PermissionError as e:
            self.error_str = str(e)
            return
        
        self._config_valid = True
    
    @property
    def is_valid(self) -> bool:
        """
        Returns:
            bool: True if config is valid, False otherwise
        """
        return self._config_valid
    
    def error(self) -> str:
        """
        Returns:
            str: Error string if config is invalid, empty string otherwise
        """
        return self.error_str
    
def download_file(url: str, filename: str) -> int:
    """Downloads file and updates progressbar in incremental manner.
        Args:
            url (str): url to download file from, protocol is prepended
            filename (str): Filename to save a review of your code and especially the constraint_action lookup table logic:to, location should be writable

        Returns:
            int: -1 for failure, file_size on success
    """
    import requests
    from urllib.parse import urlsplit
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException

    name_strip: str = urlsplit(url).path.split('/')[-1].ljust(15, ' ')
    
    try:
        response = requests.head(url)
        file_size = int(response.headers.get('content-length', 0))
        
        with requests.get(url, stream=True, timeout=10) as response:
            response.raise_for_status()

            progress_bar = tui.ProgressBar(label=name_strip, itr_label='B/s', maxvalue=file_size)

            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        progress_bar.step(len(chunk))

            progress_bar.close()
            return file_size

    except (ConnectionError, Timeout, TooManyRedirects, HTTPError, RequestException) as e:
        tui.console.print(f"Error connecting to {url}: {e}")
        return -1
    

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
                tui.console.print(f"Error connecting to {_url}: {e}")
                continue

            assert get_md5(_download_path) == _md5, f"Downloaded {_file} hash mismatch"

        else:
            _skipped += 1
            progress_bar.update(int(_file_list[_file]['size']))
            _downloaded_size += int(_file_list[_file]['size'])

        _index += 1

    progress_bar.clear()
    progress_bar.close()

    tui.console.print(f"Downloading {_total - _skipped} files, Skipped {_skipped} files")
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
        tui.console.print(f"Error: {e}")
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
        tui.console.print(f"Failed to build folder structure {e}")


class Node:
    def __init__(self, value: Any):
        self.value = value
        self.children: List['Node'] = []

    def add_child(self, child: 'Node') -> None:
        """Add a child node, avoiding duplicates"""
        if child not in self.children:
            self.children.append(child)

    def remove_child(self, child: 'Node') -> bool:
        """Remove a child node, return True if removed, False if not found"""
        try:
            self.children.remove(child)
            return True
        except ValueError:
            return False
    
    def has_child(self, child: 'Node') -> bool:
        """Check if node has a specific child"""
        return child in self.children
    
    def get_child_by_value(self, value: Any) -> Optional['Node']:
        """Find child by value"""
        for child in self.children:
            if child.value == value:
                return child
        return None
    
    def is_leaf(self) -> bool:
        """Check if node has no children"""
        return len(self.children) == 0
    
    def __repr__(self) -> str:
        return f"Node(value={self.value}, children={len(self.children)})"
    
    def __eq__(self, other: Any) -> bool:
        """Enable equality comparison"""
        if not isinstance(other, Node):
            return False
        return self.value == other.value
    
    def __hash__(self) -> int:
        """Enable hashing for set operations"""
        return hash(self.value)

from typing import Optional, Any, List

class Tree:
    def __init__(self):
        self.root: Optional[Node] = None

    def add_node(self, value: Any, parent_value: Optional[Any] = None) -> Node:
        """Add a node to the tree"""
        # Check if node with this value already exists
        if self.find_node(value) is not None:
            raise ValueError(f"Node with value '{value}' already exists")
            
        node = Node(value)
        
        if parent_value is None:
            if self.root is None:
                self.root = node
            else:
                raise ValueError("Cannot add root node as it already exists")
        else:
            parent_node = self.find_node(parent_value)
            if parent_node is None:
                raise ValueError(f"Parent node with value '{parent_value}' does not exist")
            parent_node.add_child(node)
            
        return node

    def delete_node(self, value: Any) -> bool:
        """Delete a node and handle its children"""
        node = self.find_node(value)
        if node is None:
            return False
            
        parent = self.find_parent_node(value)
        
        if parent is not None:
            # Remove from parent
            parent.remove_child(node)
            # Move children to parent (or could delete them - depends on use case)
            for child in node.children:
                parent.add_child(child)
        else:
            # Deleting root node
            if len(node.children) == 0:
                self.root = None
            elif len(node.children) == 1:
                self.root = node.children[0]
            else:
                raise ValueError("Cannot delete root node with multiple children")
        
        return True

    def find_node(self, value: Any) -> Optional[Node]:
        """Find a node by value"""
        return self._find_node_helper(self.root, value)

    def _find_node_helper(self, node: Optional[Node], value: Any) -> Optional[Node]:
        """Recursive helper for finding nodes"""
        if node is None:
            return None
        if node.value == value:
            return node
        for child in node.children:
            result = self._find_node_helper(child, value)
            if result is not None:
                return result
        return None

    def find_parent_node(self, value: Any) -> Optional[Node]:
        """Find the parent of a node with given value"""
        return self._find_parent_node_helper(None, self.root, value)

    def _find_parent_node_helper(self, parent: Optional[Node], node: Optional[Node], value: Any) -> Optional[Node]:
        """Recursive helper for finding parent nodes"""
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
        """Return total number of nodes in the tree"""
        return self._count_nodes(self.root)
    
    def _count_nodes(self, node: Optional[Node]) -> int:
        """Recursively count all nodes"""
        if node is None:
            return 0
        count = 1  # Count current node
        for child in node.children:
            count += self._count_nodes(child)
        return count

    def depth(self) -> int:
        """Return the maximum depth of the tree"""
        return self._calculate_depth(self.root)
    
    def _calculate_depth(self, node: Optional[Node]) -> int:
        """Recursively calculate tree depth"""
        if node is None:
            return 0
        if not node.children:
            return 1
        return 1 + max(self._calculate_depth(child) for child in node.children)

    def get_leaves(self) -> List[Node]:
        """Return all leaf nodes"""
        leaves = []
        self._collect_leaves(self.root, leaves)
        return leaves
    
    def _collect_leaves(self, node: Optional[Node], leaves: List[Node]) -> None:
        """Recursively collect leaf nodes"""
        if node is None:
            return
        if node.is_leaf():
            leaves.append(node)
        else:
            for child in node.children:
                self._collect_leaves(child, leaves)

    def get_path_to_node(self, value: Any) -> Optional[List[Any]]:
        """Get path from root to node with given value"""
        path = []
        if self._find_path_helper(self.root, value, path):
            return path
        return None
    
    def _find_path_helper(self, node: Optional[Node], value: Any, path: List[Any]) -> bool:
        """Recursive helper for finding path to node"""
        if node is None:
            return False
        
        path.append(node.value)
        
        if node.value == value:
            return True
        
        for child in node.children:
            if self._find_path_helper(child, value, path):
                return True
        
        path.pop()  # Backtrack
        return False

    @property
    def is_childless(self) -> bool:
        """Check if root has no children"""
        if not self.root:
            return True
        return len(self.root.children) == 0

    @property
    def is_empty(self) -> bool:
        """Check if tree is empty"""
        return self.root is None
    
    def __repr__(self) -> str:
        return f"Tree(size={self.size()}, depth={self.depth()}, empty={self.is_empty})"
    
    def print_tree(self, node: Optional[Node] = None, indent: str = "") -> None:
        """Print tree structure"""
        if node is None:
            node = self.root
        if node is None:
            print("Empty tree")
            return
            
        print(f"{indent}{node.value}")
        for child in node.children:
            self.print_tree(child, indent + "  ")