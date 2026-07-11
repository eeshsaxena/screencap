"""One atomic-write primitive shared by the corpus write paths (review fix #12).

Corpus artifacts (encrypted stills, redacted stills, the per-chunk scrub-state
marker) were each written by a hand-rolled ``mkstemp`` + write + ``chmod`` +
``os.replace`` copy that had drifted apart — one copy silently dropped the
``fsync``. This is the single source so the durability + ``0600`` + crash-safety
discipline can't diverge again.
"""

from __future__ import annotations

import os
import tempfile


def atomic_write_0600(
    dest: str | os.PathLike[str],
    data: bytes,
    *,
    fsync: bool = True,
    suffix: str = ".part",
) -> None:
    """Atomically write ``data`` to ``dest`` at mode ``0600``.

    ``mkstemp`` in the destination directory, write (``fsync`` by default), chmod
    ``0600``, then ``os.replace`` — so a crash leaves either the old file or the
    complete new one, never a torn/partial (or, for an encrypted still, a plaintext)
    result. On any failure the temp file is cleaned up and the error re-raised.
    ``fsync=False`` skips the flush for data whose loss on a hard crash is
    acceptable (e.g. still recoverable from another source)."""
    dest = os.fspath(dest)
    directory = os.path.dirname(dest) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=suffix)
    closed = False
    try:
        os.write(fd, data)
        if fsync:
            os.fsync(fd)
        os.close(fd)
        closed = True
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = ["atomic_write_0600"]
