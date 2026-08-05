"""
CoderIT Modal Executor
-----------------------
Deploys a single HTTPS endpoint on Modal that runs arbitrary Python (or
shell) code inside an isolated sandbox and returns stdout/stderr/exit
code as JSON. CoderIT's AI calls this (via the Vercel proxy, which adds
a shared-secret header) whenever it decides code needs to actually run
instead of just being shown to the user.

Deploy with:
    modal deploy modal_executor.py

This creates/updates a permanent HTTPS endpoint. The URL is stable across
deploys (based on workspace + app + function name), so the Vercel proxy
only needs to be configured once.
"""

import modal
import subprocess
import time

app = modal.App("coderit-executor")

# Base image: Python 3.11 + a handful of common packages so most
# AI-generated snippets (data analysis, quick scripts, simple ML) work
# without the model having to first install anything.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "numpy",
        "pandas",
        "requests",
        "matplotlib",
        "fastapi[standard]",
    )
)

# Shared secret CoderIT's Vercel proxy must send as `X-Executor-Secret`.
# Set via `modal secret create coderit-executor-secret EXECUTOR_SHARED_SECRET=...`
executor_secret = modal.Secret.from_name("coderit-executor-secret")


@app.function(
    image=image,
    secrets=[executor_secret],
    timeout=60,  # hard ceiling per request; keeps a runaway script from burning credits
)
@modal.fastapi_endpoint(method="POST")
def run(item: dict):
    """
    POST body: { "code": "<python source>", "language": "python" | "bash", "secret": "..." }
    Returns:   { "stdout": "...", "stderr": "...", "exit_code": 0, "duration_ms": 123 }
    """
    import os

    expected_secret = os.environ.get("EXECUTOR_SHARED_SECRET", "")
    provided_secret = item.get("secret", "")
    if not expected_secret or provided_secret != expected_secret:
        return {"error": "unauthorized", "stdout": "", "stderr": "", "exit_code": 401}

    code = item.get("code", "")
    language = item.get("language", "python")

    if not code.strip():
        return {"error": "empty code", "stdout": "", "stderr": "", "exit_code": 400}

    # Write the code to a temp file and run it in a fresh subprocess so
    # stdout/stderr capture cleanly and a crash can't take down the
    # container's own process.
    ext = "py" if language == "python" else "sh"
    path = f"/tmp/snippet.{ext}"
    with open(path, "w") as f:
        f.write(code)

    cmd = ["python3", path] if language == "python" else ["bash", path]

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,  # leaves headroom under the 60s function timeout
        )
        duration_ms = int((time.time() - start) * 1000)
        return {
            "stdout": result.stdout[-8000:],  # cap output size returned to the app
            "stderr": result.stderr[-4000:],
            "exit_code": result.returncode,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "Execution timed out after 45 seconds",
            "exit_code": 124,
            "duration_ms": 45000,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Executor error: {e}",
            "exit_code": 1,
            "duration_ms": int((time.time() - start) * 1000),
        }
