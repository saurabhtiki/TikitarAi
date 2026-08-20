"""Exceptions raised while running a saved Task (requirement 8).

One hierarchy per package, as every other package here has: the page catches `TaskRunError`
and knows the message on it is written for the person reading the screen.
"""


class TaskRunError(Exception):
    """A replay could not be carried out at all.

    Deliberately rare. Almost everything that goes wrong during a run is *one step* failing —
    a statement that no longer matches the data, a provider that didn't answer — and those
    are recorded as a `StepResult` and reported in the summary rather than raised, because a
    run that produced eleven of twelve report items is worth having. This is for the failures
    that stop the run itself: no tables loaded, or the setup refusing to apply.
    """


class TaskRunSetupError(TaskRunError):
    """The recorded setup couldn't be put back onto the loaded tables (requirement 8.2 step 1).

    Separate because it is the one failure where nothing has run yet, so the page can say
    "nothing was changed" rather than leaving the user wondering what half happened.
    """
