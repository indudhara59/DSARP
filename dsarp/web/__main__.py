import sys

# Keep this file valid for old Pythons (e.g. 3.6 on HPC login nodes) so the
# version check below runs instead of a SyntaxError.
if sys.version_info < (3, 10):
    sys.stderr.write(
        "The DSARP web interface needs Python 3.10 or newer; this is Python %d.%d.\n"
        "On an HPC login node load a newer Python first, for example:\n"
        "    module avail lang/Python          (or: module spider Python)\n"
        "    module load lang/Python/<version 3.10 or newer>\n" % sys.version_info[:2])
    raise SystemExit(1)

from .server import main  # noqa: E402

raise SystemExit(main())
