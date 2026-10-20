"""Domain scaffold testleri — domain.yaml + CONTEXT.md + contracts/ protokolü."""
import textwrap

import pytest

from factory.domain import DomainError, load_domain, scaffold_domain


def test_scaffold_creates_package(tmp_path):
    d = scaffold_domain(tmp_path, name="billing", profiles=["coder-b"],
                        depends_on=["auth"], critical=True,
                        responsibilities=["faturalama"])
    assert (d.path / "domain.yaml").exists()
    assert (d.path / "CONTEXT.md").exists()
    assert (d.path / "contracts").is_dir()
    loaded = load_domain(tmp_path, "billing")
    assert loaded.critical and loaded.depends_on == ["auth"]
    assert loaded.profiles == ["coder-b"]


def test_load_missing_domain_raises(tmp_path):
    with pytest.raises(DomainError):
        load_domain(tmp_path, "yok")


def test_invalid_yaml_raises(tmp_path):
    d = scaffold_domain(tmp_path, name="x", profiles=["c"])
    (d.path / "domain.yaml").write_text("name: [bozuk")
    with pytest.raises(DomainError):
        load_domain(tmp_path, "x")


def test_name_mismatch_raises(tmp_path):
    d = scaffold_domain(tmp_path, name="auth", profiles=["c"])
    (d.path / "domain.yaml").write_text(textwrap.dedent("""
        name: baska-bir-sey
        profiles: [c]
    """))
    with pytest.raises(DomainError):
        load_domain(tmp_path, "auth")


def test_scaffold_rejects_bad_names(tmp_path):
    for bad in ["", "Üst-Klasör", "../kaçış", "a b"]:
        with pytest.raises(DomainError):
            scaffold_domain(tmp_path, name=bad, profiles=["c"])


def test_export_contract(tmp_path):
    scaffold_domain(tmp_path, name="auth", profiles=["c"])
    d = load_domain(tmp_path, "auth")
    p = d.export_contract("api.yaml", "openapi: 3.0.0\n")
    assert p.exists() and "openapi" in p.read_text()


def test_context_update_appends_history(tmp_path):
    scaffold_domain(tmp_path, name="auth", profiles=["c"])
    d = load_domain(tmp_path, "auth")
    d.update_context("# Durum\nYeni karar: JWT.\n", by="coder-c")
    text = (d.path / "CONTEXT.md").read_text()
    assert "JWT" in text and "coder-c" in text
