# Vendor repositories

Two applications come from their makers' own signed apt repositories rather
than from Ubuntu, because Ubuntu ships them only as snaps (Brave) or not at
all (Visual Studio Code), and DesOS has no snap support. `build-rootfs.sh`
installs these keyrings and sources before the first `apt-get update`, so the
packages install like any other, and the installed system keeps the sources:
both applications update with `sudo apt upgrade`.

| Source | Keyring | Key fingerprints |
| --- | --- | --- |
| `brave-browser.sources` | `keyrings/brave-browser-archive-keyring.gpg` | `DBF1 A116 C220 B8C7 164F 9823 0686 B784 2003 8257`, `47D3 2A74 E9A9 E013 A4B4 926C 68D5 13D3 6A73 CD96` (Brave Linux Release) |
| `vscode.sources` | `keyrings/microsoft.gpg` | `BC52 8686 B50D 79E3 39D3 721C EB3E 94AD BE12 29CF` (Microsoft Release signing) |

The keyrings were fetched over HTTPS from
`brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg` and
`packages.microsoft.com/keys/microsoft.asc` (de-armoured) on 2026-11-14.
`tests/test_vendor.py` checks the fingerprints, so a changed keyring is a
deliberate, reviewed change.

Licensing: Brave is open source (MPL 2.0). Visual Studio Code's binary is under
Microsoft's product licence, which does not allow redistributing it, so an
image that contains it is for the owner's own machines. Before publishing a
DesOS image, install VS Code on first boot instead, or switch to VSCodium.
