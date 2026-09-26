"""Build a local-only F07-F09 formula preview from the three archived PDF pilots.

The output is unreviewed, is never PIT-eligible, and must not be used for
trading, ranking, historical backtests, or formal S2 strategy registration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations.s2_candidate_preview import (  # noqa: E402
    build_s2_candidate_preview,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-dir", type=Path,
                        default=ROOT / "deploy" / "agentctl-q0")
    parser.add_argument("--archive-root", type=Path,
                        default=ROOT / "deploy" / "agentctl-q0" / "forward-archive")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "deploy" / "agentctl-q0" / "s2-candidate-preview.json")
    args = parser.parse_args()
    preview = build_s2_candidate_preview(
        pilot_dir=args.pilot_dir, archive_root=args.archive_root,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(preview, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"{output}: {preview['completeFormulaPreviewCount']}/"
          f"{preview['sampleSize']} formula diagnostics; F10/rank unavailable")


if __name__ == "__main__":
    main()
