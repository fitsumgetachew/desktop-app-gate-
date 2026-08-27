import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A few tests build real widgets to prove the decision path end to end. Qt must
# pick its platform before the first QApplication, and CI has no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_app_data(tmp_path, monkeypatch):
    """Never let a test touch the operator's real app-data directory.

    Path helpers resolve against the user's home unless told otherwise, and
    the environment migration MOVES files. A suite that runs with the real
    directory can — and once did — relocate a live station's database. Every
    test therefore gets a throwaway app-data root, on every platform.
    """
    from smart_gate.utils import paths

    root = tmp_path / "app-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    monkeypatch.setenv("APPDATA", str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root))
    paths.set_active_environment(None)
    yield
    paths.set_active_environment(None)
