import bz2
import gzip
import os
import apt_pkg
from collections import OrderedDict
from urllib.parse import urlsplit
from debian.deb822 import Release

# Internal
import utils
import package

# https://github.com/romlok/python-debian/tree/master/examples
# https://www.juliensobczak.com/inspect/2021/05/15/linux-packages-under-the-hood.html

Print = print


class Cache:

    def __init__(self, base: utils.BaseDistribution, cache_dir: str):
        """Builds the Cache. Release file is used based on BaseDistribution defined
            Args:
                base (BaseDistribution): details of the system being derived from
                cache_dir (str): Dir where cache files are to be downloaded

            Returns:
        """

        self.cache_dir = cache_dir
        self.base: utils.BaseDistribution = base

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
        self.cache_files = {}

        # InRelease info
        self.release_info = ''

        # Cache data
        self.__package_file = ''
        self.__source_file = ''
        self.__package_record = []
        self.__source_records = []
        self.pkg_list = []
        self.src_list = []
        self.package_hashtable = {}
        self.provides_hashtable = {}
        self.source_hashtable = {}

        # Download files
        self.__get_files()

        # Build Hashtable
        self.__build_cache()

    def __get_files(self):

        __base_url = self.protocol + self.base.url + '/' + self.base.baseid + '/dists/' + self.base.codename + '/'

        # Defaults for release file
        __release_url = __base_url + 'InRelease'
        __release_file = os.path.join(self.cache_dir, apt_pkg.uri_to_filename(__release_url))

        # Setup files - Sequence is Packages & Sources, you change it you break it
        __cache_source: [] = []
        __cache_destination: [] = []
        for _file in self.control_files:
            __cache_source.append(__base_url + _file + self.compression)
            __cache_destination.append(os.path.join(self.cache_dir, apt_pkg.uri_to_filename(__base_url + _file)))

        # By default, download release file
        if utils.download_file(__release_url, __release_file) <= 0:
            exit(1)

        # Extract the md5 for the files, can enable Optional SHA256 also
        try:
            with open(__release_file, 'r') as fh:
                rel = Release(fh)
                for _file in self.control_files:
                    _md5 = [line['md5sum'] for line in rel['MD5Sum'] if line['name'] == _file]
                    assert len(_md5) != 0, f"File ({_file})not found in release file"
                    assert len(_md5) == 1, f"Multiple instances for {_file} found in release file"
                    self.control_files[_file] = _md5[0]
        except (Exception, FileNotFoundError, PermissionError) as e:
            Print(f"Athena Linux Error: {e}")
            exit(1)

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
                    exit(1)

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
        Print("Using Release File")
        Print('\tOrigin: {Origin}\n\tCodename: {Codename}\n\tVersion: {Version}\n\tDate: {Date}'.format_map(rel))

    def __build_cache(self):
        assert 'Packages' in self.cache_files, "Missing Packages control file from cache"
        assert 'Sources' in self.cache_files, "Missing Sources control file from cache"
        assert self.cache_files['Packages'] != '', "Missing Packages control file from cache"
        assert self.cache_files['Sources'] != '', "Missing Sources control file from cache"

        self.__package_file = self.cache_files['Packages']
        self.__source_file = self.cache_files['Sources']

        # load data from the files
        self.__package_records = utils.readfile(self.__package_file).split('\n\n')
        self.__source_records = utils.readfile(self.__source_file).split('\n\n')

        # create a list, since we can have duplicates
        for _pkg_record in self.__package_records:
            if _pkg_record.strip() == '':
                continue
            __pkg = package.Package(_pkg_record, self.base.arch)

            # add Package in hashtable
            _package_name = __pkg['Package']
            if _package_name in self.package_hashtable:
                self.package_hashtable[_package_name].append(__pkg)
            else:
                self.package_hashtable[_package_name] = [__pkg]

            # add Provides to hashtable
            for __provides in __pkg.get_provides():
                if __provides in self.provides_hashtable:
                    if __pkg not in self.provides_hashtable[__provides]:
                        self.provides_hashtable[__provides].append(__pkg)
                else:
                    self.provides_hashtable[__provides] = [__pkg]

    def get_packages(self, package_name: str) -> []:
        if package_name not in self.package_hashtable:
            return []
        return self.package_hashtable[package_name]

    def get_provides(self, provides_name: str) -> []:
        if provides_name not in self.provides_hashtable:
            return []
        return self.package_hashtable[provides_name]
