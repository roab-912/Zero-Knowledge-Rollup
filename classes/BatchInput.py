"""Circuit input serialization shared by the rollup and its benchmark."""
import json


def write_batch_input(state, batch, new_state, filepath):
    transactions = batch.transactions
    data = {
        "src": [tx.from_address for tx in transactions],
        "srcBalance": [state.get_balance(tx.from_address) for tx in transactions],
        "srcBalanceAfter": [new_state.get_balance(tx.from_address) for tx in transactions],
        "dest": [tx.to_adress for tx in transactions],
        "destBalance": [state.get_balance(tx.to_adress) for tx in transactions],
        "destBalanceAfter": [new_state.get_balance(tx.to_adress) for tx in transactions],
        "amount": [tx.amount for tx in transactions],
    }
    with open(filepath, "w") as stream:
        json.dump(data, stream, indent=4)
    return data
