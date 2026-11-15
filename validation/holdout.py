"""
Validation harness (built out on D12).
Method:
  - train on Oct 1-24, hold out Oct 25-31 (enforced by which traversals the model saw)
  - Test A: replay ~1,000 real journeys from the hold-out week through the planner,
    compare predicted bus-time vs observed
  - Test B: per-edge predicted vs observed traversal seconds
  - Test C: 40 canonical OD pairs x 3 departure times vs Google Distance Matrix
Outputs: docs/report/  (JSON + charts page)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))


def main():
    from common import CLEAN, MODEL
    print("holdout harness: skeleton — fills in on D12")
    print("traversals found:", len(list(CLEAN.glob('traversals_*.parquet'))))
    print("model dir:", MODEL)


if __name__ == "__main__":
    main()
