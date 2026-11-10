# DesOS installer design

The installer is a native Qt Calamares workflow for people trying DesOS from a
live USB. Its one job is to turn an explicitly chosen disk into their own desktop.
The form, selects, keyboard navigation, validation, disk summary and final
confirmation belong to Calamares 3.3.14; branding must not replace those owners.

Reuse DEs-UI's matte instrument palette: Ground `#06080b`, Panel `#0a0e12`, Track
`#141b22`, Line `#1a232b`, Text `#dce7ec`, Secondary `#8fa3b0`, Accent `#4fd8ce`.
Accent marks the active step and primary action. Chakra Petch carries interface
text; JetBrains Mono is restrained to release/upstream attribution. The matching
fonts and their SIL Open Font Licenses are bundled from the existing desktop.

Retain Calamares's left step rail, generous form area and bottom navigation.
Use 6px controls, visible keyboard focus and full contrast on dark surfaces.
The signature is the restrained, bracketed DesOS wordmark, with transparent
Linux/Ubuntu attribution. SVG and static QML keep assets resolution independent;
there is no motion, remote content, analytics or log-upload endpoint.

Automatic login is visibly unavailable in this preview. Credentials are required
by the installed greetd configuration. Encryption and LVM are outside this preview's
validated scope. Disk erasure is never selected by default and always receives
Calamares's summary and final confirmation. Runtime visual/keyboard acceptance is
performed in the parent's live-boot VM, including a short display and native
Wayland/Xwayland launch; configuration tests alone do not establish visual success.
