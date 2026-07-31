from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a small, sanitized GitHub Pages update heartbeat."
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--run-url")
    args = parser.parse_args()

    manifest = json.loads(
        Path(args.manifest).read_text(encoding="utf-8")
    )
    heartbeat = {
        "schema_version": 1,
        "generated_at": manifest.get("generated_at"),
        "data_updated_at": manifest.get("data_updated_at"),
        "repository": manifest.get("repository"),
        "commit_sha": manifest.get("commit_sha"),
        "league": manifest.get("league"),
        "ranked_items": manifest.get("ranked_items"),
        "history_items": manifest.get("history_items"),
        "workflow_run_id": args.run_id,
        "workflow_run_url": args.run_url,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(heartbeat, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
