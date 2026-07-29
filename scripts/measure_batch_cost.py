#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_batch_cost.py
=====================

Mesure EMPIRIQUE du coût de settlement d'un batch (G_batch) en soumettant
N fois la vraie transaction `submitBatch(...)` du contrat Rollup déployé, en
lisant `gasUsed` de chaque receipt on-chain, et en faisant la moyenne.

C'est la valeur qui remplace le placeholder G_batch (240000 / 321000) utilisé
dans regen_table.py et rollup_cost_table.py.

Principe
--------
Chaque appel `submitBatch` :
  - vérifie une preuve Groth16 (verifyProof, coût constant, indépendant de N_b),
  - contrôle que la transaction porte bien un blob EIP-4844 (blobhash(0)),
  - écrit le nouveau state root (SSTORE),
  - émet l'événement BatchSubmitted.

Comme la preuve Groth16 est de TAILLE CONSTANTE quelle que soit la taille de
batch, on peut réutiliser la même preuve : le contrat ne lie pas le proof aux
state roots (seul `input[1]` = bool de validité est vérifié). On chaîne donc :
    stateRootBefore = stateRoot courant
    stateRootAfter  = keccak(stateRootBefore || i)   (nouveau, unique)
ce qui garantit une écriture SSTORE en régime permanent (slot déjà non nul,
« warm/dirty » ~5000 gas), correspondant au régime permanent du modèle.

Le blob attaché est reconstruit de façon canonique (4096 éléments de champ,
31 octets utiles chacun) à partir des données compressées du batch — son
contenu n'influence pas le gas d'execution, seul compte que blobhash(0)
corresponde au hash versionné passé en argument.

Sorties
-------
  - stdout        : progression + statistiques (moyenne, écart-type, min/max)
  - <outdir>/batch_cost_measurements.csv   : une ligne par soumission
  - <outdir>/batch_cost_summary.json       : agrégats + contexte

Usage
-----
    # 100 écritures réelles sur Sepolia, batch de taille 2 :
    python scripts/measure_batch_cost.py --repeats 100 --size 2

    # essai à coût nul (eth_estimateGas, sans broadcast ni fonds) :
    python scripts/measure_batch_cost.py --repeats 5 --size 2 --estimate-only

    # décomposition (verify / storage+overhead) en plus :
    python scripts/measure_batch_cost.py --repeats 100 --size 2 --decompose
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from eth_account.typed_transactions.blob_transactions.blob_transaction import (
    BlobPooledTransactionData,
    Blob,
)
from hexbytes import HexBytes


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ABI_PATH = PROJECT_ROOT / "smart_contract" / "abi" / "Rollup.json"
CIRCUITS_DIR = PROJECT_ROOT / "circuits"
BLOB_DIR = PROJECT_ROOT / "blob"

BLOB_BYTES = 4096 * 32          # 131072 : taille d'un blob EIP-4844
FIELD_ELEMENTS = 4096
G_INTRINSIC = 21_000            # coût intrinsèque d'une transaction
SUBMIT_GAS_LIMIT = 800_000      # limite de gas fixe pour submitBatch (gasUsed réel ~300k)

# ABI minimale du verifier Groth16 (view) pour le contrôle de validité.
VERIFIER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256[2]", "name": "_pA", "type": "uint256[2]"},
            {"internalType": "uint256[2][2]", "name": "_pB", "type": "uint256[2][2]"},
            {"internalType": "uint256[2]", "name": "_pC", "type": "uint256[2]"},
            {"internalType": "uint256[1]", "name": "_pubSignals", "type": "uint256[1]"},
        ],
        "name": "verifyProof",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]


# --------------------------------------------------------------------------- #
# Utilitaires                                                                   #
# --------------------------------------------------------------------------- #
def make_w3(rpc_url: str) -> Web3:
    if rpc_url.startswith("ws"):
        ws_provider = (
            getattr(Web3, "LegacyWebSocketProvider", None)
            or getattr(Web3, "WebsocketProvider", None)
        )
        if ws_provider is None:
            sys.exit("[cost] Aucun provider WebSocket disponible dans web3.py.")
        return Web3(ws_provider(rpc_url))
    return Web3(Web3.HTTPProvider(rpc_url))


def load_proof_calldata(size: int):
    """
    Charge circuits/{size}/proof.json + public.json et retourne (a, b, c, input)
    au format attendu par le verifier Groth16 SnarkJS (avec le swap des
    coordonnées de G2 identique à `snarkjs zkey export soliditycalldata`).
    """
    proof_path = CIRCUITS_DIR / str(size) / "proof.json"
    public_path = CIRCUITS_DIR / str(size) / "public.json"
    if not proof_path.exists() or not public_path.exists():
        sys.exit(
            f"[cost] proof.json / public.json introuvable pour size={size} "
            f"(cherché dans {CIRCUITS_DIR / str(size)})."
        )
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    public = json.loads(public_path.read_text(encoding="utf-8"))

    a = [int(proof["pi_a"][0]), int(proof["pi_a"][1])]
    # snarkjs inverse l'ordre des composantes de G2 pour le verifier Solidity.
    b = [
        [int(proof["pi_b"][0][1]), int(proof["pi_b"][0][0])],
        [int(proof["pi_b"][1][1]), int(proof["pi_b"][1][0])],
    ]
    c = [int(proof["pi_c"][0]), int(proof["pi_c"][1])]
    pub = [int(x) for x in public]
    if len(pub) != 1:
        sys.exit(f"[cost] public.json attendu de longueur 1, obtenu {len(pub)}.")
    return a, b, c, pub


def to_canonical_blob(data: bytes) -> bytes:
    """
    Encode `data` dans un blob de 131072 octets, canonique (chaque élément de
    champ de 32 octets a son octet de poids fort à zéro, donc < module BLS),
    padding à zéro. La taille utile réelle est retournée séparément.
    """
    out = bytearray(BLOB_BYTES)
    for i in range(FIELD_ELEMENTS):
        chunk = data[i * 31:(i + 1) * 31]
        if not chunk:
            break
        out[i * 32 + 1: i * 32 + 1 + len(chunk)] = chunk
    return bytes(out)


def load_batch_data(size: int) -> bytes:
    """
    Récupère les données compressées du batch à publier en blob. Utilise
    blob/blob_{size}.bin si présent, sinon batch/batch_{size}.json, sinon un
    contenu déterministe (le coût d'execution ne dépend pas du contenu du blob).
    """
    candidate = BLOB_DIR / f"blob_{size}.bin"
    if candidate.exists():
        return candidate.read_bytes()
    batch = PROJECT_ROOT / "batch" / f"batch_{size}.json"
    if batch.exists():
        return batch.read_bytes()
    return (f"synthetic-batch-size-{size}-".encode()) * 64


def blob_versioned_hash(blob: bytes) -> bytes:
    bd = BlobPooledTransactionData(blobs=[Blob(data=HexBytes(blob))])
    vh = bd.versioned_hashes[0]
    return bytes(HexBytes(vh.data))


def next_state_root(w3: Web3, current: bytes, i: int) -> bytes:
    return bytes(w3.keccak(current + i.to_bytes(8, "big")))


def get_blob_base_fee(w3: Web3) -> int:
    """
    Prix de base du gas blob (wei), via RPC eth_blobBaseFee. Best-effort :
    retourne 1 wei (plancher réseau) si la méthode n'est pas exposée.
    """
    try:
        res = w3.provider.make_request("eth_blobBaseFee", [])
        if res.get("result") is not None:
            return int(res["result"], 16)
    except Exception:
        pass
    return 1


def dynamic_fees(w3: Web3):
    """
    Frais EIP-1559 raisonnables calés sur la base fee courante (petite sur
    Sepolia) plutôt qu'un plafond fixe élevé : maxFee = 2*baseFee + priority.
    Retourne (max_fee_per_gas, max_priority_fee_per_gas) en wei.
    """
    try:
        base = w3.eth.get_block("latest").get("baseFeePerGas") or w3.to_wei(1, "gwei")
    except Exception:
        base = w3.to_wei(1, "gwei")
    priority = w3.to_wei(2, "gwei")
    return int(base) * 2 + priority, priority


# --------------------------------------------------------------------------- #
# Décomposition (optionnelle)                                                   #
# --------------------------------------------------------------------------- #
def decompose_once(w3, account, verifier_addr, a, b, c, pub, total_gas_used):
    """
    Décomposition différentielle approximative de G_batch en trois composantes :
      - gas_verify   : coût du seul verifyProof (eth_estimateGas sur le verifier)
      - gas_overhead : coût intrinsèque type-3 (≈21000 + hachage versioned hashes)
      - gas_storage  : le reste (SSTORE du state root + logique + event), par
                       soustraction : total - verify - overhead.
    """
    verifier = w3.eth.contract(
        address=Web3.to_checksum_address(verifier_addr), abi=VERIFIER_ABI
    )
    # eth_estimateGas d'un appel qui ne fait QUE verifyProof (view exécuté en tx).
    gas_verify = verifier.functions.verifyProof(a, b, c, pub).estimate_gas(
        {"from": account.address}
    )
    gas_overhead = G_INTRINSIC
    gas_storage = total_gas_used - gas_verify - gas_overhead
    return {
        "gas_verify": gas_verify,
        "gas_overhead": gas_overhead,
        "gas_storage": gas_storage,
    }


# --------------------------------------------------------------------------- #
# Coeur : une soumission                                                         #
# --------------------------------------------------------------------------- #
def build_submit_tx(w3, rollup, account, size, a, b, c, pub,
                    root_before, root_after, vh, max_blob_fee,
                    gas_limit=None):
    fn = rollup.functions.submitBatch(
        size, a, b, c, pub, root_before, root_after, vh
    )
    max_fee, priority = dynamic_fees(w3)
    base = {
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
        "type": 3,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
        "maxFeePerBlobGas": max_blob_fee,
    }
    # On NE PEUT PAS estimer le gas via eth_estimateGas : cet appel n'attache
    # pas le blob, donc blobhash(0)=0 != vh et le contrat révoque ("blob hash
    # mismatch"). On fixe donc une limite de gas généreuse et constante ; le
    # gasUsed réel (= G_batch mesuré) est lu dans le receipt après exécution.
    if gas_limit is None:
        gas_limit = SUBMIT_GAS_LIMIT
    base["gas"] = gas_limit
    return fn.build_transaction(base)


def submit_and_measure(w3, rollup, account, size, a, b, c, pub,
                       root_before, root_after, vh, blob, max_blob_fee,
                       timeout=300):
    tx = build_submit_tx(
        w3, rollup, account, size, a, b, c, pub,
        root_before, root_after, vh, max_blob_fee,
    )
    signed = account.sign_transaction(tx, blobs=[blob])
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    if receipt.status != 1:
        raise RuntimeError(f"tx {tx_hash.hex()} échouée (status=0)")
    return receipt


# --------------------------------------------------------------------------- #
# Main                                                                          #
# --------------------------------------------------------------------------- #
def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repeats", type=int, default=100,
                    help="Nombre d'écritures/soumissions à mesurer (défaut: 100).")
    ap.add_argument("--size", type=int, default=2,
                    help="Taille de batch (verifier correspondant déjà déployé ; "
                         "défaut: 2).")
    ap.add_argument("--rpc", default=None,
                    help="URL RPC (défaut: WS_ADDRESS du .env).")
    ap.add_argument("--estimate-only", action="store_true",
                    help="N'exécute pas de vraie transaction : mesure gasUsed via "
                         "eth_estimateGas (coût nul, ne modifie pas la chaîne).")
    ap.add_argument("--decompose", action="store_true",
                    help="Ajoute la décomposition verify / storage / overhead.")
    ap.add_argument("--delay", type=float, default=5.0,
                    help="Délai (s) entre deux soumissions, pour éviter d'envoyer "
                         "deux transactions trop rapprochées (défaut: 5).")
    ap.add_argument("--yes", action="store_true",
                    help="Ne demande pas de confirmation avant de diffuser les "
                         "transactions (mode non interactif).")
    ap.add_argument("--outdir", default=str(PROJECT_ROOT / "bench-out"),
                    help="Dossier de sortie CSV/JSON.")
    return ap.parse_args()


def main():
    args = parse_args()
    load_dotenv(ENV_PATH)

    rpc_url = args.rpc or os.getenv("WS_ADDRESS")
    rollup_addr = os.getenv("ROLLUP_ADDRESS")
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    verifier_addr = os.getenv(f"CIRCUIT_{args.size}_ADDRESS", "").strip("'\"")

    if not rpc_url or rpc_url == "0":
        sys.exit("[cost] WS_ADDRESS/--rpc manquant.")
    if not rollup_addr or rollup_addr == "0":
        sys.exit("[cost] ROLLUP_ADDRESS manquant dans .env.")
    if not private_key or private_key == "0":
        sys.exit("[cost] WALLET_PRIVATE_KEY manquant dans .env.")
    if not verifier_addr:
        sys.exit(f"[cost] CIRCUIT_{args.size}_ADDRESS manquant : verifier de taille "
                 f"{args.size} non déployé (déploie-le ou change --size).")
    rollup_addr = rollup_addr.strip("'\"")

    w3 = make_w3(rpc_url)
    if not w3.is_connected():
        sys.exit(f"[cost] Connexion impossible à {rpc_url}.")

    account = Account.from_key(private_key)
    abi = json.loads(ABI_PATH.read_text(encoding="utf-8"))
    rollup = w3.eth.contract(address=Web3.to_checksum_address(rollup_addr), abi=abi)

    chain_id = w3.eth.chain_id
    balance = w3.from_wei(w3.eth.get_balance(account.address), "ether")
    print(f"[cost] chainId={chain_id} compte={account.address} solde={balance} ETH")
    print(f"[cost] Rollup={rollup_addr}  verifier[{args.size}]={verifier_addr}")

    # Préparation preuve + blob (constants pour toutes les soumissions).
    a, b, c, pub = load_proof_calldata(args.size)
    batch_data = load_batch_data(args.size)
    useful_bytes = len(batch_data)
    n_blobs = max(1, (useful_bytes + BLOB_BYTES - 1) // BLOB_BYTES)
    if n_blobs > 1:
        print(f"[cost] ATTENTION : données batch = {useful_bytes} o > 1 blob "
              f"({BLOB_BYTES} o) -> {n_blobs} blobs nécessaires. Ce script en "
              f"soumet 1 (mesure du régime 1 blob) ; borne de capacité DA atteinte.")
    blob = to_canonical_blob(batch_data)
    vh = blob_versioned_hash(blob)
    print(f"[cost] blob : {useful_bytes} o utiles / {BLOB_BYTES} o  "
          f"versioned_hash=0x{vh.hex()}")

    # Contrôle de validité de la preuve contre le verifier déployé (eth_call).
    verifier = w3.eth.contract(
        address=Web3.to_checksum_address(verifier_addr), abi=VERIFIER_ABI
    )
    ok = verifier.functions.verifyProof(a, b, c, pub).call()
    if not ok:
        sys.exit("[cost] La preuve chargée n'est PAS valide selon le verifier "
                 "déployé — vérifie circuits/{size}/proof.json.")
    print("[cost] Preuve validée par le verifier on-chain (eth_call verifyProof=true).")

    max_blob_fee = max(get_blob_base_fee(w3) * 2, w3.to_wei(1, "gwei"))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = outdir / f"batch_cost_{args.size}_{ts}.csv"
    json_path = outdir / f"batch_cost_{args.size}_{ts}.json"

    rows = []
    decomposition = None

    # --- Garde-fou de coût avant broadcast (mode on-chain uniquement) ---
    if not args.estimate_only:
        max_fee, _ = dynamic_fees(w3)
        # Borne haute : maxFeePerGas * limite de gas (pire cas) + coût blob.
        per_tx_wei = max_fee * SUBMIT_GAS_LIMIT + max_blob_fee * BLOB_BYTES
        total_eth = (per_tx_wei * args.repeats) / 1e18
        bal_eth = float(w3.from_wei(w3.eth.get_balance(account.address), "ether"))
        print(f"[cost] Estimation de coût MAX (bornée par maxFeePerGas) : "
              f"~{total_eth:.5f} ETH pour {args.repeats} tx "
              f"(le coût réel via effectiveGasPrice sera généralement bien moindre).")
        print(f"[cost] Solde disponible : {bal_eth:.5f} ETH")
        if total_eth > bal_eth:
            sys.exit(f"[cost] Coût max potentiel ({total_eth:.5f} ETH) > solde "
                     f"({bal_eth:.5f} ETH). Réduis --repeats ou recharge le compte.")
        if not args.yes:
            resp = input(f"[cost] Diffuser {args.repeats} transactions sur "
                         f"chainId={chain_id} ? [y/N] ").strip().lower()
            if resp not in ("y", "yes", "o", "oui"):
                sys.exit("[cost] Annulé.")

    print(f"\n[cost] Démarrage : {args.repeats} soumission(s) "
          f"({'estimation' if args.estimate_only else 'ON-CHAIN'}, "
          f"délai={args.delay:g}s entre tx)\n")

    for i in range(args.repeats):
        current_root = rollup.functions.stateRoot().call()
        root_before = bytes(current_root)
        root_after = next_state_root(w3, root_before, i)

        try:
            if args.estimate_only:
                tx = build_submit_tx(
                    w3, rollup, account, args.size, a, b, c, pub,
                    root_before, root_after, vh, max_blob_fee,
                )
                gas_used = w3.eth.estimate_gas(tx)
                blob_gas_used = BLOB_BYTES  # 1 blob = 131072 gas blob
                eff_price = w3.eth.gas_price
                blob_price = max_blob_fee
                block_number = w3.eth.block_number
                tx_hash = "(estimate)"
            else:
                receipt = submit_and_measure(
                    w3, rollup, account, args.size, a, b, c, pub,
                    root_before, root_after, vh, blob, max_blob_fee,
                )
                gas_used = receipt.gasUsed
                blob_gas_used = receipt.get("blobGasUsed", BLOB_BYTES)
                eff_price = receipt.get("effectiveGasPrice", 0)
                blob_price = receipt.get("blobGasPrice", 0)
                block_number = receipt.blockNumber
                tx_hash = receipt.transactionHash.hex()
        except Exception as e:
            msg = str(e)
            if args.estimate_only and "blob hash mismatch" in msg:
                sys.exit(
                    "[cost] --estimate-only impossible sur ce RPC : eth_estimateGas "
                    "n'attache pas de blob, donc blobhash(0) != versioned_hash et le "
                    "contrat révoque. Ce mode requiert un noeud blob-aware (ex. "
                    "`anvil --hardfork cancun --fork-url <rpc>`). Utilise le mode "
                    "on-chain (sans --estimate-only) pour une mesure réelle."
                )
            print(f"  [{i+1:>3}/{args.repeats}] ÉCHEC : {msg}")
            continue

        row = {
            "i": i,
            "N_b": args.size,
            "gasUsed": gas_used,
            "blobGasUsed": blob_gas_used,
            "n_blobs": 1,
            "effectiveGasPrice_wei": int(eff_price),
            "blobGasPrice_wei": int(blob_price),
            "block": block_number,
            "tx_hash": tx_hash,
            "state_root_before": "0x" + root_before.hex(),
            "state_root_after": "0x" + root_after.hex(),
        }
        rows.append(row)

        eth_cost = (int(eff_price) * gas_used) / 1e18
        running = [r["gasUsed"] for r in rows]
        running_mean = statistics.mean(running)
        pct = 100 * (i + 1) / args.repeats
        print(f"  [{i+1:>3}/{args.repeats}] ({pct:5.1f}%) gasUsed={gas_used:>7}  "
              f"moy.={running_mean:>10,.1f}  blobGas={blob_gas_used}  "
              f"~{eth_cost:.8f} ETH  block={block_number}", flush=True)

        # Décomposition une seule fois (sur la 1re soumission réussie).
        if args.decompose and decomposition is None:
            try:
                decomposition = decompose_once(
                    w3, account, verifier_addr, a, b, c, pub, gas_used
                )
                d = decomposition
                print(f"      décomposition : verify={d['gas_verify']} "
                      f"storage={d['gas_storage']} overhead={d['gas_overhead']}")
            except Exception as e:
                print(f"      décomposition indisponible : {e}")

        # Délai entre deux soumissions (pas après la dernière) pour ne pas
        # empiler deux transactions trop rapprochées sur le mempool.
        if args.delay > 0 and i < args.repeats - 1:
            time.sleep(args.delay)

    if not rows:
        sys.exit("[cost] Aucune mesure réussie.")

    # ---- Statistiques ----
    gas_vals = [r["gasUsed"] for r in rows]
    mean_gas = statistics.mean(gas_vals)
    stdev_gas = statistics.pstdev(gas_vals) if len(gas_vals) > 1 else 0.0
    median_gas = statistics.median(gas_vals)

    # ---- Écriture CSV ----
    fields = ["i", "N_b", "gasUsed", "blobGasUsed", "n_blobs",
              "effectiveGasPrice_wei", "blobGasPrice_wei", "block", "tx_hash",
              "state_root_before", "state_root_after"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "timestamp": ts,
        "chain_id": chain_id,
        "rollup": rollup_addr,
        "verifier": verifier_addr,
        "N_b": args.size,
        "repeats_requested": args.repeats,
        "repeats_success": len(rows),
        "mode": "estimate" if args.estimate_only else "onchain",
        "G_batch_mean_gas": mean_gas,
        "G_batch_median_gas": median_gas,
        "G_batch_stdev_gas": stdev_gas,
        "G_batch_min_gas": min(gas_vals),
        "G_batch_max_gas": max(gas_vals),
        "blobGasUsed": rows[0]["blobGasUsed"],
        "n_blobs": 1,
        "useful_batch_bytes": useful_bytes,
        "decomposition": decomposition,
        "csv": str(csv_path),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # ---- Récapitulatif ----
    print("\n" + "=" * 60)
    print(f"  G_batch mesuré (N_b = {args.size}, n = {len(rows)} soumissions)")
    print("=" * 60)
    print(f"  moyenne   : {mean_gas:>12,.1f} gas")
    print(f"  médiane   : {median_gas:>12,.1f} gas")
    print(f"  écart-type: {stdev_gas:>12,.1f} gas  "
          f"({100*stdev_gas/mean_gas:.3f} %)")
    print(f"  min / max : {min(gas_vals):,} / {max(gas_vals):,} gas")
    print(f"  blobGasUsed (marché blob séparé) : {rows[0]['blobGasUsed']:,} gas / blob")
    if decomposition:
        d = decomposition
        tot = mean_gas
        print("  -- décomposition --")
        print(f"    verify   : {d['gas_verify']:>10,} gas "
              f"({100*d['gas_verify']/tot:.1f} %)")
        print(f"    storage  : {d['gas_storage']:>10,} gas "
              f"({100*d['gas_storage']/tot:.1f} %)")
        print(f"    overhead : {d['gas_overhead']:>10,} gas "
              f"({100*d['gas_overhead']/tot:.1f} %)")
    print("-" * 60)
    print(f"  CSV  -> {csv_path}")
    print(f"  JSON -> {json_path}")


if __name__ == "__main__":
    main()
