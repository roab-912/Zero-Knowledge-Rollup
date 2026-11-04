import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional

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
    - Une connexion WAL persistante par pool, protégée par un verrou réentrant.
    - L'insertion et la mise à jour du hash forment une transaction atomique.
    - get_batch() sélectionne au plus N transactions uniques par from_address
      parmi celles avec included=0, puis les marque incluses dans une transaction
      unique.
    """

    def __init__(self, db_path: str = "storage/txpool.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._batch_lock = threading.Lock()
        self._connection_lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None, timeout=30.0)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA temp_store=MEMORY")
            self._init_db()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Keep the lock throughout the operation: check_same_thread=False alone
        # does not protect multi-statement transactions from other threads.
        with self._connection_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("TxPool is closed")
            with self._connection:
                yield self._connection

    def close(self) -> None:
        """Release the connection and its WAL resources; safe to call twice."""
        with self._connection_lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self):
        with self._connection_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("TxPool is closed")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def __del__(self):
        # Explicit close/context management is preferred; also release pools
        # owned for the lifetime of a Sequencer when they are collected.
        try:
            self.close()
        except Exception:
            pass

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
        previous_id, previous_hash = tx.id, tx.hash
        try:
            with self._connect() as conn:
                # A consumer must never see the inserted row before its final
                # ID-dependent hash has been stored. One commit per add().
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """
                    INSERT INTO transactions (from_address, to_address, amount, timestamp, hash)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (tx.from_address, tx.to_adress, tx.amount, tx.timestamp, tx.hash),
                )
                tx.id = cur.lastrowid
                tx.hash = tx.__hash_transaction__()
                conn.execute("UPDATE transactions SET hash = ? WHERE id = ?", (tx.hash, tx.id))
                conn.commit()
        except BaseException:
            tx.id, tx.hash = previous_id, previous_hash
            raise
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
