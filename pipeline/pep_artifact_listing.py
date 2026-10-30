#!/usr/bin/env python3
"""List this workflow run's artifacts for the certification aggregate (pep-certify.yml).

The listing is read through the capture transport (``pep_capture_io``), so it gets the same
bounded retries for transient GitHub API failures and the same credential-origin rules and
redaction as capture. It writes:
  * ``--listing``: the normalized listing the collector consumes,
    ``[{id, name, expired, size_in_bytes}, ...]`` (no URLs or digests);
  * ``download_ids=<comma-separated ids>`` appended to ``--github-output``: the non-expired
    artifacts whose name starts with ``--name-prefix``.
Exit 0 on success. On any listing failure it writes neither file and exits 1; the workflow
step is continue-on-error, so the collector then fails closed exactly as before. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pep_capture_io as IO      # noqa: E402  shared transport, pagination and redaction

_LISTING_FIELDS = ("id", "name", "expired", "size_in_bytes")


def list_run_artifacts(transport, repo, run_id):
    """Every artifact of the run, in API order, reduced to the allowlisted fields.

    A non-object entry keeps its place as ``None``: the collector counts it as an invalid
    entry and fails closed, so malformed listing data never silently disappears. Its
    content is not copied, so the listing still carries only allowlisted fields."""
    pages = IO._list_pages(transport, lambda p: IO._artifacts_url(repo, run_id, p), "artifacts")
    return [{k: art.get(k) for k in _LISTING_FIELDS} if isinstance(art, dict) else None
            for page in pages for art in page["artifacts"]]


def download_ids(listing, name_prefix):
    """Ids of the non-expired artifacts whose name starts with ``name_prefix``."""
    return [art["id"] for art in listing
            if isinstance(art, dict)
            and isinstance(art.get("name"), str) and art["name"].startswith(name_prefix)
            and art.get("expired") is not True
            and isinstance(art.get("id"), int) and not isinstance(art.get("id"), bool)]


def main(argv=None, env=None):
    ap = argparse.ArgumentParser(description="List this run's artifacts for the certification aggregate")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--name-prefix", required=True)
    ap.add_argument("--listing", required=True, help="write the normalized listing JSON here")
    ap.add_argument("--github-output", required=True, help="file to append download_ids=... to")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env

    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        print("artifact listing failed: no GITHUB_TOKEN available for the REST API", file=sys.stderr)
        return 1
    try:
        listing = list_run_artifacts(IO.UrllibTransport(token), args.repo, args.run_id)
    except IO.CaptureIOError as e:
        print("artifact listing failed: %s" % IO.redact(str(e)), file=sys.stderr)
        return 1
    ids = download_ids(listing, args.name_prefix)
    with open(args.listing, "w", encoding="utf-8") as f:
        json.dump(listing, f)
    with open(args.github_output, "a", encoding="utf-8") as f:
        f.write("download_ids=%s\n" % ",".join(str(i) for i in ids))
    print("Prepared listing with %d artifact(s); download ids: %s"
          % (len(listing), ",".join(str(i) for i in ids) or "<none>"))
    malformed = sum(1 for art in listing if art is None)
    if malformed:
        print("artifact listing: %d non-object entr%s kept as invalid placeholders"
              % (malformed, "y" if malformed == 1 else "ies"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
