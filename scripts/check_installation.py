"""Report installed dependency origins; no credentials, SDK clients or network."""

import argparse
import importlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import shlex
import shutil
import sys


def host_report(os_release=Path("/etc/os-release")):
    """Stdlib-only inventory; safe to run before creating the Bot environment."""
    distro, errors = {}, []
    try:
        text = os_release.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        text = ""
        errors.append({"check": "os-release", "error": type(exc).__name__})
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in {"ID", "VERSION_ID", "PRETTY_NAME"}:
            try:
                words = shlex.split(value, comments=True)
            except ValueError:
                errors.append({"check": name, "error": "invalid_os_release_value"})
            else:
                distro[name] = " ".join(words)
    return {
        "mode": "host-only", "python": sys.version, "executable": sys.executable,
        "python_supported_by_project": sys.version_info >= (3, 11),
        "platform": platform.platform(), "machine": platform.machine(),
        "virtual_environment": sys.prefix != sys.base_prefix,
        "distribution": distro, "uv_on_path": shutil.which("uv"),
        "obscura_on_path": shutil.which("obscura"), "errors": errors,
        "note": "Read-only host inventory; no env/config/credentials, SDK imports, network, database or service changes. Dependency and service checks remain separate.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--math", action="store_true", help="Also require optional math dependencies"
    )
    parser.add_argument(
        "--host-only", action="store_true", help="Stdlib-only host inventory, before installation"
    )
    args = parser.parse_args()
    if args.host_only:
        if args.math:
            parser.error("--host-only does not check dependencies; run --math separately")
        report = host_report()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return bool(report["errors"] or not report["python_supported_by_project"])
    packages = {
        "sayuki_bot": "sayuki-bot",
        "discord": "discord.py",
        "openai": "openai",
        "dotenv": "python-dotenv",
        "aiohttp": "aiohttp",
        "PIL": "Pillow",
    }
    if args.math:
        packages.update(sympy="sympy", numpy="numpy", matplotlib="matplotlib")
    report = {
        "python": sys.version,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": {},
        "errors": [],
    }
    for module_name, distribution in packages.items():
        try:
            module = importlib.import_module(module_name)
            report["packages"][distribution] = {
                "version": version(distribution),
                "file": str(Path(module.__file__).resolve()),
            }
        except Exception as exc:
            report["errors"].append({"package": distribution, "error": type(exc).__name__})
    report["obscura_on_path"] = shutil.which("obscura")
    report["note"] = (
        "External Obscura is optional for startup; actual web tools require its configured binary. No network or model calls made."
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return bool(report["errors"])


if __name__ == "__main__":
    raise SystemExit(main())
