"""TEST-ONLY: run the real app with the offline extractor instead of Gemini, so the UI can be
exercised without an API key. The header shows "Offline test mode" while this is used.
    python tests/dev_server.py   ->  http://localhost:8000
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uvicorn  # noqa: E402

from app import main  # noqa: E402
from tests.fixture_extractor import fixture_extractor  # noqa: E402


def slow_fixture(pdf, text=""):
    time.sleep(1.5)  # roughly what a real model call feels like
    return fixture_extractor(pdf, text)


slow_fixture.__name__ = "fixture_extractor"
main.EXTRACTOR = slow_fixture

if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=8000)
