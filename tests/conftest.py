"""Shared pytest setup.

`AppTest.from_file("app_pages/<page>.py")` runs a page with `app_pages/` first on the import
path, where `app_pages/dashboard.py` hides the top-level `dashboard` package. The real app
starts from the project root and never sees this; a test file run on its own did, failing
with "'dashboard' is not a package". Importing the package here, before any page runs,
puts the right module in `sys.modules` first.
"""

import dashboard.session  # noqa: F401
