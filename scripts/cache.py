import bz2
import gzip
import hashlib
import os
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

    # TODO: Support https
    base_url = 'http://' + base.url + '/' + base.baseid + '/dists/' + base.codename
    release_url = base_url + '/InRelease'

    # Default release file
    release_file = os.path.join(cache_dir, apt_pkg.uri_to_filename(release_url))

    # By default, download
    if utils.download_file(release_url, release_file) <= 0:
        exit(1)

    try:
        with open(release_file) as fh:
            rel = Release(fh)
    except (FileNotFoundError, PermissionError) as e:
        Print(f"Athena Linux Error: {e}")
        exit(1)

    # sequence is Packages & Sources, you change it you break it
    # TODO: currently, only for main, add for update & security repo too
    cache_source = [base_url + '/main/binary-' + base.arch + '/Packages.gz',
                    base_url + '/main/source/Sources.gz']

    control_files = ['/main/binary-' + base.arch + '/Packages', '/main/source/Sources']

    cache_destination = []
    for uri in cache_source:
        cache_destination.append(os.path.join(cache_dir, apt_pkg.uri_to_filename(uri)))

    md5 = []
    # Extract the md5 for the files, can enable Optional SHA256 also
    try:
        with open(release_file, 'r') as fh:
            rel = Release(fh)
            for _file in control_files:
                _md5 = [line['md5'] for line in rel['MD5Sum'] if line['name'] == _file]
                if _md5 is None:
                    raise Exception(f"File ({_file})not found in release file")
                md5.append(hash)
    except (Exception, FileNotFoundError, PermissionError) as e:
        Print(f"Athena Linux Error: {e}")
        exit(1)

    # Iterate over destination files
    for _file in cache_destination:
        # searching for the decompressed files - stripping extensions
        base = os.path.splitext(_file)[0]
        if os.path.isfile(base):
            # Open the file and calculate the MD5 hash
            with open(base, 'rb') as f:
                fdata = f.read()
                md5_check = hashlib.md5(fdata).hexdigest()
        else:
            md5_check = ''

        index = cache_destination.index(_file)
        if md5[index] != md5_check:
            # download given file to location
            if (utils.download_file(cache_source[index], cache_destination[index])) <= 0:
                exit(1)

            # decompress file based on extension
            base, ext = os.path.splitext(_file)
            if ext == '.gz':
                with gzip.open(_file, 'rb') as f_in:
                    with open(base, 'wb') as f_out:
                        f_out.write(f_in.read())
            elif ext == '.bz2':
                with bz2.BZ2File(_file, 'rb') as f_in:
                    with open(base, 'wb') as f_out:
                        f_out.write(f_in.read())
            else:
                # if no ext leave as such
                # TODO: check if other extensions are required to be supported
                continue

        # List of cache files are in the sequence specified earlier
        cache_files[urlsplit(control_files[index]).path.split('/')[-1]] = base
    Print("Using Release File")
    Print('\tOrigin: {Origin}\n\tCodename: {Codename}\n\tVersion: {Version}\n\tDate: {Date}'.format_map(rel))
    return cache_files
