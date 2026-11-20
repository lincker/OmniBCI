"""Small author commands sharing the host's plugin package rules."""
import argparse
import json
from pathlib import Path

from omnibci.infrastructure.plugins.package_installer import PackageInstallError
from omnibci.infrastructure.plugins.project_tools import package_project, validate_project


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate and package OmniBCI API v1 plugins")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Statically validate a selected plugin root")
    validate.add_argument("project", type=Path)
    package = commands.add_parser("package", help="Create a validated deterministic ZIP")
    package.add_argument("project", type=Path)
    package.add_argument("--output", type=Path, required=True, help="Archive path outside the plugin root")
    args = parser.parse_args(argv)
    try:
        result = (validate_project(args.project) if args.command == "validate"
                  else package_project(args.project, args.output))
    except (PackageInstallError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
