import hashlib
import json

class State:
    def __init__(self, initial_balances: dict = None):
        """
        Initialise l'état avec un dictionnaire de balances.
        Si aucun solde initial n'est fourni, initialise un dictionnaire vide.
        """
        self.balances = initial_balances if initial_balances else {}

    def get_balance(self, address: str):
        """
        Retourne la balance associée à une adresse.
        Si l'adresse n'existe pas, retourne 0 par défaut.
        """
        return self.balances.get(address, 0)

    def add_account(self, address: str, balance=0):
        """
        Ajoute un nouveau compte avec une balance initiale (par défaut 0),
        uniquement si l'adresse n'existe pas déjà dans l'état.
        """
        if address not in self.balances:
            self.balances[address] = balance
            print(f"[State] Compte ajouté : {address} avec balance {balance}")

    def update_balances(self, from_address: str, to_address: str, amount: float):
        """
        Met à jour les balances en transférant 'amount' de 'from_address' vers 'to_address'.
        - Vérifie que 'from_address' existe, sinon lève une erreur.
        - Ajoute 'to_address' avec balance 0 si elle n'existe pas encore.
        - Effectue la soustraction sur le compte émetteur et l'ajout sur le compte destinataire.
        """
        if from_address not in self.balances:
            raise ValueError(f"Compte émetteur inconnu: {from_address}")
        
        if to_address not in self.balances:
            self.add_account(to_address)
       
        self.balances[from_address] -= amount
        self.balances[to_address] += amount

    def get_all_balances(self):
        """
        Retourne une copie triée (par adresse) du dictionnaire des balances.
        """
        return dict(sorted(self.balances.items()))

    def get_all_accounts(self):
        """
        Retourne une copie triée (par adresse) du dictionnaire des comptes.
        """
        return list(self.balances.keys())

    def copy(self):
        """
        Retourne une copie indépendante de l'objet State,
        avec un nouveau dictionnaire de balances copié.
        """
        return State(self.balances.copy())

    def to_dict(self):
        """
        Retourne une version sérialisable (triée) de l'état.
        """
        return self.get_all_balances()
    
    def hash(self):
        """
        Retourne le hash SHA256 de l'état actuel (balances triées),
        utile pour la vérification ou publication dans un blob.
        """
        state_json = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(state_json.encode()).hexdigest()
    
    def __str__(self):
        """
        Représentation en chaîne de caractères de l'état pour affichage simple.
        """
        return f"State({self.balances})"

    def __repr__(self):
        """
        Représentation officielle identique à __str__ pour consoles et debug.
        """
        return self.__str__()
