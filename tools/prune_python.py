"""Обрезка python-build-standalone для колонки: то, что Питону на колонке не нужно (tk, тесты, idle, заголовки).

    python -I tools/prune_python.py py.tar.gz py_pruned.tar.gz

Тем же правилом (:data:`DROP`) режет :mod:`build_runtime`.
"""
import re
import sys
import tarfile

DROP = re.compile(r"^python/(lib/libpython|lib/libtcl|lib/libtk|lib/tcl|lib/tk|lib/itcl|lib/thread|share/|include/|"
                  r"lib/pkgconfig|lib/python3\.12/(site-packages/|ensurepip|idlelib|tkinter|turtledemo|test/|lib2to3|"
                  r"pydoc_data|config-3|__phello__|lib-dynload/_tkinter|lib-dynload/_test|lib-dynload/xxlimited|"
                  r"lib-dynload/_ctypes_test)|.*/__pycache__/)")


def main(src: str, dst: str) -> None:
    kept = dropped = 0
    with tarfile.open(src) as t, tarfile.open(dst, "w:gz", compresslevel=6) as o:
        for m in t:
            if DROP.match(m.name):
                dropped += m.size
                continue
            kept += m.size
            o.addfile(m, t.extractfile(m) if m.isfile() else None)
    print(f"оставлено {kept/2**20:.1f} МБ, выкинуто {dropped/2**20:.1f} МБ")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
