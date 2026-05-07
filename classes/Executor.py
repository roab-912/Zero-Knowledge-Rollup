from models.Transaction import Transaction
from models.Batch import Batch
from models.State import State

class Executor:
    def compute_batch(self, batch: Batch, state: State):
        """
        Applique toutes les transactions d'un batch sur une copie de l'état donné.
        Pour chaque transaction :
        - Vérifie sa validité.
        - Applique les effets si elle est valide.
        
        Retourne le nouvel état mis à jour si toutes les transactions sont valides,
        sinon retourne None.
        """
        self.state = state

        if len(batch) == 0:
            print("[Executor] Batch vide")
            return self.state        
        
        print("[Executor] État avant batch :", state)
        for tx in batch:
            print("[Executor] Traitement de la transaction :", tx.hash)
            if self.__valid_transaction__(tx):
                self.compute_transaction(tx, self.state)
            else:
                return None
        print("[Executor] État après transaction :", state)
        return self.state

    def compute_transaction(self, transaction: Transaction, state: State):
        """
        Applique une transaction valide à l'état donné :
        - Décrémente le solde de l'expéditeur.
        - Incrémente le solde du destinataire.
        """
        state.update_balances(transaction.from_address, transaction.to_adress, transaction.amount)
    
    def __valid_transaction__(self, transaction: Transaction):
        """
        Vérifie la validité d'une transaction :
        - L'adresse source doit exister.
        - L'adresse source doit être différente de la destination.
        - Le solde de l'expéditeur doit être suffisant.
        """
        user_from = transaction.from_address
        user_to = transaction.to_adress
        amount = transaction.amount

        valid = (
            user_from in self.state.balances.keys()
            and user_from != user_to
            and self.state.get_balance(user_from) >= amount
        )

        print(f"[Executor] Validation de la transaction {transaction.hash} : {'valide' if valid else 'invalide'}")
        return valid