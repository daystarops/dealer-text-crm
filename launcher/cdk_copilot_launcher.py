"""One-click launcher for the deterministic CDK Lead Copilot pipeline."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

USER_PROFILE = Path(os.environ.get("USERPROFILE", Path.home())).resolve()
REPOSITORY = USER_PROFILE / "cdk-lead-copilot"
UV_TOOL_DIR = USER_PROFILE / ".uv-tools-clean"
UV_TOOL_BIN_DIR = USER_PROFILE / ".local" / "bin-clean"


def configure_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["UV_TOOL_DIR"] = str(UV_TOOL_DIR)
    environment["UV_TOOL_BIN_DIR"] = str(UV_TOOL_BIN_DIR)
    old_path = environment.get("PATH", "")
    environment["PATH"] = str(UV_TOOL_BIN_DIR) + (os.pathsep + old_path if old_path else "")
    return environment


def locate_executable(preferred: Path | None, names: tuple[str, ...]) -> Path | None:
    if preferred is not None and preferred.is_file():
        return preferred
    for name in names:
        located = shutil.which(name)
        if located:
            return Path(located)
    return None


def windows_review_command(comspec: str, interactive: bool = False) -> list[str]:
    command_text = r"call scripts\run-review.cmd"
    if interactive:
        command_text += " --interactive"
    return [comspec, "/d", "/c", command_text]


def launch_pipeline(environment: dict[str, str], interactive: bool = False) -> int:
    if not REPOSITORY.is_dir():
        print(f"ERROR: Repository not found: {REPOSITORY}")
        return 1
    review_script = REPOSITORY / "scripts" / "run-review.cmd"
    if not review_script.is_file():
        print(f"ERROR: Review script not found: {review_script}")
        return 1
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    command = windows_review_command(comspec, interactive)
    print(f"Working directory: {REPOSITORY}")
    print("Starting interactive troubleshooting." if interactive else "Starting deterministic review pipeline.")
    try:
        return subprocess.Popen(command, cwd=REPOSITORY, env=environment).wait()
    except OSError as error:
        print(f"ERROR: Pipeline could not be launched: {error}")
        return 1


def main() -> int:
    environment = configure_environment()
    print("CDK Lead Copilot Launcher\n========================")
    interactive = any(argument.casefold() == "--interactive" for argument in sys.argv[1:])
    if not interactive:
        browser_use = locate_executable(UV_TOOL_BIN_DIR / "browser-use.exe", ("browser-use.exe", "browser-use"))
        if browser_use is None:
            print(f"ERROR: Browser Use was not found at {UV_TOOL_BIN_DIR / 'browser-use.exe'} or on PATH.")
            return 1
    return launch_pipeline(environment, interactive=interactive)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nLauncher cancelled.")
        raise SystemExit(130)
