import sys, tarfile, zipfile, io, os, glob
sp, files_list, repo, out = sys.argv[1:5]
def add(t, name, data):
    ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644; t.addfile(ti, io.BytesIO(data))
n = 0
with tarfile.open(out, "w:gz") as t:
    for w in glob.glob(os.path.join(sp, "dl_whl", "*.whl")):
        with zipfile.ZipFile(w) as z:
            for i in z.infolist():
                if not i.is_dir(): add(t, "site/" + i.filename, z.read(i)); n += 1
    with tarfile.open(glob.glob(os.path.join(sp, "dl_whl", "sdist", "pyyaml-*.tar.gz"))[0]) as s:
        for m in s:
            p = m.name.split("/", 1)[1] if "/" in m.name else ""
            if p.startswith("lib/yaml/") and m.isfile(): add(t, "site/yaml/" + p[len("lib/yaml/"):], s.extractfile(m).read()); n += 1
    for f in open(files_list, encoding="utf-8").read().split("\n"):
        rel = os.path.relpath(f, repo).replace("\\", "/")
        add(t, "core/" + rel, open(f, "rb").read()); n += 1
print("файлов", n, "архив МБ", round(os.path.getsize(out) / 2**20, 2))
