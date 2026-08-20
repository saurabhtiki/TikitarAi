"""Failures a Task can have, as its own hierarchy (requirement 7.6).

One base class the page can catch, and two specific ones, following `checks/exceptions.py`.
Every message here is written to be shown to a user unchanged — the page prints these, so
"couldn't be read" beats a stack trace they can do nothing with.
"""


class TaskError(Exception):
    """Anything that went wrong with a Task."""


class TaskStorageError(TaskError):
    """A Task couldn't be saved, loaded, listed or deleted.

    Covers both halves of storage — the SQLite row and the JSON inside it — because from the
    page's point of view they are one failure: the recipe didn't survive.
    """


class TaskCaptureError(TaskError):
    """A Task couldn't be assembled from what is on screen.

    Raised before anything is written, so the session is untouched when it happens.
    """
