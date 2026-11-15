import pytest

from app.config import ConfigError, mail_settings

ENV = ("EXTRANALYZER_GMAIL_USER", "EXTRANALYZER_GMAIL_APP_PASSWORD", "EXTRANALYZER_GMAIL_APP_PASSWORD_FILE",
       "EXTRANALYZER_RECEIPT_LABELS", "EXTRANALYZER_MAIL_INTERVAL_MIN")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_mail_settings_strip_spaces_and_defaults(monkeypatch, tmp_path):
    f = tmp_path / "pw"
    f.write_text("abcd efgh ijkl mnop\n")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD_FILE", str(f))
    m = mail_settings()
    assert m.user == "test@example.invalid"
    assert m.app_password == "abcdefghijklmnop"
    assert m.labels == (("Super U/Tickets de caisses", "superu"), ("intermarche/Tickets de caisses", "intermarche"))
    assert m.interval_min == 60


def test_mail_settings_custom_labels_and_interval(monkeypatch):
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD", "abcd efgh")
    monkeypatch.setenv("EXTRANALYZER_RECEIPT_LABELS", "A/B=superu; C=intermarche;")
    monkeypatch.setenv("EXTRANALYZER_MAIL_INTERVAL_MIN", "15")
    m = mail_settings()
    assert m.app_password == "abcdefgh"
    assert m.labels == (("A/B", "superu"), ("C", "intermarche"))
    assert m.interval_min == 15


@pytest.mark.parametrize("bad", ["0", "-5", "x"])
def test_mail_settings_bad_interval(monkeypatch, bad):
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD", "abcd")
    monkeypatch.setenv("EXTRANALYZER_MAIL_INTERVAL_MIN", bad)
    with pytest.raises(ConfigError):
        mail_settings()


def test_mail_settings_bad_labels(monkeypatch):
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD", "abcd")
    monkeypatch.setenv("EXTRANALYZER_RECEIPT_LABELS", "sans-enseigne")
    with pytest.raises(ConfigError):
        mail_settings()


def test_mail_settings_disabled_when_empty(monkeypatch, tmp_path):
    assert mail_settings() is None                                  # rien de défini
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD", "abcd")
    assert mail_settings() is None                                  # pas d'utilisateur
    monkeypatch.delenv("EXTRANALYZER_GMAIL_APP_PASSWORD")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    f = tmp_path / "pw"
    f.write_text("  \n")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD_FILE", str(f))
    assert mail_settings() is None                                  # fichier vide


def test_mail_settings_both_password_sources_is_error(monkeypatch, tmp_path):
    f = tmp_path / "pw"
    f.write_text("abcd")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD", "abcd")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD_FILE", str(f))
    with pytest.raises(ConfigError):
        mail_settings()


def test_mail_secrets_not_in_repr(monkeypatch, tmp_path):
    from pathlib import Path
    from app.config import Settings
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    m = mail_settings()
    s = Settings(data_dir=Path("d"), password_hash=b"x", mail=m)
    for text in (repr(m), repr(s)):
        assert "abcdefghijklmnop" not in text and "test@example.invalid" not in text
