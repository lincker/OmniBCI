"""Small author commands sharing the host's plugin package rules."""
import argparse
import json
import os
from pathlib import Path
import signal

from omnibci.infrastructure.plugin_management.installer import PackageInstallError
from omnibci.infrastructure.plugin_management.project_tools import package_project, validate_project
from omnibci.application.plugin_management.packages import PluginPackageError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate, package and develop OmniBCI API v1 plugins")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Statically validate a selected plugin root")
    validate.add_argument("project", type=Path)
    package = commands.add_parser("package", help="Create a validated deterministic ZIP")
    package.add_argument("project", type=Path)
    package.add_argument("--output", type=Path, required=True, help="Archive path outside the plugin root")
    develop = commands.add_parser("develop", help="Launch an isolated snapshot through normal module hosts")
    develop.add_argument("project", type=Path)
    develop.add_argument("--configuration", type=Path, required=True)
    develop.add_argument("--contribution", required=True, help="Contribution ID declared by the source project")
    develop.add_argument("--companion", type=Path, action="append", default=[], help="Companion ZIP (repeatable)")
    develop.add_argument("--port", type=int, default=8002, help="Local development API port")
    develop.add_argument("--command-timeout", type=float, help="Host command timeout in seconds (default: host setting)")
    args = parser.parse_args(argv)
    try:
        if args.command == "develop":
            from omnibci.plugin_development import develop_project
            if os.name == "nt":
                signal.signal(signal.SIGBREAK, signal.default_int_handler)
            return develop_project(args.project, args.configuration, args.contribution,
                                   companions=args.companion, port=args.port, command_timeout=args.command_timeout)
        result = (validate_project(args.project) if args.command == "validate"
                  else package_project(args.project, args.output))
    except (PackageInstallError, PluginPackageError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
