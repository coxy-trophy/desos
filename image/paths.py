"""Protect the image worker's explicit filesystem staging directory."""
import argparse
from pathlib import Path


def validate_rootfs(root: Path, parent: Path = Path('/srv/desos-build')) -> Path:
    if not root.is_absolute() or not parent.is_absolute() or root.is_symlink():
        raise ValueError('Absolute, non-symlink rootfs path required')
    base, target = parent.resolve(), root.resolve()
    if base == Path('/') or target == base or not target.is_relative_to(base):
        raise ValueError('Rootfs must be a separate child of the build directory')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(validate_rootfs(args.root))
