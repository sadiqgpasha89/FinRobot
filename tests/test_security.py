import http.server
import threading

import pytest

from finrobot.report.render import prose_html, to_pdf
from finrobot.tools.tables import Raw, html_table


def test_model_text_cannot_inject_markup_into_the_report():
    out = prose_html("Revenue rose [F1].\n\n<script>alert(1)</script> <img src='http://169.254.169.254/x'> & more")
    assert "<script" not in out and "<img" not in out and "&lt;script&gt;" in out
    assert "<sup class='cite'>F1</sup>" in out      # citations still render


def test_table_cells_are_escaped_even_when_they_start_with_a_tag():
    """Regression: a cell starting with '<' bypassed escaping, and risk-table cells come straight from model output."""
    html = html_table(["Risk"], [["<script>alert(1)</script>"], ["<b>bold</b> text"]], "caption <i>")
    assert "<script" not in html and "&lt;script&gt;" in html and "<b>" not in html and "&lt;i&gt;" in html
    assert "<b>ok</b>" in html_table(["h"], [[Raw("<b>ok</b>")]])


def test_rendering_a_pdf_never_makes_a_network_request():
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/secret.png"
        pdf, pages = to_pdf(f"<html><body><h1>Report</h1><img src='{url}'><link rel='stylesheet' href='{url}'></body></html>")
    finally:
        server.shutdown()
    assert pdf.startswith(b"%PDF") and pages >= 1
    assert hits == []


def test_object_keys_cannot_escape_the_local_store(tmp_path, monkeypatch):
    from finrobot.config import settings
    from finrobot.data import objects
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    monkeypatch.setattr(objects, "_s3", lambda: None)
    assert objects.put("runs/r1/a.txt", b"x").endswith("runs/r1/a.txt")
    assert objects.get("runs/r1/a.txt") == b"x"
    with pytest.raises(ValueError):
        objects.put("../../outside.txt", b"x")


def test_object_store_reconnects_after_a_cooldown(monkeypatch):
    import time

    from finrobot.data import objects
    objects.reset()
    monkeypatch.setattr(objects, "RETRY_AFTER", 0.05)
    attempts = []

    class Boom:
        def __init__(self, *a, **k):
            attempts.append(1)
            raise ConnectionError("down")
    import minio
    monkeypatch.setattr(minio, "Minio", Boom)
    assert objects._s3() is None and objects._s3() is None and len(attempts) == 1    # cooling down: no hammering
    time.sleep(0.06)
    assert objects._s3() is None and len(attempts) == 2                               # tries again later instead of failing forever
    objects.reset()


def test_run_artifacts_resolve_only_inside_the_run_directory(tmp_path, monkeypatch):
    from finrobot import runs
    from finrobot.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "secret.txt").write_text("s")
    assert runs.artifact_path("r1", "a.pdf") is not None
    for run_id, name in (("r1", "../../secret.txt"), ("..", "secret.txt"), ("r1", ".."), ("r1/..", "a.pdf"), ("nope", "a.pdf")):
        assert runs.artifact_path(run_id, name) is None
    assert not (tmp_path / "runs" / "nope").exists()          # a read never creates a directory (an unknown id used to leave one behind)
    with pytest.raises(ValueError):
        runs.run_dir("../escape")
