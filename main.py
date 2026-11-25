#!/usr/bin/env python3
"""The 3 AM Excuse — make.fun bounty entry.

Writes exactly one line (under 200 characters) in the voice of a
hardware wallet refusing to sign a transaction at 3 AM, to
``$MAKEFUN_OUTPUT_DIR/entry.txt`` (default ``/output``), with no
trailing newline.

The program is deterministic and uses only the Python standard
library. The write is atomic: a temporary file is created with
``O_EXCL`` inside the output directory, written in full (a loop, so
a short ``os.write`` can never silently truncate the entry),
fsynced and chmodded ``0644``, then moved into place with
:func:`os.replace`, followed by a directory fsync and a read-back
verification of both the bytes and the file mode. A failed run never
leaves a half-written ``entry.txt`` behind, and a stale temporary
file from an earlier run is cleaned up first (with retries) so
repeated runs stay byte-identical.

Revision notes (reviser, 25 Nov 2026, against builder HEAD 76b0b00)
-------------------------------------------------------------------
The builder's entrypoint was already strong; the revision closes
the gaps that were still open:

1. Loose validator: the single-line check only looked for ``\\n``
   and ``\\r``, so the Unicode line separators U+0085 (NEL),
   U+2028 and U+2029 passed as "one line" even though a line-based
   reader splits on them; the control-character check also tolerated
   tabs. The validator now rejects every C0/C1 control character
   (including tab), DEL, and a leading byte-order mark, and the
   single-line check uses :meth:`str.splitlines`, which splits on
   every Unicode line boundary.
2. Short write: the builder raised on a short ``os.write`` instead
   of finishing the write, so a transient short write failed the
   whole run. The write is now a loop over a :class:`memoryview`
   that keeps writing until every byte is out (a zero-byte write
   raises instead of looping forever), with the length assertion
   kept as a final guard.
3. Temp file leaked on write failure: if the write or fsync
   failed, the builder closed the descriptor but left the temp
   file behind. The temp file is now unlinked (best effort) on any
   failure inside the write step.
4. Mode outside the durability window: the builder fsynced the
   file and only then chmodded it to 0644, so the mode change was
   never covered by the fsync. The mode is now set with
   ``os.fchmod`` while the descriptor is still open, *before* the
   fsync, so one fsync covers both the bytes and the mode.
5. Post-publish verify had no cleanup: the module docstring
   promised a failed run never leaves a bad ``entry.txt`` behind,
   but if read-back verification failed *after* ``os.replace``,
   the wrong file stayed published. A verification failure now
   removes the published file (best effort) before re-raising.
   (A directory-fsync failure is different: the bytes are already
   correct, so the file is left in place and only the error is
   reported — deleting correct bytes would be strictly worse.)
6. ``resolve_output_dir`` was not defensive: the filesystem root
   ``/`` was accepted as an output directory, and a path that
   exists as a plain file only failed later with a confusing
   error. The root is now rejected outright, and after creation
   the path is verified to really be a directory.
7. Stale-temp sweep retried hopelessly: a stale entry that is a
   directory (not a file) can never be unlinked, yet the builder
   retried the unlink three times before failing. The sweep now
   checks with ``os.lstat`` first and fails immediately with a
   clear message when a stale entry is a directory.
8. A broken stdout (closed pipe) after a successful write used
   to turn success into failure. The success message is now
   best-effort; the exit code reflects the write, not the report.

Exit code is 0 on success. Any problem — an invalid entry, an
unusable output path, or a read-back mismatch — raises an explicit
exception with a descriptive message instead of failing silently.
"""

import os
import stat
import sys
import tempfile
import time

ENTRY_TEXT = (
    "At 3 AM, the vault sleeps, the keys dream, and I \u2014 the only "
    "one here who remembers your last 3 AM trade \u2014 say no."
)

MAX_CHARS = 200
DEFAULT_OUTPUT_DIR = "/output"
TMP_PREFIX = ".entry-"
TMP_SUFFIX = ".tmp"
CLEANUP_ATTEMPTS = 3
CLEANUP_RETRY_DELAY_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Validation: split into small, single-purpose checks so each failure
# mode gets its own explicit error instead of one opaque assertion.
# ---------------------------------------------------------------------------

def _reject_if_not_string(text):
    """Raise unless *text* is a real string."""
    if not isinstance(text, str):
        raise ValueError(
            "entry must be a string, got %s" % type(text).__name__
        )


def _reject_if_blank_or_too_long(text):
    """Raise unless *text* is non-blank and within the character gate."""
    if not text.strip():
        raise ValueError("entry must not be empty or only whitespace")
    if len(text) > MAX_CHARS:
        raise ValueError(
            "entry is %d characters, over the %d limit"
            % (len(text), MAX_CHARS)
        )


def _reject_if_control_character(text):
    """Raise if *text* contains a control or invisible format character.

    Every C0 control (U+0000-U+001F, including tab, newline and
    carriage return), DEL (U+007F), every C1 control (U+0080-U+009F,
    including NEL, U+0085) and the byte-order mark (U+FEFF) is
    rejected. Tabs were tolerated before, but a tab is still a
    control character in a one-line entry, and a BOM is invisible
    while inflating the character count the gate measures, so
    neither belongs in clean one-line UTF-8 text.
    """
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F or code == 0xFEFF:
            raise ValueError(
                "entry contains a control character: U+%04X" % code
            )


def _reject_if_not_single_line(text):
    """Raise if *text* spans more than one line.

    :meth:`str.splitlines` splits on every Unicode line boundary —
    ``\\n``, ``\\r``, ``\\r\\n`` and also U+0085, U+2028 and
    U+2029 — so a separator the control-character check cannot see
    (U+2028/U+2029 are not control characters) is still caught
    here instead of slipping through as a "single" line.
    """
    if len(text.splitlines()) > 1:
        raise ValueError("entry must be a single line")


def validate_entry(text):
    """Run every validation check on *text*."""
    _reject_if_not_string(text)
    _reject_if_blank_or_too_long(text)
    _reject_if_control_character(text)
    _reject_if_not_single_line(text)


# ---------------------------------------------------------------------------
# Output path handling.
# ---------------------------------------------------------------------------

def resolve_output_dir():
    """Return the output directory from the environment or the default.

    ``MAKEFUN_OUTPUT_DIR`` names the grader's mount point; ``/output``
    is the fallback the competition rules name. The directory itself
    is never deleted or recreated — only its contents are touched —
    because the grader mounts it. The filesystem root is rejected
    outright: it is never a legitimate mount point for this entry,
    and writing ``entry.txt`` there would be a misconfiguration,
    not a result.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR", "")
    path = raw.strip() or DEFAULT_OUTPUT_DIR
    path = os.path.abspath(path)
    if path == os.path.sep:
        raise ValueError(
            "refusing to use the filesystem root as the output directory"
        )
    return path


def resolve_output_path(output_dir):
    """Return the full path of the entry file inside *output_dir*."""
    return os.path.join(output_dir, "entry.txt")


def ensure_output_dir(output_dir):
    """Create *output_dir* when it does not exist yet.

    When the grader mounts the directory it already exists, so this
    is a no-op there; locally it lets the program run anywhere.
    The path is verified to really be a directory afterwards, so a
    plain file sitting where the directory should be fails here
    with a clear message instead of later, inside the write.
    """
    if os.path.exists(output_dir) and not os.path.isdir(output_dir):
        raise OSError(
            "output path %r exists and is not a directory" % output_dir
        )
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.isdir(output_dir):
        raise OSError(
            "output path %r exists and is not a directory" % output_dir
        )


# ---------------------------------------------------------------------------
# Atomic write: stale-temp cleanup, O_EXCL temp file, write loop, fchmod
# before fsync, os.replace, directory fsync, read-back verification.
# ---------------------------------------------------------------------------

def _cleanup_stale_temps(output_dir):
    """Remove stale temporary files from earlier runs, with retries.

    Only files this program could have created (its own prefix and
    suffix) are removed, and only inside the output directory. A
    leftover that cannot be removed after the retries is an error,
    because it would otherwise shadow a later run's temp name. A
    stale entry that is a directory is not retried at all: it can
    never be unlinked, so it fails immediately with a clear message
    instead of wasting three attempts on a hopeless unlink.
    """
    for name in os.listdir(output_dir):
        if not name.startswith(TMP_PREFIX):
            continue
        if not name.endswith(TMP_SUFFIX):
            continue
        stale = os.path.join(output_dir, name)
        if os.path.isdir(stale) and not os.path.islink(stale):
            raise OSError(
                "stale temp entry %r is a directory, refusing to remove it"
                % stale
            )
        last_error = None
        for _ in range(CLEANUP_ATTEMPTS):
            try:
                os.unlink(stale)
                last_error = None
                break
            except FileNotFoundError:
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                time.sleep(CLEANUP_RETRY_DELAY_SECONDS)
        if last_error is not None:
            raise OSError(
                "could not remove stale temp file %r: %s"
                % (stale, last_error)
            )


def _write_temp_file(output_dir, data):
    """Write *data* to a fresh temp file in *output_dir* and return it.

    The file is created with ``O_EXCL`` so it can never clobber an
    existing file. The write is a loop over a memoryview, so a short
    ``os.write`` is finished rather than reported as a failure (a
    zero-byte write raises instead of looping forever). The mode is
    set with ``os.fchmod`` to 0644 *before* the fsync, so the single
    fsync makes both the bytes and the mode durable, and the grader
    (a different user) can read the file. Any failure here unlinks
    the temp file again, so a failed write leaves nothing behind.
    """
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=output_dir
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(
                    "write to %r returned 0 bytes" % tmp_path
                )
            view = view[written:]
        os.fchmod(fd, 0o644)
        os.fsync(fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.close(fd)
    return tmp_path


def _fsync_directory(output_dir):
    """Fsync *output_dir* so the rename inside it is durable."""
    dir_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _verify_written_file(output_path, expected_data):
    """Read *output_path* back and check its bytes and mode."""
    with open(output_path, "rb") as handle:
        actual_data = handle.read()
    if actual_data != expected_data:
        raise OSError(
            "read-back mismatch for %r: got %d bytes, expected %d"
            % (output_path, len(actual_data), len(expected_data))
        )
    mode = stat.S_IMODE(os.stat(output_path).st_mode)
    if mode != 0o644:
        raise OSError(
            "output file %r has mode %04o, expected 0644"
            % (output_path, mode)
        )


def write_entry(output_dir, text):
    """Write *text* atomically to ``entry.txt`` in *output_dir*."""
    data = text.encode("utf-8")
    ensure_output_dir(output_dir)
    _cleanup_stale_temps(output_dir)
    tmp_path = _write_temp_file(output_dir, data)
    output_path = resolve_output_path(output_dir)
    try:
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        _fsync_directory(output_dir)
    except BaseException:
        # The bytes are already correct; only the durability
        # confirmation failed. Leave the file in place and report
        # the error — deleting correct bytes would be worse.
        raise
    try:
        _verify_written_file(output_path, data)
    except BaseException:
        # The published bytes are wrong: remove the file so a
        # failed run never leaves a bad entry.txt behind.
        try:
            os.unlink(output_path)
        except OSError:
            pass
        raise
    return output_path


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main(argv=None):
    """Validate the entry and write it to the output directory."""
    del argv  # no command-line arguments are used
    validate_entry(ENTRY_TEXT)
    output_dir = resolve_output_dir()
    output_path = write_entry(output_dir, ENTRY_TEXT)
    try:
        sys.stdout.write(
            "wrote %d characters to %s\n" % (len(ENTRY_TEXT), output_path)
        )
    except OSError:
        # The write already succeeded; a closed stdout must not
        # turn success into failure.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
