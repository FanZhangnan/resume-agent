"""Audit a completed ``vercel build`` output before preview deployment."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / ".vercel" / "output"
SERVICE_TYPES = {"web": "web", "resume_workflow": "worker"}
REQUIRED_FILES = {
    "webui/vercel_server.py",
    "webui/static/vercel_app.html",
    "workflows/resume_workflow.py",
    "tools/analysis.py",
    "llm_client.py",
}


def _forbidden(path):
    return (
        path == ".env"
        or path.startswith(".env.")
        or path.startswith(".claude/")
        or path.startswith(".vercel/")
        or path.startswith(".venv312/")
        or path.startswith(".playwright-cli/")
        or path.startswith("venv/")
        or path.startswith("output/")
        or path.startswith("output_test/")
        or path.startswith("docs/")
        or path.startswith("samples/")
        or path.startswith("test_")
    )


def test_built_services_and_file_maps():
    config_path = OUTPUT / "config.json"
    assert config_path.is_file(), "run `vercel build` before this audit"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    services = {item["name"]: item["type"] for item in config.get("services", [])}
    assert services == SERVICE_TYPES, services

    for name in SERVICE_TYPES:
        function_config = (
            OUTPUT / "functions" / "_svc" / name / "index.func" / ".vc-config.json"
        )
        assert function_config.is_file(), function_config
        data = json.loads(function_config.read_text(encoding="utf-8"))
        files = set((data.get("filePathMap") or {}).keys())
        forbidden = sorted(path for path in files if _forbidden(path))
        assert not forbidden, (name, forbidden[:10])
        assert REQUIRED_FILES <= files, (name, REQUIRED_FILES - files)


if __name__ == "__main__":
    test_built_services_and_file_maps()
    print("Vercel build-output audit passed.")
