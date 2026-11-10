"""Calamares 3.3 Python job adapter for DesOS target cleanup."""
import importlib.util
from pathlib import Path

import libcalamares


def pretty_name():
    return "Prepare your installed DesOS system"


def run():
    spec = importlib.util.spec_from_file_location("desos_cleanup", Path(__file__).with_name("cleanup.py"))
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)
    try:
        def target_command(args):
            # Calamares provides the chroot. env is fixed and never inherited
            # from the live account, avoiding an interactive apt frontend.
            libcalamares.utils.check_target_env_call([
                "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", *args
            ])

        cleanup.cleanup_target(
            Path(libcalamares.globalstorage.value("rootMountPoint")),
            libcalamares.globalstorage.value("username"),
            target_command,
        )
    except Exception as error:
        libcalamares.utils.warning(f"DesOS target cleanup failed: {error}")
        return ("Installed-system setup failed", "DesOS could not remove live-session settings. Installation cannot finish safely. See the installer log for details.")
    return None
