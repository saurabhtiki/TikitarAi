# Vendored Vega-Lite runtime

These three files are the charting engine for the **exported dashboard** only. The app's own
screens (Chat with Data, report items) still draw with Plotly — see `analyst/charts.py`.

They are committed rather than installed because no Python package ships a usable browser
bundle: Altair carries only an 8 KB widget loader, and Streamlit's copies are webpack chunks.
The exported HTML has to run with no network, so the bytes must be inlined at build time.

| File | Version | Source |
|---|---|---|
| `vega.min.js` | 5.30.0 | https://cdn.jsdelivr.net/npm/vega@5.30.0/build/vega.min.js |
| `vega-lite.min.js` | 5.21.0 | https://cdn.jsdelivr.net/npm/vega-lite@5.21.0/build/vega-lite.min.js |
| `vega-embed.min.js` | 6.26.0 | https://cdn.jsdelivr.net/npm/vega-embed@6.26.0/build/vega-embed.min.js |

Load order matters: vega, then vega-lite, then vega-embed.

## Updating

Nothing watches these for you. An update is a deliberate act:

1. Download the three files at the *same* matching versions (vega-lite pins a vega range).
2. Update the table above.
3. Run `uv run pytest tests/ -k live_dashboard` — the export tests assert each file is
   present, non-empty, and free of a literal `</script>`, which is what keeps them safe to
   inline into a `<script>` tag.
4. Export a dashboard and open it with the network disabled.
