"""The cloud copy on Modal: one always-on container running `textcrm run` with "cloud": true.

Deploy: uvx modal deploy textcrm/modal_app.py
The database lives on the "textcrm-private" Volume; settings and keys come from the "textcrm-cloud"
Secret (TEXTCRM_JSON, QUO_API_KEY, optionally ANTHROPIC_API_KEY). One container only: a Volume is
not safe for two writers to the same SQLite file.
"""
import subprocess

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("tzdata")
    .pip_install("anthropic")
    .env({"TZ": "America/New_York"})
    .add_local_python_source("textcrm")
)
private = modal.Volume.from_name("textcrm-private", create_if_missing=True)
app = modal.App("textcrm", image=image)


@app.function(volumes={"/root/private": private}, secrets=[modal.Secret.from_name("textcrm-cloud")],
              min_containers=1, max_containers=1, timeout=86400)
@modal.concurrent(max_inputs=100)
@modal.web_server(8787, startup_timeout=60)
def web():
    # The volume is committed in the background every few seconds and on shutdown.
    subprocess.Popen(["python", "-u", "-m", "textcrm", "run"], cwd="/root")
