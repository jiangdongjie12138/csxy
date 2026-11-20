# simmeta.py - shared identity banner for the CSXY simulator services.
#
# The direction card requires every materially changed script to carry
# version +1, a unique fingerprint, and to self-print its own source
# sha1/size/mtime so that results from different script revisions are never
# silently compared.
#
import datetime, hashlib, os, sys


def self_identity(script_path):
    """Return (sha1, size, mtime_str) of the module that calls us."""
    p = os.path.abspath(script_path)
    st = os.stat(p)
    raw = open(p, "rb").read()
    return (hashlib.sha1(raw).hexdigest(), st.st_size,
            datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"))


def banner(name, version, fingerprint, extra=None):
    src = sys.argv[0]
    h, size, mtime = self_identity(src)
    lines = [
        "=" * 62,
        "CSXY simulated server  %s  v%s  fp=%s" % (name, version, fingerprint),
        "  source : %s" % src,
        "  sha1   : %s" % h,
        "  size   : %d   mtime: %s" % (size, mtime),
    ]
    if extra:
        for e in extra:
            lines.append("  " + str(e))
    lines.append("=" * 62)
    print("\n".join(lines), flush=True)