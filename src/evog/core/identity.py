"""One runtime identity shared by campaigns, held-out runs and judge recovery."""

from pathlib import Path

from evog.core.io import fingerprint


def runtime_source_fingerprint() -> str:
    package = Path(__file__).parents[1]
    return fingerprint(
        {
            path.relative_to(package).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(package.rglob("*"))
            if path.is_file() and path.suffix in {".py", ".md", ".json", ".toml", ".yaml"}
        }
    )
