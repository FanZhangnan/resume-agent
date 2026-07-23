"""Audit a completed ``vercel build`` output before preview deployment."""

import json
import os
from pathlib import Path


ROOT = Path(os.environ.get("VERCEL_BUILD_ROOT", Path(__file__).resolve().parent)).resolve()
OUTPUT = ROOT / ".vercel" / "output"
SERVICE_NAMES = {"web", "resume_workflow"}
REQUIRED_FILES = {
    "webui/vercel_server.py",
    "webui/static/vercel_app.html",
    "workflows/resume_workflow.py",
    "workflows/vercel_worker.py",
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
    services = {item["name"]: item for item in config.get("services", [])}
    assert set(services) == SERVICE_NAMES, set(services)
    assert all(item.get("schema") == "experimentalServicesV2" for item in services.values())

    for name in SERVICE_NAMES:
        function_config = (
            OUTPUT / "services" / name / "functions" / "index.func" / ".vc-config.json"
        )
        assert function_config.is_file(), function_config
        data = json.loads(function_config.read_text(encoding="utf-8"))
        files = set((data.get("filePathMap") or {}).keys())
        forbidden = sorted(path for path in files if _forbidden(path))
        assert not forbidden, (name, forbidden[:10])
        assert REQUIRED_FILES <= files, (name, REQUIRED_FILES - files)

        triggers = data.get("experimentalTriggers") or []
        if name == "resume_workflow":
            assert len(triggers) == 1, triggers
            assert triggers[0].get("type") == "queue/v2beta"
            assert triggers[0].get("topic") == "__wkf_*"
        else:
            assert not triggers, triggers


if __name__ == "__main__":
    test_built_services_and_file_maps()
    print("Vercel build-output audit passed.")
