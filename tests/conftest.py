import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A few tests build real widgets to prove the decision path end to end. Qt must
# pick its platform before the first QApplication, and CI has no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
