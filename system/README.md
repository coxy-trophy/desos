# system — DesOS security and platform groundwork

Installed into the image by `image/components.py` (first in the fixed component
order). Packages and units are declared in `component.json`; `rootfs/` is copied
verbatim onto the image root after all packages are installed.

**Nothing here has been booted.** The build VM no longer exists; the host-safe
tests check file contents, the firewall script against a fake `ufw`, and the
update-channel tool against temp files. Package names, unit names and paths on
Ubuntu 26.04 are assumptions until an image is built and booted.

## What it provides

| Area | Mechanism |
| --- | --- |
| AppArmor | `apparmor` package, `apparmor.service` enabled. The built DesOS kernel config has `CONFIG_SECURITY_APPARMOR=y` and `apparmor` in `CONFIG_LSM`. |
| Firewall | `ufw`; `desos-firewall-setup.service` runs `/usr/lib/desos/firewall-setup` once (stamp `/var/lib/desos/firewall-configured`): default deny incoming, allow outgoing, DES Connect from LAN sources only, then `ufw --force enable`. A failure leaves no stamp and is retried next boot; it never blocks boot. After the stamp exists the administrator's own `ufw` choices are left alone. |
| DES Connect | ufw application profile `des-connect`: UDP 1716 (discovery) and TCP 1716–1764 (TLS link and payload transfers), matching `des-ui/connect/src/service.rs` and `transfers.rs`. Allowed only from 10/8, 172.16/12, 192.168/16, 169.254/16 and fe80::/10. |
| Security upgrades | `unattended-upgrades` with an explicit `origin=Ubuntu,archive=resolute-security` pattern (the stock pattern uses `lsb_release -i`, which is `DesOS` here and would match nothing) plus `origin=DesOS,archive=desos`. No automatic reboot. Disabled in the live session (`boot=casper`). |
| Swap | `systemd-zram-generator`: `zram0`, `min(ram / 2, 4096)` MiB, zstd, priority 100; `vm.page-cluster=0`, `vm.swappiness=100`. |
| Update channel | `/usr/sbin/desos-update-channel enable\|disable\|status` flips `Enabled:` in `/etc/apt/sources.list.d/desos-updates.sources`. That file and the apt pin `/etc/apt/preferences.d/desos` are written by `image/configure.py`, which holds the one URL constant (`UPDATE_CHANNEL_URL`). The URL is a `.invalid` placeholder: nothing is published, and `enable` refuses a placeholder, a non-https URI or a missing keyring. The tool never contacts a server or runs apt. The tree such a server would hold is prepared offline by `image/publish_channel.py` (operator steps in the top-level README); the apt pin also covers the kernel metapackages `linux-image-desos` and `linux-headers-desos`. |

`fwupd`, `power-profiles-daemon` and `mesa-vulkan-drivers` are plain entries in
`image/packages.list`; `power-profiles-daemon.service` is enabled from here.

## Installed-system notes

Everything in this component is meant to stay on installed systems; the
Calamares cleanup must not remove it. `/var/lib/desos/firewall-configured` is
created at runtime in the live overlay and is not part of the squashfs, so an
installed system configures its firewall on its own first boot.
