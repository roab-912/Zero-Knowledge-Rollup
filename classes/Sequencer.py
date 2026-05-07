from fastapi import FastAPI
from fastapi.responses import JSONResponse
import threading
import math
import uvicorn

from models.Transaction import Transaction
from models.Batch import Batch
from models.TransactionRequest import TransactionRequest
from classes.TxPool import TxPool


class Sequencer:
    """
    Reçoit les transactions via HTTP et les regroupe en batchs.

    Le pool de transactions est persisté sur fichier (SQLite, voir TxPool)
    pour absorber des rafales de plusieurs milliers de transactions sans
    saturer la mémoire et survivre à un redémarrage.
    """

    # Borne supérieure du nombre de transactions par batch
    # (≥ aux tailles de circuits disponibles).
    MAX_BATCH_SIZE = 32768

    def __init__(self, duration: int = 10, db_path: str = "storage/txpool.db"):
        self.duration = duration
        self.batch: Batch = None
        self.app = FastAPI()
        self.txpool = TxPool(db_path=db_path)

        # Resynchronise le compteur d'id de Transaction à partir de la DB
        # pour ne pas réutiliser un id déjà attribué.
        max_id = self.txpool.max_id()
        if max_id:
            Transaction.reset_counter_from(max_id)

        self.setup_routes()

        self.server_thread = threading.Thread(target=self.run, daemon=True)
        self.server_thread.start()
        print(
            f"[Sequencer] Serveur FastAPI lancé dans un thread "
            f"(txpool persistée : {self.txpool.db_path}, en attente : {self.txpool.pending_count()})"
        )

    def add_transaction(self, from_address, to_address, amount):
        """
        Crée une transaction et l'insère dans la txpool persistée.
        Retourne le hash de la transaction.
        """
        transaction = Transaction(from_address, to_address, amount)
        self.txpool.add(transaction)
        return transaction.hash

    def setup_routes(self):
        """
        Déclare les routes FastAPI, notamment le endpoint POST /add_transaction
        pour permettre l'ajout de transactions via API REST.
        """

        @self.app.post("/add_transaction")
        async def add_transaction_endpoint(tx: TransactionRequest):
            tx_hash = self.add_transaction(tx.from_address, tx.to_address, tx.amount)
            return JSONResponse(content={"message": "Transaction added", "tx_hash": tx_hash})

        @self.app.get("/pool_size")
        async def pool_size_endpoint():
            return JSONResponse(
                content={
                    "pending": self.txpool.pending_count(),
                    "total": self.txpool.total_count(),
                }
            )

    def run(self):
        """Démarre le serveur FastAPI dans un thread dédié."""
        print("[Sequencer] Démarrage du serveur FastAPI...")
        uvicorn.run(
            self.app, host="127.0.0.1", port=5000, log_level="critical", access_log=False
        )

    def get_batch(self) -> Batch:
        """
        Sélectionne jusqu'à MAX_BATCH_SIZE transactions uniques par from_address
        depuis la txpool, puis tronque à la plus grande puissance de 2 inférieure.
        Les transactions retenues sont marquées comme incluses dans la même
        transaction SQLite (pas de double consommation).
        """
        candidates = self.txpool.fetch_batch_unique_senders(self.MAX_BATCH_SIZE)

        if not candidates:
            self.batch = Batch()
            print("[Sequencer] Aucune transaction unique trouvée")
            return self.batch

        batch_size = 2 ** int(math.floor(math.log2(len(candidates))))
        transactions_to_batch = candidates[:batch_size]

        # Les transactions au-delà de la puissance de 2 ont déjà été marquées
        # incluses ; on les remet en attente pour ne pas les perdre.
        leftover_ids = [tx.id for tx in candidates[batch_size:]]
        if leftover_ids:
            self._reopen(leftover_ids)

        self.batch = Batch(transactions_to_batch)
        self.txpool.assign_batch_id([tx.id for tx in transactions_to_batch], self.batch.id)
        print(
            f"[Sequencer] Batch extrait ({batch_size} txs, "
            f"{self.txpool.pending_count()} restantes) : {self.batch}"
        )
        return self.batch

    def _reopen(self, ids):
        """Repasse en attente des transactions sélectionnées mais non utilisées."""
        if not ids:
            return
        with self.txpool._connect() as conn:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE transactions SET included = 0 WHERE id IN ({placeholders})",
                ids,
            )
