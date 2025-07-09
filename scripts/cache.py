import bz2
import gzip
import os
import apt_pkg
from collections import OrderedDict
from urllib.parse import urlsplit
from debian.deb822 import Release
from debian.debian_support import DpkgArchTable, Version

# Internal
import utils
import package
import tui

from package import Package, Source
from typing import List, Dict
from collections import defaultdict
from utils import BuildConfig


# https://github.com/romlok/python-debian/tree/master/examples
# https://www.juliensobczak.com/inspect/2021/05/15/linux-packages-under-the-hood.html

from tui import ProgressBar, Spinner

class Cache:

    package_hashtable:  Dict[str, List[Package]]
    provides_hashtable: Dict[str, Dict[Version, List[str]]]
    source_hashtable:   Dict[str, List[Source]]

    
    _arch_table: DpkgArchTable

    class BaseDistribution:
        def __init__(self, url: str, baseid: str, codename: str, version: str, arch: str):
            self.url: str = url
            self.baseid: str = baseid
            self.codename: str = codename
            self.version: str = version
            self.arch: str = arch

    def __init__(self, buildconfig: BuildConfig):
        """Builds the Cache. Release file is used based on BaseDistribution defined
            Args:
                base (BaseDistribution): details of the system being derived from
                cache_dir (str): Dir where cache files are to be downloaded

            Returns:
        """

        self._arch_table = DpkgArchTable.load_arch_table()

        # Set when config is validated
        self._config_valid: bool = False
        self.error_str = ''

        # Base Distribution
        self.cache_dir = buildconfig.dir_cache
        self.base = self.BaseDistribution( url=buildconfig.baseurl, baseid=buildconfig.baseid, 
                                     codename=buildconfig.basecodename, version=buildconfig.baseversion, 
                                     arch=buildconfig.arch)

        # Compression
        self.supported_compression = ['.gz', '.bz2']
        self.compression = '.gz'
        assert self.compression in self.supported_compression, f"Unsupported Compression {self.compression} specified"

        # Protocol
        self.supported_protocol = ['http://', 'https://']
        self.protocol = 'http://'
        assert self.protocol in self.supported_protocol, f"Unsupported Protocol {self.protocol} specified"

        # Control files
        # TODO: currently, only for main, add for update & security repo too
        self.control_files = OrderedDict.fromkeys(
            ['main/binary-' + self.base.arch + '/Packages', 'main/source/Sources']
        )

        # Outputs file list
        self.cache_files: Dict[str, str] = {}

        # InRelease info
        self.release_info = ''

        # Cache data
        self.__package_file = ''
        self.__source_file = ''
        self.__package_records = []
        self.__source_records = []
        self.pkg_list = []
        self.src_list = []
        
        self.required: List[str] = []
        self.important: List[str] = []

        
        self.package_hashtable = defaultdict(list)  # Dict[str, List[Package]]
        self.provides_hashtable = defaultdict(lambda: defaultdict(list))
        self.source_hashtable = defaultdict(list) # Dict[str, List[Source]]

        # Download files
        if self.__get_files() < 0:
            return

        # Build Hashtable
        self.__build_cache(buildconfig.arch)

        # Set when config is validated
        self._config_valid: bool = True

    def __get_files(self) -> int:

        __base_url = self.protocol + self.base.url + '/' + self.base.baseid + '/dists/' + self.base.codename + '/'

        # Defaults for release file
        __release_url = __base_url + 'InRelease'
        __release_file = os.path.join(self.cache_dir, apt_pkg.uri_to_filename(__release_url))

        # Setup files - Sequence is Packages & Sources, you change it you break it
        __cache_source: List[str] = []
        __cache_destination: List[str] = []
        
        for _file in self.control_files:
            __cache_source.append(__base_url + _file + self.compression)
            __cache_destination.append(os.path.join(self.cache_dir, apt_pkg.uri_to_filename(__base_url + _file)))

        # By default, download release file
        if utils.download_file(__release_url, __release_file) <= 0:
            self.error_str = f"Error downloading release file from {__release_url}"
            return -1

        # Extract the md5 for the files, can enable Optional SHA256 also
        try:
            with open(__release_file, 'r') as fh:
                rel = Release(fh)
                for _file in self.control_files:
                    # Check if file is present in release file
                    _md5 = [line['md5sum'] for line in rel['MD5Sum'] if line['name'] == _file]
                    if len(_md5) == 0:
                        self.error_str = f"File ({_file}) not found in release file"
                        return -1
                    
                    # If multiple instances found, raise error
                    if len(_md5) > 1:
                        self.error_str = f"Multiple instances for {_file} found in release file"
                        return -1

                    self.control_files[_file] = _md5[0]

        except (Exception, FileNotFoundError, PermissionError) as e:
            tui.console.print(f"Athena Linux Error: {e}")
            return -1

        _iter_control_file = iter(self.control_files)

        # Iterate over uncompressed destination files
        for _file in __cache_destination:
            
            # get hash
            md5_check = utils.get_md5(_file)
            index = __cache_destination.index(_file)
            control_files_key = next(_iter_control_file)
            _md5 = self.control_files[control_files_key]
            
            if _md5 != md5_check:
                # download given file to location
                if (utils.download_file(__cache_source[index], __cache_destination[index] + self.compression)) <= 0:
                    self.error_str = f"Error downloading file {__cache_source[index]}"
                    return -1

                # decompress file based on extension
                if self.compression == '.gz':
                    with gzip.open(_file + self.compression, 'rb') as f_in:
                        with open(_file, 'wb') as f_out:
                            f_out.write(f_in.read())
                
                elif self.compression == '.bz2':
                    with bz2.BZ2File(_file, 'rb') as f_in:
                        with open(_file, 'wb') as f_out:
                            f_out.write(f_in.read())

                elif self.compression == '':
                    # if no ext leave as such
                    pass
                    # XXX: check if other extensions are required to be supported

                else:
                    pass

            # List of cache files are in the sequence specified earlier
            self.cache_files[urlsplit(control_files_key).path.split('/')[-1]] = _file

        tui.console.print("Using Release File")
        tui.console.print('\tOrigin: {Origin}\n\tCodename: {Codename}\n\tVersion: {Version}\n\tDate: {Date}'.format_map(rel))

        return 0

    def __build_cache(self, arch: str) -> bool:
        """Builds the cache from the control files downloaded"""

        if 'Packages' not in self.cache_files:
            self.error_str = "Missing Packages control file from cache"
            return False

        if 'Sources' not in self.cache_files:
            self.error_str = "Missing Sources control file from cache"
            return False
        
        if self.cache_files['Packages'] == '':
            self.error_str = "Missing Packages control file from cache"
            return False
        
        if self.cache_files['Sources'] == '':
            self.error_str = "Missing Sources control file from cache"
            return False
        

        self.__package_file = self.cache_files['Packages']
        self.__source_file = self.cache_files['Sources']

        # load data from the files
        self.__package_records = utils.readfile(self.__package_file).split('\n\n')
        self.__source_records = utils.readfile(self.__source_file).split('\n\n')

        # create a list, since we can have duplicates
        parser_spinner = Spinner("Parsing Package Files")
        
        progress_bar_pkg = ProgressBar(label = f"{'Indexing Package File'}", itr_label = 'rec/s', maxvalue = len(self.__package_records))
        for _pkg_record in self.__package_records:
            
            progress_bar_pkg.step(1)
            
            _pkg_record = _pkg_record.strip() 
            
            if not _pkg_record:
                continue

            _pkg = package.Package(_pkg_record)

            if not _pkg.isvalid: 
                continue

            # add Package in hashtable
            _package_name = _pkg.package

            # Check if the package architecture matches the current architecture
            if self._arch_table.matches_architecture(_pkg.arch, arch) is False:
                continue
            
            # Package associated with 'Package' name, 
            # Mode than one Package could be associated by same name, e.g. different version
            self.package_hashtable[_package_name].append(_pkg)

            # Which Package provides 'package' name
            # get_provides() returns a list of tupple(name, version)
            # e.g. [('acorn', '8.0.5+ds+~cs19.19.27-3'), ('node-acorn', '8.0.5+ds+~cs19.19.27-3'), ('node-acorn-bigint','1.0.0')]
            # there can be more than one version in provided by for same package name.
            
            for _provided in _pkg.get_provides():
                _provided_name = _provided[0]
                _provided_ver = _provided[1]
                # provides_hashtable: Dict[str, Dict[Version, List[str]]]
                self.provides_hashtable[_provided_name][_provided_ver].append(_pkg.package)

            # build the required(s) list
            if _pkg.priority == 'required':
                self.required.append(_package_name)

            # Build the 'important' list
            if _pkg.priority == 'important':
                self.important.append(_package_name)
                
        progress_bar_pkg.close()
   
        progress_bar_src = ProgressBar(label = f"{'Indexing Source File'}", itr_label = 'rec/s', maxvalue = len(self.__source_records))
        for _src_record in self.__source_records:
            progress_bar_src.step(1)
            
            if _src_record.strip() == '':
                continue
            _pkg = package.Source(_src_record)
            
            if not _pkg.isvalid:
                continue

            # add Package in hashtable
            _package_name = _pkg.package

            _arch_match: bool = False            
            for _pkt_arch in _pkg.arch:
                # Check if the package architecture matches the current architecture
                if self._arch_table.matches_architecture(_pkt_arch, arch):
                    _arch_match = True
            
            if not _arch_match:
                continue
            
            self.source_hashtable[_package_name].append(_pkg) 

        progress_bar_src.close()
        parser_spinner.done()
        
        # Special case - if gcc-10 already selected, e.g. both gcc-9-base & gcc-10-base are marked required
        gcc_versions = [pkg for pkg in self.required if pkg.startswith('gcc-')]
        latest_gcc_versions = sorted(gcc_versions, key=lambda x: tuple(int(num) for num in x.split('-')[1].split('.')))[-1:]
        latest_gcc = set(latest_gcc_versions)
        self.required = [pkg for pkg in self.required if not pkg.startswith('gcc-') or pkg in latest_gcc]

        tui.console.print(f"Required Package Count : {len(self.required)}")
        tui.console.print(f"Important Package Count : {len(self.important)}")
        
        return True

    def get_packages(self, package_name: str) -> List[Package]:
        return self.package_hashtable[package_name]

    def get_provides(self, provides_name: str) -> Dict[Version, List[str]]:
        return self.provides_hashtable[provides_name]
