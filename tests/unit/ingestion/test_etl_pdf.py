from unittest import mock

import pytest

from lies.etl.normalize.pdf import extract_text, extract_text_ocr


def test_extract_text_uses_pymupdf() -> None:
    fake_doc = mock.Mock()
    fake_doc.__iter__ = mock.Mock(return_value=iter([mock.Mock(get_text=lambda: "p1")]))
    with mock.patch("pymupdf.open", return_value=fake_doc):
        assert extract_text(b"%PDF") == "p1"


def test_extract_text_ocr_calls_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_doc = mock.Mock()
    fake_page = mock.Mock()
    fake_pix = mock.Mock()
    fake_pix.tobytes = mock.Mock(return_value=b"\x89PNG")
    fake_page.get_pixmap = mock.Mock(return_value=fake_pix)
    fake_doc.__iter__ = mock.Mock(return_value=iter([fake_page]))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock.Mock(returncode=0, stdout=b"ocr text"))
    with mock.patch("pymupdf.open", return_value=fake_doc):
        out = extract_text_ocr(b"%PDF")
    assert "ocr text" in out


def test_extract_text_ocr_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_doc = mock.Mock()
    fake_page = mock.Mock()
    fake_pix = mock.Mock()
    fake_pix.tobytes = mock.Mock(return_value=b"\x89PNG")
    fake_page.get_pixmap = mock.Mock(return_value=fake_pix)
    fake_doc.__iter__ = mock.Mock(return_value=iter([fake_page]))
    monkeypatch.setattr("pymupdf.open", lambda *a, **kw: fake_doc)

    def fake_run(*a, **kw):
        raise FileNotFoundError("tesseract")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(FileNotFoundError):
        extract_text_ocr(b"%PDF")
