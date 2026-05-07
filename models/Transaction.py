import hashlib
import json
import time

class Transaction:
    # Compteur statique pour assigner un ID unique à chaque transaction
    _id_counter = 0
    
    def __init__(
        self,
        from_address: str,
        to_adress: str,
        amount: float,
        *,
        id: int = None,
        timestamp: float = None,
        hash: str = None,
    ):
        """
        Initialise une transaction avec :
        - un ID unique (auto si non fourni, sinon repris depuis la persistance),
        - les adresses émettrice et destinataire,
        - le montant transféré,
        - un timestamp de création (auto si non fourni),
        - un hash calculé sur les données de la transaction (recalculé si non fourni).

        Les paramètres nommés `id`, `timestamp`, `hash` permettent de reconstruire
        une Transaction à l'identique depuis la txpool persistée.
        """
        self.id = id if id is not None else Transaction._get_next_id()
        self.from_address = from_address
        self.to_adress = to_adress
        self.amount = amount
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.hash = hash if hash is not None else self.__hash_transaction__()

    @classmethod
    def _get_next_id(cls):
        """
        Méthode de classe pour incrémenter et retourner un ID unique.
        """
        cls._id_counter += 1
        return cls._id_counter

    @classmethod
    def reset_counter_from(cls, max_id: int):
        """
        Resynchronise le compteur d'ID à partir d'une valeur (typiquement
        MAX(id) dans la txpool persistée), pour que les nouvelles transactions
        in-memory ne réutilisent pas un id déjà attribué côté DB.
        """
        if max_id is not None and max_id > cls._id_counter:
            cls._id_counter = max_id

    def to_dict(self):
        """
        Retourne un dictionnaire représentant la transaction,
        incluant son ID, adresses, montant, timestamp et hash.
        Utile pour la sérialisation JSON ou autres usages.
        """
        return {
            "id": self.id,
            "from_address": self.from_address,
            "to_adress": self.to_adress,
            "amount": self.amount,
            "timestamp": self.timestamp,
            "hash": self.hash
        }
    
    def __hash_transaction__(self):
        """
        Calcule un hash SHA-256 de la transaction basée sur
        ses données (sauf le hash lui-même pour éviter la circularité).
        Les données sont triées pour assurer la consistance du hash.
        """
        transaction_string = json.dumps({
            "id": self.id,
            "from_address": self.from_address,
            "to_adress": self.to_adress,
            "amount": self.amount,
            "timestamp": self.timestamp
        }, sort_keys=True)
        return hashlib.sha256(transaction_string.encode()).hexdigest()

    def __str__(self):
        """
        Représentation en chaîne de caractères simplifiée pour affichage.
        Le hash est tronqué pour une lecture plus claire.
        """
        return (f"Transaction(id={self.id}, from={self.from_address}, to={self.to_adress}, "
                f"amount={self.amount}, timestamp={self.timestamp}, hash={self.hash[:10]}...)")

    def __repr__(self):
        """
        Représentation officielle identique à __str__ pour consoles et debug.
        """
        return self.__str__()
