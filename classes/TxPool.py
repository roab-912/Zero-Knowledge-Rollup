import os
import sqlite3
import threading
from typing import List, Optional

from models.Transaction import Transaction


class TxPool:
    """
    Pool de transactions persisté sur fichier (SQLite, mode WAL).

    Conçu pour absorber des rafales de plusieurs milliers de transactions
    sans saturer la RAM ni perdre de données en cas de redémarrage.

    Schéma :
        transactions(
            id INTEGER PK AUTOINCREMENT,
            from_address TEXT,
            to_address  TEXT,
            amount      REAL,
            timestamp   REAL,
            hash        TEXT,
            included    INTEGER DEFAULT 0,
            batch_id    INTEGER NULL
        )

    Politiques :
    - INSERT en autocommit, WAL → écritures concurrentes possibles.
    - get_batch() sélectionne au plus N transactions uniques par from_address
      parmi celles avec included=0, puis les marque incluses dans une transaction
      unique.
    """

    def __init__(self, db_path: str = "storage/txpool.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        # Verrou utilisé uniquement pour les sections multi-statements (get_batch).
        # Les insertions simples passent en autocommit côté SQLite.
        self._batch_lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_address TEXT    NOT NULL,
                    to_address   TEXT    NOT NULL,
                    amount       REAL    NOT NULL,
                    timestamp    REAL    NOT NULL,
                    hash         TEXT    NOT NULL,
                    included     INTEGER NOT NULL DEFAULT 0,
                    batch_id     INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending ON transactions(included, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_from ON transactions(from_address, included)"
            )

    def add(self, tx: Transaction) -> str:
        """
        Insère une transaction. L'id assigné par SQLite est ré-injecté dans
        l'objet et le hash est recalculé pour rester cohérent.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO transactions (from_address, to_address, amount, timestamp, hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tx.from_address, tx.to_adress, tx.amount, tx.timestamp, tx.hash),
            )
            tx.id = cur.lastrowid
            # `__hash_transaction__` est un dunder (entoure de `__`), donc
            # Python ne fait PAS de name-mangling — on l'appelle directement.
            tx.hash = tx.__hash_transaction__()
            conn.execute(
                "UPDATE transactions SET hash = ? WHERE id = ?",
                (tx.hash, tx.id),
            )
        return tx.hash

    def fetch_batch_unique_senders(self, limit: int) -> List[Transaction]:
        """
        Sélectionne jusqu'à `limit` transactions en attente, à raison d'une seule
        par from_address (la plus ancienne), et les marque incluses.
        Retourne la liste des Transaction reconstruites.
        """
        if limit <= 0:
            return []

        with self._batch_lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT id, from_address, to_address, amount, timestamp, hash
                    FROM transactions
                    WHERE included = 0
                      AND id IN (
                        SELECT MIN(id)
                        FROM transactions
                        WHERE included = 0
                        GROUP BY from_address
                      )
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

                if not rows:
                    conn.execute("COMMIT")
                    return []

                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE transactions SET included = 1 WHERE id IN ({placeholders})",
                    ids,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return [
            Transaction(
                from_address=r["from_address"],
                to_adress=r["to_address"],
                amount=r["amount"],
                id=r["id"],
                timestamp=r["timestamp"],
                hash=r["hash"],
            )
            for r in rows
        ]

    def assign_batch_id(self, tx_ids: List[int], batch_id: int) -> None:
        """Associe a posteriori un batch_id aux transactions incluses."""
        if not tx_ids:
            return
        with self._connect() as conn:
            placeholders = ",".join("?" * len(tx_ids))
            conn.execute(
                f"UPDATE transactions SET batch_id = ? WHERE id IN ({placeholders})",
                (batch_id, *tx_ids),
            )

    def pending_count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE included = 0"
            ).fetchone()[0]

    def total_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    def max_id(self) -> Optional[int]:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(id) FROM transactions").fetchone()
            return row[0] if row else None
