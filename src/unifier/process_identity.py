"""Host process start stamps for matching a recorded PID to its owner."""

from pathlib import Path


def process_stamp(pid):
    """Return the start stamp of a live process, or None if unproven."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else int(fields[19])
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, TypeError, ValueError):
        return None
