import bz2
import gzip
import hashlib
import os
from collections import OrderedDict
from urllib.parse import urlsplit

import apt_pkg

import utils
from debian import deb822
from debian.deb822 import Release

# https://github.com/romlok/python-debian/tree/master/examples
# https://www.juliensobczak.com/inspect/2021/05/15/linux-packages-under-the-hood.html

Print = print


class Cache:
    pass


def build_cache(base: utils.BaseDistribution, cache_dir: str) -> dict[str, str]:
    """Builds the Cache. Release file is used based on BaseDistribution defined
        Args:
            base (BaseDistribution): details of the system being derived from
            cache_dir (str): Dir where cache files are to be downloaded

        Returns:
            dict {}:
    """
    cache_files = {}
    compression = '.gz'
    protocol = 'http://'

    # TODO: Support https
    base_url = protocol + base.url + '/' + base.baseid + '/dists/' + base.codename + '/'
    release_url = base_url + 'InRelease'

    # Default release file
    release_file = os.path.join(cache_dir, apt_pkg.uri_to_filename(release_url))

    # By default, download
    if utils.download_file(release_url, release_file) <= 0:
        exit(1)

    # sequence is Packages & Sources, you change it you break it
    # TODO: currently, only for main, add for update & security repo too
    control_files = OrderedDict.fromkeys(['main/binary-' + base.arch + '/Packages', 'main/source/Sources'])

    control_files_compressed: [] = []
    cache_source: [] = []
    cache_destination: [] = []

    for _file in control_files:
        control_files_compressed.append(os.path.basename(apt_pkg.uri_to_filename(base_url + _file)) + compression)
        cache_source.append(base_url + _file + compression)
        cache_destination.append(os.path.join(cache_dir, apt_pkg.uri_to_filename(base_url + _file)))

    # Extract the md5 for the files, can enable Optional SHA256 also
    try:
        with open(release_file, 'r') as fh:
            rel = Release(fh)
            for _file in control_files:
                _md5 = [line['md5sum'] for line in rel['MD5Sum'] if line['name'] == _file]
                assert len(_md5) != 0, f"File ({_file})not found in release file"
                assert len(_md5) == 1, f"Multiple instances for {_file} found in release file"
                control_files[_file] = _md5[0]
    except (Exception, FileNotFoundError, PermissionError) as e:
        Print(f"Athena Linux Error: {e}")
        exit(1)

    _iter_control_file = iter(control_files)
    # Iterate over uncompressed destination files
    for _file in cache_destination:
        # get hash
        md5_check = utils.get_md5(_file)
        index = cache_destination.index(_file)
        control_files_key = next(_iter_control_file)
        _md5 = control_files[control_files_key]
        if _md5 != md5_check:
            # download given file to location
            if (utils.download_file(cache_source[index], cache_destination[index] + compression)) <= 0:
                exit(1)

            # decompress file based on extension
            # base = os.path.splitext(_file)[0]
            # base, ext = os.path.splitext(_file)
            if compression == '.gz':
                with gzip.open(_file + compression, 'rb') as f_in:
                    with open(_file, 'wb') as f_out:
                        f_out.write(f_in.read())
            elif compression == '.bz2':
                with bz2.BZ2File(_file, 'rb') as f_in:
                    with open(_file, 'wb') as f_out:
                        f_out.write(f_in.read())
            elif compression == '':
                # if no ext leave as such
                pass
                # XXX: check if other extensions are required to be supported
            else:
                pass

        # List of cache files are in the sequence specified earlier
        cache_files[urlsplit(control_files_key).path.split('/')[-1]] = _file
    Print("Using Release File")
    Print('\tOrigin: {Origin}\n\tCodename: {Codename}\n\tVersion: {Version}\n\tDate: {Date}'.format_map(rel))
    return cache_files
