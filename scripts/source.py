# Internal
import deb822

# External
import os
import apt_pkg


class Source(deb822.DEB822file):

    def __init__(self, section: str, arch: str):

        self.package = ''
        self.version = ''

        self._build_depends = []
        self._build_conflicts = []

        super().__init__(section)

        # Setting Values post calling super()
        if arch not in ['amd64']:
            raise ValueError(f"Current Architecture '{arch}' is not supported")
        self.arch: str = arch

        assert 'Package' in self, "Malformed Package, No Package Name"
        assert 'Version' in self, "Malformed Package, No Version Given"
        assert 'Files' in self, "Malformed Package, No Version Given"
        assert 'Directory' in self, "Malformed Package, No Version Given"

        assert not self['Package'] == '', "Malformed Package, No Package Name"
        assert not self['Version'] == '', "Malformed Package, No Version Given"
        assert not self['Files'] == '', "Malformed Package, No Files Given"
        assert not self['Directory'] == '', "Malformed Package, No Directory Given"

        self.package = self['Package']
        self.version = self['Version']
        self.directory = self['Directory']
        self.pkgs: [] = []
        self.files: {} = {}
        self.skip_test = False
        self.patch_list = []

        # if self.package == 'glibc':
        #    print('.')

        _depends_list = []
        _dep_string = ['Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch']
        for _dep in _dep_string:
            self._build_depends += apt_pkg.parse_src_depends(self[_dep], strip_multi_arch=True, architecture=self.arch)

        _files_list = self['Files'].split('\n')
        for _file in _files_list:
            _file = _file.split()
            if len(_file) == 3:
                self.files[_file[2]] = {'path': os.path.join(self.directory, _file[2]),
                                        'size': _file[1], 'md5': _file[0]}

        # can be derived from Package-List field, but it is tedious - correlation for versions required
        # One source provides multiple packages, package may have different version from the source version
        # Package-List may have additional information e.g. 'udeb' tag which is not there in package
        # Lets only select the package-files that the Package actually needs, the others produced are optional


    @property
    def download_size(self) -> int:
        _download_size = 0
        for _file in self.files:
            _download_size += int(self.files[_file]['size'])
        return _download_size

    @property
    def build_depends(self) -> str:
        _dep_str = ''
        for _dep in self._build_depends:
            # by default select first package even for multi/alt dependencies
            _dep_str += _dep[0][0] + ' '

        return _dep_str
