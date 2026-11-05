"""Packs a folder into a .tar.gz with Unix file modes, for the Linux builds (scripts/make-unix.js).

Usage: python tarball.py FOLDER OUT.tar.gz

Built on Windows, files have no execute bit to keep, so it is decided here: programs (ELF files)
and scripts (#!) are 0755, everything else 0644. Everything is owned by root, as release archives are.
"""

import os
import sys
import tarfile


def main() -> None:
    src, out = os.path.abspath(sys.argv[1]), sys.argv[2]

    def modes(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        if info.isdir():
            info.mode = 0o755
        elif info.isfile():
            with open(os.path.join(os.path.dirname(src), info.name), "rb") as fh:
                head = fh.read(4)
            info.mode = 0o755 if head == b"\x7fELF" or head[:2] == b"#!" else 0o644
        return info

    with tarfile.open(out, "w:gz", compresslevel=6) as tar:
        tar.add(src, arcname=os.path.basename(src), filter=modes)


if __name__ == "__main__":
    main()
