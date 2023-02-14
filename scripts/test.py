from debian.deb822 import Deb822, Packages
import apt
from aptsources import sourceslist

# Create an instance of the AptSourcesList class
sources = sourceslist.SourcesList()

filename = '/home/harkirat/Athena-Build/cache/deb.debian.org_debian_dists_bullseye_main_binary-amd64_Packages'

with open(filename, 'r') as fh:
    pkgs = Packages.iter_paragraphs(fh)
    # awks = [pkg for pkg in pkgs if 'Provides' in pkg and 'awk' in pkg['Provides']]

# for pkg in pkgs:
#    print(pkg['Package'])
#    print(pkg.relations['depends'])

package_text = 'Package: gpaw\nVersion: 21.1.0-1\nInstalled-Size: 6178\n' \
               'Maintainer: Debichem Team <debichem-devel@lists.alioth.debian.org>\nArchitecture: amd64\n' \
               'Depends: gpaw-data, openmpi-bin, python3-ase (>= 3.21.0) | python3-scipy, python3-numpy (>= 1:1.16.0~rc1), python3-numpy-abi9, python3 (<< 3.10), python3 (>= 3.9~), python3:any, libc6 (>= 2.14), libelpa15 (>= 2019.11.001), libfftw3-double3 (>= 3.3.5), libopenmpi3 (>= 4.1.0), libscalapack-openmpi2.1 (>= 2.1.0), libxc5 (>= 4.2.1)\n' \
               'Description: DFT and beyond within the projector-augmented wave method\n' \
               'Homepage: https://wiki.fysik.dtu.dk/gpaw/\n' \
               'Description-md5: 299c52e61efe392985b4be165a33dfb5\n' \
               'Section: science\n' \
               'Priority: optional\n' \
               'Filename: pool/main/g/gpaw/gpaw_21.1.0-1_amd64.deb\n' \
               'Size: 1170092\n' \
               'MD5sum: 0c63213b2f5dd9460b220e4f5aa4255a\n' \
               'SHA256: 0d13bf60f79c72902226d965a0c2601931a0c4c47a8d65dd1a33b051a666ee58\n\n'

pkg = Packages(package_text)
print(pkg['Package'])
print(pkg['Provides'])
print(pkg.source)