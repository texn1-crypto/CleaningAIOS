from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quality_criteria import assess_junit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    args = parser.parse_args()
    result = assess_junit(args.junit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["not_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
