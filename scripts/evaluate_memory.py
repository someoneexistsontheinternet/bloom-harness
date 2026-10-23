"""Compatibility entry point for the retired automatic-Luna memory evaluator.

The owner now requires Astra approval for every semantic memory write. The old
workflow purchased Luna proposals and published them automatically, so running it
would test a superseded contract. Its historical source and results remain in
Git and the recorded acceptance runs.
"""

from __future__ import annotations

import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_known_args()
    parser.error(
        "Automatic Luna publication is retired. Use scripts/evaluate_research_context.py --help "
        "for retained-web curation, explicit Astra working-compaction and continuation checks. "
        "Exact semantic approval/source/revision checks are in tests/test_memory_authority.py."
    )


if __name__ == "__main__":
    main()
