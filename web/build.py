"""Assemble the web edition into one folder of static files.

    python3.12 web/build.py [--out DIR]      # default: web/dist

The site is this folder's page files plus py/app.zip: the tracker's src/
package and the demo portfolio, which py-worker.js unpacks into Pyodide.
GitHub Pages serves the result as is (see .github/workflows/pages.yml). To
try it locally:

    python3.12 web/build.py && python3.12 -m http.server 8000 -d web/dist

then open http://localhost:8000 (the price relay accepts localhost).

The script finds the repository by looking upwards for src/pipeline.py, so it
also runs from the private repo's public_overlay/web/.
"""
import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Files in web/ that are tooling, not part of the site.
NOT_SITE = {"build.py", "README.md", "dist", "test", "node_modules", "package.json",
            "package-lock.json", ".DS_Store"}
# What Python needs in the browser. src/brief.py is the private edition's
# markdown brief, which the public edition never imports.
APP_FILES = ["src/*.py", "data/demo_trades.csv", "data/demo_allocation.yaml"]
APP_EXCLUDE = {"src/brief.py"}
# Fixed timestamp, so an unchanged tree gives a byte-identical zip.
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def find_repo() -> Path:
    for parent in HERE.parents:
        if (parent / "src" / "pipeline.py").is_file():
            return parent
    sys.exit("Couldn't find the repository (no src/pipeline.py above web/).")


def app_zip(repo: Path, dest: Path) -> int:
    files = sorted(
        p for pattern in APP_FILES for p in repo.glob(pattern)
        if p.is_file() and p.relative_to(repo).as_posix() not in APP_EXCLUDE
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            info = zipfile.ZipInfo(p.relative_to(repo).as_posix(), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())
    return len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=HERE / "dist",
                        help="output folder, replaced on each build (default: web/dist)")
    out = parser.parse_args().out.resolve()
    repo = find_repo()
    if out == repo or out in HERE.parents or out == HERE:
        sys.exit(f"Refusing to replace {out}.")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    pages = 0
    for p in sorted(HERE.iterdir()):
        if p.name in NOT_SITE or p.name.startswith("."):
            continue
        if p.is_dir():
            shutil.copytree(p, out / p.name)
        else:
            shutil.copyfile(p, out / p.name)
        pages += 1
    n = app_zip(repo, out / "py" / "app.zip")
    print(f"Built {out}: {pages} page files, py/app.zip with {n} files.")


if __name__ == "__main__":
    main()
