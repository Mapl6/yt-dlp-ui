"""Make a copy of a YT Downloader library that is safe to run a test server against.

Usage: python make_safe_copy.py <dest_data_dir> <scratch_output_dir> [source_data_dir]

The copy can never touch the user's real downloads:
  * every queued / downloading / processing video is reset to "new" (nothing starts downloading);
  * every playlist and video folder points at <scratch_output_dir>, and known file paths are cleared;
  * an interrupted "Check files" is marked finished.
The source is read only (SQLite opened read-only, or the legacy state.json). The result is written as a
legacy state.json in <dest_data_dir>, which the app migrates into its own library.db on start.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path


def load(src: Path) -> list:
    db = src / "library.db"
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            jobs = {jid: json.loads(data) for jid, data in conn.execute("SELECT id, data FROM jobs")}
            for job in jobs.values():
                job["items"] = []
            for jid, data in conn.execute("SELECT job_id, data FROM items"):
                if jid in jobs:
                    jobs[jid]["items"].append(json.loads(data))
        finally:
            conn.close()
        return list(jobs.values())
    for name in ("state.json", "state.json.migrated", "state.backup.json"):
        if (src / name).exists():
            return json.loads((src / name).read_text())
    return []


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    dest, scratch = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    src = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else Path(__file__).resolve().parents[4] / "data"
    if dest == src:
        sys.exit("Refusing to write into the source library.")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    scratch.mkdir(parents=True, exist_ok=True)

    jobs, reset = load(src), 0
    for job in jobs:
        job["outdir"] = str(scratch)
        job["paused"] = False
        if (job.get("check") or {}).get("running"):
            job["check"]["running"] = False
        for it in job["items"]:
            if it["status"] in ("queued", "downloading", "processing"):
                it["status"] = "new"
                reset += 1
            it.update(outdir=str(scratch), filepath=None, next_try=0, cancel=False)
    (dest / "state.json").write_text(json.dumps(jobs))

    settings = {}
    if (src / "settings.json").exists():
        settings = json.loads((src / "settings.json").read_text())
    settings["output_dir"] = str(scratch)
    (dest / "settings.json").write_text(json.dumps(settings, indent=2))

    items = sum(len(j["items"]) for j in jobs)
    print(f"Safe copy: {len(jobs)} playlists, {items} videos ({reset} reset from queued/active) -> {dest}")
    print(f"All downloads redirected to {scratch}")


if __name__ == "__main__":
    main()
