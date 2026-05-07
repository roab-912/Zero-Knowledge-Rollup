import hashlib
import json
from typing import List
import time

from models.Transaction import Transaction

class Batch:
    _id_counter = 0

    def __init__(self, transactions: List[Transaction] = None):
        """
        Initialise un batch de transactions avec :
        - une liste de transactions (vide par défaut),
        - un timestamp de création,
        - un hash calculé sur le contenu du batch.
        """
        self.id = Batch._get_next_id()
        self.transactions = transactions if transactions is not None else []
        self.timestamp = time.time()

    @classmethod
    def _get_next_id(cls):
        """
        Méthode de classe pour incrémenter et retourner un ID unique.
        """
        cls._id_counter += 1
        return cls._id_counter

    def add_transaction(self, transaction: Transaction):
        """
        Ajoute une transaction au batch et met à jour le hash.
        """
        self.transactions.append(transaction)

    def to_dict(self):
        """
        Retourne une représentation dict du batch, incluant :
        - le timestamp,
        - la liste des transactions converties en dict.
        Utile pour sérialisation ou calcul de hash.
        """
        return {
            "timestamp": self.timestamp,
            "transactions": [tx.to_dict() for tx in self.transactions]
        }

    def hash_batch(self):
        """
        Calcule le hash SHA-256 du batch sur la base de sa
        représentation JSON triée, garantissant l'unicité du hash
        selon le contenu et l'ordre.
        """
        batch_string = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(batch_string.encode()).hexdigest()

    def __iter__(self):
        """
        Permet d'itérer directement sur les transactions du batch.
        """
        return iter(self.transactions)

    def __len__(self):
        """
        Retourne le nombre de transactions dans le batch.
        """
        return len(self.transactions)

    def __getitem__(self, index):
        """
        Permet l'accès direct à une transaction par index.
        """
        return self.transactions[index]

    def __str__(self):
        """
        Représentation simplifiée pour affichage,
        montrant le nombre de transactions et un hash tronqué.
        """
        return f"Batch({len(self.transactions)} txs, hash={self.hash_batch()[:10]}...)"
