"""Finds the prebuilt HTML dashboards shipped in `static/reports/` (phase 62).

The folder is the whole registry: drop `sales_report.html` in and "Sales Report" appears in
Utilities → Reports. It sits under `static/` so Streamlit's static serving can open each file
as its own page in a new tab, at `app/static/reports/<file>`.
"""

import logging
import re
from pathlib import Path

from utils.static_files import static_url

logger = logging.getLogger(__name__)

DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "static" / "reports"
HTML_SUFFIXES = {".html", ".htm"}


def display_name(file_path: Path) -> str:
    """`sales_report.html` -> "Sales Report", `CashFlowDashboard.html` -> "Cash Flow Dashboard"."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", file_path.stem)
    return " ".join(spaced.replace("_", " ").replace("-", " ").split()).title()


def dashboard_url(file_path: Path) -> str:
    """The static-serving URL that opens the file in its own tab."""
    return static_url("reports", file_path)


def list_dashboards(folder: Path = DASHBOARDS_DIR) -> dict[str, Path]:
    """Report name -> HTML file, sorted by name. A missing or unreadable folder gives {}."""
    try:
        html_files = [
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in HTML_SUFFIXES
        ]
    except FileNotFoundError:
        return {}
    except OSError:
        logger.exception("Couldn't read the prebuilt dashboards folder %s.", folder)
        return {}
    return {display_name(path): path for path in sorted(html_files, key=display_name)}
