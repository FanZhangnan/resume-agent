"""Run the repository's offline executable checks; never call live gateways."""

import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"test_llm.py", "test_vercel_build_output.py"}


def main():
    test_env = dict(os.environ)
    for name in ("AGENT_MODEL_OPTIONS", "AGENT_BASE_URL", "AGENT_MODEL", "AGENT_REASONING_EFFORT", "AGENT_LIVE_ENABLED"):
        test_env.pop(name, None)
    test_env.update({"AGENT_MOCK": "1", "AGENT_WORKFLOW_TEST": "1", "OPENAI_API_KEY": "unit-test-gateway-key"})
    failures = []
    tests = [path for path in sorted((ROOT / "tests").glob("test_*.py")) if path.name not in EXCLUDED]
    for path in tests:
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=test_env, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            failures.append(path.name)
            print(f"FAIL {path.name}: exceeded 120 seconds", flush=True)
            continue
        elapsed = time.monotonic() - started
        if result.returncode:
            failures.append(path.name)
            print(f"FAIL {path.name} ({elapsed:.2f}s)", flush=True)
            print("\n".join((result.stdout + result.stderr).splitlines()[-45:]), flush=True)
        else:
            print(f"PASS {path.name} ({elapsed:.2f}s)", flush=True)
    print(f"{len(tests) - len(failures)}/{len(tests)} offline suites passed; live API and build-artifact checks are separate.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
