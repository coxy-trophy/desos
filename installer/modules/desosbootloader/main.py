"""Calamares 3.3 job: Ubuntu's own bootloader job, unchanged, for DesOS's disk page.

The packaged bootloader module declares requiredModules: [partition] although
it only reads global storage (partitions, bootLoader, efiSystemPartition,
firmwareType), which the desosdisk job fills in the same format. With the
DesOS disk page in place of Calamares's partition module, Calamares refuses
to load it ("bootloader@bootloader could not be loaded"). This loads the
packaged main.py and runs it as it is, under the same bootloader.conf
(settings.conf: instance desosbootloader@bootloader)."""
import importlib.util

UPSTREAM = "/usr/lib/x86_64-linux-gnu/calamares/modules/bootloader/main.py"

_spec = importlib.util.spec_from_file_location("calamares_bootloader", UPSTREAM)
_upstream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_upstream)

pretty_name = _upstream.pretty_name
run = _upstream.run
