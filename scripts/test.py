from debian.deb822 import Deb822, Packages

filename = '/home/harkirat/Athena-Build/cache/deb.debian.org_debian_dists_bullseye_main_binary-amd64_Packages'

with open(filename, 'r') as fh:
    pkgs = Packages.iter_paragraphs(fh)
    awks = [pkg for pkg in pkgs if 'Provides' in pkg and 'awk' in pkg['Provides']]

for pkg in awks:
    print(pkg[relationship])
