"""Disposable UI for manual/browser-tool QA; never reads project .env."""

import os
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["SLEIGHT_HOME"] = sys.argv[1]
os.environ.pop("SLEIGHT_DATABASE_URL", None)
from sleight.deploy.api.app import create_app

uvicorn.run(create_app(token="console-local-fixture-only"), host="127.0.0.1", port=int(sys.argv[2]))
