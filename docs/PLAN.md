# DesOS Live Distribution Build Plan

Owner authorized autonomous implementation and USB erase; execute continuously.
Spec: `docs/DESIGN.md`. Branch: `codex/desos-live`.

1. [x] Pin signed upstream LTS source and build kernel packages in
   the isolated Ubuntu worker, preserving original source and configuration.
2. [x] Configure Calamares and DesOS branding/launcher/installed-system cleanup;
   verify configuration against the actually installed Calamares version.
3. [x] Build a clean Ubuntu-compatible DesOS root filesystem with verified native
   desktop packages, upstream and recovery kernels, signed local package metadata,
   public live account, and no development credentials or default SSH service.
4. [x] Produce a BIOS/UEFI hybrid squashfs live ISO with Try/Install/Recovery menu
   entries and matching startup images. Record component/source/artifact hashes.
5. [x] Boot live media in QEMU BIOS and UEFI, verify DesOS identity, correct kernel,
   desktop readiness and input; install to a disposable disk and boot the installed
   system, checking account setup and removal of live-only privileges.
6. [x] Review guards and image/installer integration; fix material findings and
   rerun affected checks. Preserve artifacts/checksums and honest limitations.
7. [x] Revalidate approved USB model/serial/size/removability and prohibited-host
   disk relationship; unmount it, write only the verified ISO, flush, and compare
   all written bytes. Record receipt and leave a bootable USB for the owner.

Independent kernel, installer, and USB-tool tasks may run in parallel in their
assigned directories. Only the parent operates the build VM, test VMs and USB.
No production source changes are planned in DEs-UI. Tests must exercise actual
guard/config behavior; kernel/live/install/write results require real evidence.
