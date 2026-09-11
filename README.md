# TikitarAi

AI-powered data analysis and visualization, built on Agno agents with a Streamlit UI.
See `docs/requirements.md` for scope and `CLAUDE.md` for the working conventions.

## Running it hosted

The report saves each pinned chart as a picture so the HTML and Excel downloads can carry
it. That is done by kaleido, which drives a headless browser — a laptop already has one, a
cloud container does not. Without a browser the download still works, but each chart is
replaced by its table and a notice saying why.

`packages.txt` is what fixes that on Streamlit Cloud: it asks the host to apt-install
`chromium` before the app starts. Keep the file to plain package names, one per line — the
host feeds it straight to apt, so a comment line reads as a package it cannot find.

On any other host, install a Chrome or Chromium and make sure it is on `PATH`, or set
`BROWSER_PATH` to the executable.
