class TransformError(Exception):
    """Base class for every failure raised by the transform package."""


class InvalidOperationError(TransformError):
    """A step names an operation that isn't in the registry.

    Separate from InvalidStepParamsError because the two are recoverable in different
    ways: unknown parameters can be corrected in the form, an unknown operation means the
    step was written by a version of the app that knew something this one doesn't.
    """


class InvalidStepParamsError(TransformError):
    """A step's inputs or parameters are invalid.

    Raised at validation time, before a step is admitted into a pipeline, so that a
    stored pipeline is well-formed by construction — the invariant phase 26's saved
    pipelines will depend on when they replay against next month's upload.
    """


class DuplicateFrameNameError(TransformError):
    """A step would create a named table under a name that is already taken.

    Its own class rather than a flavour of InvalidStepParamsError because the page acts on
    it differently: the name box is re-focused with a suggestion, rather than the whole
    form being marked wrong.
    """


class FrameNotFoundError(TransformError):
    """A step names a table that isn't in the workspace."""


class TransformExportError(TransformError):
    """The workbook couldn't be built (nothing selected, or Excel's limits exceeded).

    Distinct from `cleaner.exceptions.ExportError`, which this package's export wraps: a
    page catching TransformError should not have to also know the cleaner's hierarchy.
    """


class PipelineStorageError(TransformError):
    """A saved pipeline couldn't be written, read back, or found.

    Its own class rather than `InvalidStepParamsError` because the two are recoverable in
    different ways: bad parameters are corrected in the form, a storage failure is nothing
    the user can fix from the page and is reported as "we couldn't save this" instead.
    """
