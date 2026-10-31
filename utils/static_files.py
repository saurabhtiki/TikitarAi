"""The one rule for links to files under `static/`, served by Streamlit's static serving."""

from pathlib import Path
from urllib.parse import quote


def static_url(subfolder: str, file_path: Path) -> str:
    """`static_url("help", Path("run task.html"))` -> "app/static/help/run%20task.html"."""
    return f"app/static/{subfolder}/{quote(file_path.name)}"
