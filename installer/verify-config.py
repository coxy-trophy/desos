#!/usr/bin/env python3
"""Check DesOS configuration against an extracted Calamares module tree."""
import argparse
from pathlib import Path
import sys

import jsonschema
import yaml


def verify(modules, upstream=None):
    source = Path(__file__).resolve().parent
    settings = yaml.safe_load((source / "calamares/settings.conf").read_text())
    configured = {name for phase in settings["sequence"] for names in phase.values() for name in names}
    count = 0
    for name in sorted(configured):
        directory = source / "modules/desoscleanup" if name == "desoscleanup" else modules / name
        desc = yaml.safe_load((directory / "module.desc").read_text())
        if desc["name"] != name:
            raise ValueError(f"Module name mismatch: {name}")
        if desc["interface"] == "python" and not (directory / desc["script"]).is_file():
            raise ValueError(f"Python module has no script: {name}")
        if desc["interface"] == "qtplugin" and not (directory / desc["load"]).is_file():
            raise ValueError(f"Qt module has no plugin: {name}")
        config_file = source / f"calamares/modules/{name}.conf"
        if config_file.exists():
            config = yaml.safe_load(config_file.read_text())
            schema = (upstream / name if upstream else directory) / f"{name}.schema.yaml"
            if schema.exists():
                jsonschema.validate(config, yaml.safe_load(schema.read_text()))
                count += 1
    branding = source / "branding/desos"
    desc = yaml.safe_load((branding / "branding.desc").read_text())
    for filename in [*desc["images"].values(), desc["slideshow"]]:
        if not (branding / filename).is_file():
            raise ValueError(f"Branding asset is missing: {filename}")
    if settings["dont-chroot"] or not settings["prompt-install"]:
        raise ValueError("Installer must use target chroot and final confirmation")
    print(f"Verified {len(configured)} available modules, {count} module schemas and all branding assets")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules-directory", required=True, type=Path)
    parser.add_argument("--upstream-directory", type=Path, help="Optional v3.3.14 src/modules tree, including Qt module schemas")
    args = parser.parse_args()
    verify(args.modules_directory, args.upstream_directory)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, jsonschema.ValidationError) as error:
        print(f"Calamares configuration verification failed: {error}", file=sys.stderr)
        sys.exit(1)
