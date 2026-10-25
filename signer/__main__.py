"""python3 -m signer <command>

  sync   bring the signed releases in line with the main repository and the
         certificate pool: check every certificate with Apple, publish status
         changes, sign whatever is new or changed (macOS), drop what left
  site   build the install site from the releases

Run from the repository root. Everything is configured by environment
variables (see README.md); the flags below override the matching ones."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
from pathlib import Path

from .util import CommandError, Fatal, HTTPFailure, error


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(prog="python3 -m signer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path,
                        default=Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "signer",
                        help="scratch space (default: $RUNNER_TEMP/signer)")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync", help="check, sign and publish")
    sync_p.add_argument("--tag", default=os.environ.get("SIGN_TAG", ""),
                        help="a main-repository release to work on (default: its live stable and beta)")
    sync_p.add_argument("--force", action="store_true",
                        default=os.environ.get("SIGN_FORCE", "").lower() in ("1", "true", "yes"),
                        help="re-sign every certificate, not only new and changed ones")
    sync_p.add_argument("--check-only", action="store_true",
                        help="never sign; publish status changes and report whether signing is needed")
    sync_p.add_argument("--dry-run", action="store_true",
                        help="change nothing on GitHub; write what would be published under the workdir")

    site_p = sub.add_parser("site", help="build the install site")
    site_p.add_argument("--out", type=Path, default=root / "_site")
    site_p.add_argument("--static-dir", type=Path, default=Path(os.environ.get("STATIC_DIR") or root / "main-repo"))
    site_p.add_argument("--site-url", default=os.environ.get("SITE_URL") or "https://sideinstaller.net")
    site_p.add_argument("--from-dir", type=Path, help="build from a dry run's output instead of the releases")

    args = parser.parse_args(argv)
    # Let a cancelled run clean up (the keychain above all) on its way out.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    from .sync import Config
    cfg = Config.from_env(root, args.workdir.resolve())
    try:
        if args.command == "sync":
            from .sync import sync
            sync(cfg, tag=args.tag.strip() or None, force=args.force,
                 check_only=args.check_only, dry_run=args.dry_run)
        else:
            from .site import build
            build(root=root, out=args.out.resolve(), static_dir=args.static_dir.resolve(),
                  site_url=args.site_url, signed_repo=cfg.signed_repo, main_repo=cfg.main_repo,
                  prefix=cfg.prefix, ipa_asset=cfg.ipa_asset, workdir=cfg.workdir / "site",
                  from_dir=args.from_dir.resolve() if args.from_dir else None)
    except (Fatal, HTTPFailure, CommandError) as exc:
        error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
