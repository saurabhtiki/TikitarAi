class DataCleanerError(Exception):
    """Base class for every failure raised by the cleaner package."""


class FileParseError(DataCleanerError):
    """An uploaded file couldn't be read or decoded."""


class UnsupportedFileTypeError(FileParseError):
    """The uploaded file's extension isn't one the cleaner can read (e.g. legacy .xls)."""


class SheetNotFoundError(FileParseError):
    """A requested worksheet doesn't exist in the workbook."""


class InvalidStepError(DataCleanerError):
    """A cleaning step's action or parameters are invalid.

    Raised at validation time, before a step is admitted into a recipe, so that a
    stored recipe is well-formed by construction — an invariant Stage 7 (Task Builder)
    depends on when it serializes recipes for later replay.
    """


class ExportError(DataCleanerError):
    """The cleaned workbook couldn't be built (nothing to export, or Excel's limits exceeded)."""


class TemplateStorageError(DataCleanerError):
    """A cleaning template couldn't be saved, loaded, listed or deleted.

    Covers both halves of storage — the SQLite row and the JSON inside it — because from
    the page's point of view they are one failure: the recipe didn't survive. Mirrors
    `tasks.exceptions.TaskStorageError`, whose `cleaner/db.py` is copied from.
    """
