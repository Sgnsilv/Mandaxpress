"""Aplicador de migrations SQL versionadas.

- Arquivos `NNN_descricao.sql` em ordem numérica.
- Cada migration aplicada é registrada em `schema_migrations` com checksum.
- Se uma migration já aplicada for alterada, o processo aborta (nunca reaplica).
- Um lock nomeado impede duas execuções simultâneas.
- Só roda SQL do repositório; não há DROP DATABASE nem limpeza de dados.
- Atenção: DDL no MariaDB confirma implicitamente (sem rollback); por isso
  cada migration deve ser pequena e idempotente na medida do possível.
"""
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .database import Database

logger = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")
_LOCK_NAME = "mandaxpress_migrate"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationFile:
    version: str
    path: Path
    checksum: str
    sql: str


def load_migrations(directory: str) -> List[MigrationFile]:
    base = Path(directory)
    if not base.is_dir():
        raise MigrationError(f"Diretório de migrations não encontrado: {directory}")
    result = []
    for path in sorted(base.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(f"Nome de migration inválido: {path.name}")
        content = path.read_text(encoding="utf-8")
        result.append(MigrationFile(
            version=path.stem,
            path=path,
            checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            sql=content,
        ))
    versions = [m.version for m in result]
    if len(set(m.version.split("_")[0] for m in result)) != len(versions):
        raise MigrationError("Há migrations com o mesmo número.")
    return result


def split_statements(sql: str) -> List[str]:
    """Remove comentários de linha (--) e separa por ';'. As migrations não
    devem conter ';' dentro de literais."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _ensure_table(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version VARCHAR(150) NOT NULL PRIMARY KEY, "
        "checksum CHAR(64) NOT NULL, "
        "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


def get_status(db: Database, directory: str) -> List[dict]:
    migrations = load_migrations(directory)
    conn = db.connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("SELECT version, checksum, applied_at FROM schema_migrations")
            applied = {r["version"]: r for r in cur.fetchall()}
    finally:
        conn.close()
    return [
        {
            "version": m.version,
            "applied": m.version in applied,
            "checksum_ok": applied[m.version]["checksum"] == m.checksum if m.version in applied else None,
            "applied_at": applied[m.version]["applied_at"] if m.version in applied else None,
        }
        for m in migrations
    ]


def migrate(db: Database, directory: str) -> List[str]:
    """Aplica as migrations pendentes. Retorna as versões aplicadas agora."""
    migrations = load_migrations(directory)
    conn = db.connect(autocommit=True)
    newly_applied: List[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 30) AS got", (_LOCK_NAME,))
            if cur.fetchone()["got"] != 1:
                raise MigrationError("Não foi possível obter o lock de migrations.")
            try:
                _ensure_table(cur)
                cur.execute("SELECT version, checksum FROM schema_migrations")
                applied = {r["version"]: r["checksum"] for r in cur.fetchall()}

                unknown = set(applied) - {m.version for m in migrations}
                if unknown:
                    raise MigrationError(f"Banco contém migrations desconhecidas: {sorted(unknown)}")

                for m in migrations:
                    if m.version in applied:
                        if applied[m.version] != m.checksum:
                            raise MigrationError(
                                f"Migration {m.version} foi alterada após ser aplicada."
                            )
                        continue
                    logger.info("Aplicando migration %s", m.version)
                    for stmt in split_statements(m.sql):
                        cur.execute(stmt)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (m.version, m.checksum),
                    )
                    newly_applied.append(m.version)
            finally:
                cur.execute("SELECT RELEASE_LOCK(%s)", (_LOCK_NAME,))
    finally:
        conn.close()
    return newly_applied
