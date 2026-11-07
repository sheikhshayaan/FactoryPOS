"""Keyboard use: Enter presses a dialog's main button, Esc closes it, Tab reaches buttons and Enter / Space presses them."""
import pytest

pytest.importorskip("tkinter")
from tests.test_ui_smoke import app, populated, no_blocking_confirm, login, dialogs, fill, _find  # noqa: E402,F401
from services import queries as q  # noqa: E402
from ui.widgets import Message, make_button, show_info  # noqa: E402


def _press(app, widget, key="<Return>"):
    widget.focus_force(); app.update()
    widget.event_generate(key); app.update()


def test_enter_in_a_field_saves_the_form(app):
    login(app); app.show("customers"); app.update()
    page = app.screen.page_cache["customers"]
    page.add(); app.update()
    d = dialogs(app)[-1]
    assert d.default_btn is d.submit_btn
    fill(d, name="Keyboard Co", phone="0300")
    _press(app, d.widgets["name"]._entry)                                        # Enter while the cursor is in the Name box
    assert "Keyboard Co" in [c["name"] for c in q.customers(app.ctx.conn)]
    assert not d.winfo_exists()                                                 # and the dialog closed


def test_the_cursor_starts_in_the_first_box(app):
    login(app); app.show("customers"); app.update()
    page = app.screen.page_cache["customers"]
    page.add(); app.update(); app.update_idletasks()
    d = dialogs(app)[-1]
    assert d.focus_get() is d.widgets["name"]._entry
    d.destroy()


def test_escape_closes_and_enter_does_not_break_multiline_boxes(app):
    login(app); app.show("expenses"); app.update()
    page = app.screen.page_cache["expenses"]
    page.reverse() if False else None
    page.expense(); app.update()
    d = dialogs(app)[-1]
    _press(app, d, "<Escape>")
    assert not d.winfo_exists()
    login_page = app.screen.page_cache["inventory"] if "inventory" in app.screen.page_cache else None
    app.show("inventory"); app.update()
    inv = app.screen.page_cache["inventory"]
    inv.issue(); app.update()
    d = dialogs(app)[-1]
    box = d.widgets["reason"]
    box.focus_force(); app.update()
    assert d._on_enter() is None and d.winfo_exists()                          # Enter inside the Reason box = new line, nothing is submitted
    d.destroy(); del login_page


def test_message_closes_with_enter(app):
    login(app)
    show_info(app, "Hello", "Some text"); app.update()
    m = _find(app, Message)[-1]
    _press(app, m)
    assert not m.winfo_exists()


def test_buttons_work_from_the_keyboard(app):
    login(app)
    hits = []
    b = make_button(app, "Go", lambda: hits.append(1), "secondary")
    b.pack(); app.update()
    assert str(b._canvas.cget("takefocus")) == "1"
    _press(app, b._canvas, "<Return>"); _press(app, b._canvas, "<space>")
    assert hits == [1, 1]
    b.configure(state="disabled"); _press(app, b._canvas, "<Return>")
    assert hits == [1, 1]                                                       # a disabled button stays disabled
    b.destroy()


def test_a_late_focus_call_on_a_closed_widget_is_not_reported(app, capsys, monkeypatch):
    """CustomTkinter schedules 'focus the widget that had it' after showing a window; if that widget was closed meanwhile Tk says
    'invalid command name'. That must be ignored quietly, while real errors are still reported."""
    import tkinter as tk
    login(app)
    dead = tk.Canvas(app); dead.pack(); app.update(); dead.destroy()
    app.after(1, dead.focus); app.after(30, app.quit if False else (lambda: None)); app.update(); app.after(40, lambda: None)
    import time; time.sleep(0.08); app.update()
    assert "invalid command name" not in capsys.readouterr().err
    seen = []
    monkeypatch.setattr(tk.Tk, "report_callback_exception", lambda self, *a: seen.append(a[1]))
    app.after(1, lambda: 1 / 0); time.sleep(0.05); app.update()
    assert any(isinstance(e, ZeroDivisionError) for e in seen)                 # a genuine error still gets through


def test_charts_never_hold_the_keyboard_focus(app):
    login(app); app.show("insights"); app.update()
    page = app.screen.page_cache["insights"]
    page.refresh(); app.update()
    w = page.canvases["bars"].get_tk_widget()
    w.focus_set(); app.update()
    assert app.focus_get() is not w                                            # focus_set on a chart does nothing
    page.refresh(); app.update()                                               # redrawing the charts raises no error
