"""Pressing **Load** for a test that just put files in the uploader.

Files wait in the uploader until the user presses Load — see `app_pages/setup_view.py`
and `tests/test_setup_upload_gate.py`, which is where that behaviour is actually asserted.
Every other page test only wants "a session with a table loaded", so it says so through
here rather than knowing about the button.

Silent when there is no button: a run where the files were already confirmed, or where the
uploader is on a hidden mount, is an ordinary state rather than a failure.
"""


def load_uploaded_files(app):
    """Runs the app, presses Load if it is waiting, and returns the app."""
    app.run()
    waiting = [button for button in app.button if button.key == "de_load_files"]
    if waiting:
        waiting[0].click().run()
    return app
