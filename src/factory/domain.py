"""Domain paketi — domain.yaml + CONTEXT.md + contracts/ (ARCHITECTURE §2, ADR-003).

Domain'ın sahiplik paketi diskte yaşar; SQLite'taki domains tablosu bunun
çalışma-zamanı görüntüsüdür. Tek yazan domain agent'ıdır.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

CONTEXT_TEMPLATE = """# {name} — Domain Context

Sahip: domain agentı ({profiles}). Bu dosyayı yalnız domain agent'ı yazar.

## Güncel durum
(yazılacak)

## Kararlar
(yok)

## Bilinen tuzaklar
(yok)
"""


class DomainError(Exception):
    pass


@dataclass
class Domain:
    name: str
    root: Path                      # domains/<name>/
    profiles: list[str]
    depends_on: list[str] = field(default_factory=list)
    critical: bool = False
    paths: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    max_parallel_workers: int = 1

    @property
    def path(self) -> Path:
        return self.root

    def export_contract(self, filename: str, content: str) -> Path:
        if "/" in filename or filename.startswith("."):
            raise DomainError(f"geçersiz contract dosya adı: {filename}")
        p = self.root / "contracts" / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def update_context(self, new_body: str, by: str) -> None:
        """CONTEXT.md gövdesini değiştir; history altına imza satırı ekle."""
        ctx = self.root / "CONTEXT.md"
        text = new_body.rstrip() + f"\n\n---\n_son güncelleme: {by}_\n"
        ctx.write_text(text)


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name or ""):
        raise DomainError(f"geçersiz domain adı: {name!r} "
                          "(küçük harf/rakam/tire, 1-32)")


def scaffold_domain(base: Path, name: str, profiles: list[str],
                    depends_on: list[str] | None = None,
                    critical: bool = False, paths: list[str] | None = None,
                    responsibilities: list[str] | None = None,
                    max_parallel_workers: int = 1) -> Domain:
    _validate_name(name)
    if not profiles:
        raise DomainError("en az bir profil şart")
    root = Path(base) / "domains" / name
    if (root / "domain.yaml").exists():
        raise DomainError(f"domain mevcut: {name}")
    root.mkdir(parents=True)
    (root / "contracts").mkdir()
    spec = {"name": name, "profiles": profiles,
            "depends_on": depends_on or [], "critical": critical,
            "paths": paths or [f"src/{name}/**"],
            "responsibilities": responsibilities or [],
            "max_parallel_workers": max_parallel_workers}
    (root / "domain.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True, sort_keys=False))
    (root / "CONTEXT.md").write_text(
        CONTEXT_TEMPLATE.format(name=name, profiles=", ".join(profiles)))
    return Domain(name=name, root=root, profiles=profiles,
                  depends_on=depends_on or [], critical=critical,
                  paths=spec["paths"],
                  responsibilities=responsibilities or [],
                  max_parallel_workers=max_parallel_workers)


def load_domain(base: Path, name: str) -> Domain:
    _validate_name(name)
    root = Path(base) / "domains" / name
    yml = root / "domain.yaml"
    if not yml.exists():
        raise DomainError(f"domain yok: {name} ({yml})")
    try:
        spec = yaml.safe_load(yml.read_text())
    except yaml.YAMLError as e:
        raise DomainError(f"bozuk domain.yaml ({name}): {e}") from e
    if not isinstance(spec, dict):
        raise DomainError(f"domain.yaml sözlük değil: {name}")
    if spec.get("name") != name:
        raise DomainError(f"domain.yaml adı ({spec.get('name')!r}) "
                          f"klasör adıyla ({name!r}) uyuşmuyor")
    if not spec.get("profiles"):
        raise DomainError(f"profiles boş: {name}")
    return Domain(name=name, root=root, profiles=list(spec["profiles"]),
                  depends_on=list(spec.get("depends_on") or []),
                  critical=bool(spec.get("critical")),
                  paths=list(spec.get("paths") or []),
                  responsibilities=list(spec.get("responsibilities") or []),
                  max_parallel_workers=int(spec.get("max_parallel_workers", 1)))


def sync_to_store(store, domain: Domain) -> None:
    """Diskteki domain.yaml'ı çalışma-zamanı tablosuna yansıt."""
    store.upsert_domain(name=domain.name, profiles=domain.profiles,
                        depends_on=domain.depends_on, critical=domain.critical,
                        paths=domain.paths,
                        max_parallel_workers=domain.max_parallel_workers)
