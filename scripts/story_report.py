"""Render saved story-report-bundle-v1 evidence offline, without executing any checks.

Usage: python -m scripts.story_report --input CONFIG --output DIRECTORY [--overwrite]
Paths resolve from the working directory. Legacy story1-report-bundle-v1 is accepted.
Exit codes: 0 rendered evidence (not acceptance), 1 I/O error, 2 invalid input/evidence.
The output directory must be under the working directory's ignored artifacts directory.
"""

from scripts.story1_report import build_bundle, build_parser, generate_bundle, main

__all__ = ["build_bundle", "build_parser", "generate_bundle", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
