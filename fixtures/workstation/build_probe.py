"""Build the fixture driver from the selected Workstation source snapshot."""

import json
import os
import subprocess
import sys
from pathlib import Path


root = Path.cwd().resolve()
spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
compiler = os.environ[spec["compiler_env"]]
output = (root / spec["output_relative"]).resolve()
if not output.is_relative_to(root):
    raise SystemExit("Invalid build output binding")
output.parent.mkdir(parents=True, exist_ok=True)
source_root = (root / spec["source_root_relative"]).resolve()
sources = [(root / file).resolve() for file in spec["source_files"]]
if not source_root.is_relative_to(root) or any(not file.is_relative_to(root) for file in sources):
    raise SystemExit("Invalid source binding")
subprocess.run([compiler, *spec["flags"], "-I", str(source_root), *map(str, sources), "-o", str(output)], check=True)
print("Workstation fixture driver compiled")
