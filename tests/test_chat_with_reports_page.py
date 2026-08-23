"""AppTest coverage for the Chat with Reports page (Phase 13).

Shaped like `test_chat_with_data_page.py`: a real database under `tmp_path`, the page
driven through `AppTest`, and the model call stubbed — what is under test is the page's
behaviour around an answer, never the model's answer itself.

The two behaviours that matter most here are the ones this page does *differently* from
Chat with Data, and both are about the data being shared:

- a message that would change a column is refused, rather than rewriting rows every other
  user is reading;
- the agent is handed the report's **saved** schema — the tables, the column meanings and
  the links — rather than whatever the session happens to have loaded.
"""

from pathlib import Path

import duckdb
import pandas as pd
from streamlit.testing.v1 import AppTest

from analyst import routing
from analyst.pipeline import Answer
from auth.db import init_db, seed_default_admin
from engine.dictionary import ColumnEntry
from engine.relationships import Relationship
from llm.db import create_profile, init_llm_table
from reports import session as reports_session
from reports.db import init_report_datasets_table, record_dataset
from reports.model import ReportSetup, StoredTable
from reports.store import open_store, write_tables
from tasks.db import init_tasks_table, save_task
from tasks.model import Task

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "chat_with_reports.py")


def _setup() -> ReportSetup:
    return ReportSetup(
        tables=[StoredTable("sales", 3)],
        dictionary=[
            ColumnEntry("sales", "amount", "DOUBLE", "money", "What the sale was worth", ["revenue"]),
            ColumnEntry("sales", "cust_id", "INTEGER", "identifier", "Who bought it"),
        ],
        relationships=[Relationship("sales", "cust_id", "customer", "cust_id")],
    )


def _seed_report(tmp_path, name="Monthly sales") -> int:
    """A saved report with data on disk, exactly as a finished run would leave it."""
    init_db()
    seed_default_admin()
    init_llm_table()
    init_tasks_table()
    init_report_datasets_table()

    task = save_task(1, Task(name=name))

    source = duckdb.connect(":memory:")
    source.execute(
        "CREATE TABLE sales AS SELECT * FROM (VALUES (1, 1, 100.0), (2, 2, 250.0), (3, 1, 50.0)) "
        "AS t(sale_id, cust_id, amount)"
    )
    store = open_store(task.task_id)
    write_tables(store, source, ["sales"])
    store.close()
    source.close()

    record_dataset(task.task_id, _setup(), "Ravi")
    return task.task_id


def _make_app(tmp_path, monkeypatch, *, with_model=True):
    monkeypatch.chdir(tmp_path)
    task_id = _seed_report(tmp_path)
    if with_model:
        create_profile(1, "Stub", "local", "http://localhost:1234/v1", None, "stub-model")

    app = AppTest.from_file(PAGE_PATH, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.run()
    return app, task_id


def _open_report(app, task_id):
    app.button(key=f"cr_open_{task_id}").click().run()
    return app


# --------------------------------------------------------------------------------------
# The picker
# --------------------------------------------------------------------------------------


def test_a_report_with_saved_data_is_offered(tmp_path, monkeypatch):
    app, task_id = _make_app(tmp_path, monkeypatch)

    assert app.button(key=f"cr_open_{task_id}") is not None
    assert any("Data last refreshed" in caption.value for caption in app.caption)


def test_nothing_to_chat_with_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    init_tasks_table()
    init_report_datasets_table()

    app = AppTest.from_file(PAGE_PATH, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["role"] = "normal_user"
    app.run()

    assert any("No report has any data saved yet" in info.value for info in app.info)


def test_opening_a_report_shows_its_tables_and_when_it_was_refreshed(tmp_path, monkeypatch):
    app, task_id = _make_app(tmp_path, monkeypatch)
    _open_report(app, task_id)

    captions = " ".join(caption.value for caption in app.caption)
    assert "Tables you can ask about: sales" in captions
    assert "by Ravi" in captions


# --------------------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------------------


def test_the_agent_is_given_the_reports_saved_schema(tmp_path, monkeypatch):
    """Column meanings and links reach the model, or its SQL would be guesswork."""
    seen = {}

    def fake_answer(profile, connection, schema_context, question, **kwargs):
        seen["context"] = schema_context
        seen["rows"] = connection.execute("SELECT count(*) FROM sales").fetchone()[0]
        return Answer(question=question, text="Three sales.")

    monkeypatch.setattr("analyst.pipeline.answer", fake_answer)

    app, task_id = _make_app(tmp_path, monkeypatch)
    _open_report(app, task_id)
    app.chat_input(key="cr_chat_input").set_value("How many sales?").run()

    assert "Table sales:" in seen["context"]
    assert "What the sale was worth" in seen["context"]
    assert "sales.cust_id = customer.cust_id" in seen["context"]
    # And it is genuinely reading the saved file, not an empty session engine.
    assert seen["rows"] == 3


def test_an_answer_is_shown_with_its_sql(tmp_path, monkeypatch):
    def fake_answer(profile, connection, schema_context, question, **kwargs):
        return Answer(
            question=question,
            text="Sales came to 400.",
            sql="SELECT sum(amount) FROM sales",
            frame=pd.DataFrame({"total": [400.0]}),
            outputs={routing.OUTPUT_DATAFRAME},
        )

    monkeypatch.setattr("analyst.pipeline.answer", fake_answer)

    app, task_id = _make_app(tmp_path, monkeypatch)
    _open_report(app, task_id)
    app.chat_input(key="cr_chat_input").set_value("Total sales?").run()

    assert any("Sales came to 400." in markdown.value for markdown in app.markdown)
    assert len(app.dataframe) == 1


def test_a_column_change_is_refused_without_calling_the_model(tmp_path, monkeypatch):
    """Shared data: one person's edit would rewrite what everyone else is reading."""
    called = {"count": 0}

    def fake_answer(*args, **kwargs):
        called["count"] += 1
        return Answer(question="", text="should not happen")

    monkeypatch.setattr("analyst.pipeline.answer", fake_answer)

    app, task_id = _make_app(tmp_path, monkeypatch)
    _open_report(app, task_id)
    app.chat_input(key="cr_chat_input").set_value("Add a column tax = 10% of amount").run()

    assert called["count"] == 0
    assert any("can only read" in error.value for error in app.error)


def test_the_shared_data_is_untouched_by_a_refused_change(tmp_path, monkeypatch):
    monkeypatch.setattr("analyst.pipeline.answer", lambda *args, **kwargs: Answer(question="", text=""))

    app, task_id = _make_app(tmp_path, monkeypatch)
    _open_report(app, task_id)
    app.chat_input(key="cr_chat_input").set_value("Delete the blank rows").run()

    columns = [row[0] for row in duckdb.connect(str(tmp_path / "data" / "reports" / f"report_{task_id}.duckdb"))
               .execute("DESCRIBE sales").fetchall()]
    assert columns == ["sale_id", "cust_id", "amount"]


def test_without_a_session_model_the_input_is_disabled(tmp_path, monkeypatch):
    app, task_id = _make_app(tmp_path, monkeypatch, with_model=False)
    _open_report(app, task_id)

    assert app.chat_input(key="cr_chat_input").disabled is True


def test_clearing_the_chat_leaves_the_report_open(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "analyst.pipeline.answer", lambda *args, **kwargs: Answer(question="q", text="an answer")
    )

    app, task_id = _make_app(tmp_path, monkeypatch)
    _open_report(app, task_id)
    app.chat_input(key="cr_chat_input").set_value("Total sales?").run()
    app.button(key="cr_clear").click().run()

    assert app.session_state[reports_session.CR_MESSAGES_KEY] == []
    assert app.session_state[reports_session.CR_REPORT_KEY]["task_id"] == task_id


def test_going_back_closes_the_conversation(tmp_path, monkeypatch):
    app, task_id = _make_app(tmp_path, monkeypatch)
    _open_report(app, task_id)
    app.button(key="cr_back").click().run()

    assert reports_session.CR_REPORT_KEY not in app.session_state
    assert app.button(key=f"cr_open_{task_id}") is not None
