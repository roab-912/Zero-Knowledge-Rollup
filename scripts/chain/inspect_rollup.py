"""
Inspecte l'état du Rollup déployé sur Sepolia :
  - state root courant + batchCount + owner
  - mapping verifiers(size) pour les tailles connues
  - événements BatchSubmitted récents (par défaut, derniers 100 000 blocs)

Usage :
    python scripts/chain/inspect_rollup.py
    python scripts/chain/inspect_rollup.py --from-block 7800000
    python scripts/chain/inspect_rollup.py --watch          # streaming continu
"""

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ABI_PATH = PROJECT_ROOT / "smart_contract" / "abi" / "Rollup.json"
KNOWN_SIZES = [1, 2, 4, 8, 16, 32, 64]


def make_w3(rpc_url: str) -> Web3:
    if rpc_url.startswith("ws"):
        ws_provider = (
            getattr(Web3, "LegacyWebSocketProvider", None)
            or getattr(Web3, "WebsocketProvider", None)
        )
        return Web3(ws_provider(rpc_url))
    return Web3(Web3.HTTPProvider(rpc_url))


def short(addr: str) -> str:
    return f"{addr[:8]}...{addr[-6:]}"


def print_state(rollup, latest_block: int) -> None:
    state_root = rollup.functions.stateRoot().call()
    batch_count = rollup.functions.batchCount().call()
    owner = rollup.functions.owner().call()
    print(f"--- Rollup @ {rollup.address} (block #{latest_block}) ---")
    print(f"  owner       : {owner}")
    print(f"  batchCount  : {batch_count}")
    print(f"  stateRoot   : 0x{state_root.hex()}")
    print("  verifiers   :")
    for s in KNOWN_SIZES:
        v = rollup.functions.verifiers(s).call()
        marker = "(non set)" if int(v, 16) == 0 else ""
        print(f"    size={s:<5} -> {v} {marker}")


def print_events(rollup, w3: Web3, from_block: int, to_block) -> None:
    event = rollup.events.BatchSubmitted()
    logs = event.get_logs(from_block=from_block, to_block=to_block)
    if not logs:
        print(f"  (aucun BatchSubmitted entre {from_block} et {to_block})")
        return
    print(f"  {len(logs)} BatchSubmitted entre {from_block} et {to_block} :")
    for ev in logs:
        a = ev["args"]
        tx_hash = ev["transactionHash"].hex()
        # versioned hash list de la tx (si blob tx)
        tx = w3.eth.get_transaction(ev["transactionHash"])
        blobs = tx.get("blobVersionedHashes") or []
        blob_str = ", ".join(b.hex() if isinstance(b, (bytes, bytearray)) else str(b) for b in blobs)
        print(
            f"    block={ev['blockNumber']} batchId={a['batchId']} size={a['batchSize']} "
            f"tx=0x{tx_hash}\n"
            f"      before=0x{a['stateRootBefore'].hex()}\n"
            f"      after =0x{a['stateRootAfter'].hex()}\n"
            f"      blobVH=0x{a['blobVersionedHash'].hex()}  (tx blobs: {blob_str or 'aucun'})"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-block", type=int, default=None,
                        help="Bloc de départ pour la recherche d'événements (default: latest - 100000).")
    parser.add_argument("--watch", action="store_true",
                        help="Streaming : ré-affiche l'état toutes les 12s.")
    parser.add_argument("--interval", type=int, default=12,
                        help="Intervalle de rafraîchissement en mode --watch.")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    rpc_url = os.getenv("WS_ADDRESS")
    rollup_addr = os.getenv("ROLLUP_ADDRESS")
    if not rpc_url or rpc_url == "0":
        raise SystemExit("WS_ADDRESS manquant dans .env")
    if not rollup_addr or rollup_addr == "0":
        raise SystemExit("ROLLUP_ADDRESS manquant dans .env (deploy_contracts.py pas exécuté ?).")

    w3 = make_w3(rpc_url)
    if not w3.is_connected():
        raise SystemExit(f"Connexion impossible à {rpc_url}")

    abi = json.loads(ABI_PATH.read_text(encoding="utf-8"))
    rollup = w3.eth.contract(address=Web3.to_checksum_address(rollup_addr), abi=abi)

    last_block_seen = None
    while True:
        latest = w3.eth.block_number
        print_state(rollup, latest)

        from_block = args.from_block if args.from_block is not None else max(0, latest - 100_000)
        to_block = "latest"
        print_events(rollup, w3, from_block, to_block)
        print("")

        if not args.watch:
            break
        last_block_seen = latest
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
