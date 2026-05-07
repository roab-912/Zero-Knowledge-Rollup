from pydantic import BaseModel

class TransactionRequest(BaseModel):
    """
    Modèle de données représentant une transaction reçue via l'API REST.

    Ce modèle est utilisé par FastAPI pour valider automatiquement le corps
    JSON des requêtes entrantes lors de l'ajout d'une transaction.

    Attributs :
    ----------
    from_address : str
        Adresse de l'expéditeur de la transaction.
    
    to_address : str
        Adresse du destinataire de la transaction.
    
    amount : float
        Montant de la transaction à transférer.
    """
    from_address: str
    to_address: str
    amount: float
