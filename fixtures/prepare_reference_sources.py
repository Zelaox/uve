"""Copy explicitly bound source inputs into the isolated reference fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path


def within(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or candidate == root or not candidate.is_relative_to(root):
        raise ValueError("Reference path escapes its bound root")
    return candidate


def main() -> None:
    root = Path.cwd().resolve()
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    inventory = []
    for fixture in spec["fixtures"]:
        source_root = Path(os.environ[fixture["source_root_env"]]).resolve(strict=True)
        target = within(root, fixture["destination_relative"])
        if not target.is_relative_to(within(root, spec["fixture_root_relative"])):
            raise ValueError("Snapshot destination is not a fixture directory")
        paths = set()
        for pattern in fixture["source_globs"]:
            matches = {p for p in source_root.glob(pattern) if p.is_file()}
            if not matches:
                raise ValueError(f"No source matched {pattern}")
            paths.update(matches)
        written = []
        for source in sorted(paths):
            relative = source.relative_to(source_root).as_posix()
            if within(source_root, relative) != source:
                raise ValueError("Source snapshot contains a link")
            data = source.read_bytes()
            if len(data) > spec["max_source_file_bytes"]:
                raise ValueError("Source file exceeds the snapshot budget")
            destination = within(target, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or destination.read_bytes() != data:
                temporary = destination.with_suffix(destination.suffix + f".{uuid.uuid4().hex}.preparing")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(data)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            digest = hashlib.sha256(data).hexdigest()
            written.append((source, relative, digest))
        for source, relative, digest in written:
            if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError("Reference source changed during snapshot")
            inventory.append({"fixture": fixture["id"], "path": relative, "sha256": digest})
    print(json.dumps({"status": "reference_sources_prepared", "files": inventory}))


if __name__ == "__main__":
    main()
