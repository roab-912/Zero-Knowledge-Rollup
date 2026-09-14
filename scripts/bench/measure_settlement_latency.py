#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_settlement_latency.py
=============================

Mesure EMPIRIQUE de la LATENCE de settlement d'un batch ZK rollup : le temps
mur complet entre le broadcast de la transaction `submitBatch(...)` (avec son
blob EIP-4844 attaché) et sa validation on-chain — c.-à-d. l'inclusion dans un
bloc qui écrit le blob ET met à jour la state root du Rollup.

On répète N fois (défaut 100) pour en tirer moyenne / médiane / écart-type.

Ce que mesure la latence
------------------------
Pour chaque tentative :
    t_send    = instant juste avant eth_sendRawTransaction
    t_receipt = instant où le receipt (tx minée) est disponible
    latence   = t_receipt - t_send          (temps mur, en secondes)

La latence englobe donc : propagation de la tx + du blob sur le réseau, attente
du prochain bloc, exécution (vérification Groth16 + BLOBHASH + SSTORE de la
state root). Optionnellement, on attend --confirmations blocs supplémentaires
pour mesurer la latence jusqu'à une profondeur de confirmation donnée.

On réutilise measure_batch_cost.py (préparation preuve/blob/contrat) pour ne
pas dupliquer la logique.

Sorties
-------
  - stdout : progression (latence + moyenne courante) + statistiques finales
  - <outdir>/settlement_latency_{size}_{ts}.csv   : une ligne par tentative
  - <outdir>/settlement_latency_{size}_{ts}.json  : agrégats + contexte

Usage
-----
    python scripts/bench/measure_settlement_latency.py --repeats 100 --size 2 --yes
    python scripts/bench/measure_settlement_latency.py --repeats 20 --confirmations 2
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

# Réutilise la préparation preuve/blob/contrat de measure_batch_cost.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_batch_cost as mbc  # noqa: E402


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repeats", type=int, default=100,
                    help="Nombre de settlements à chronométrer (défaut: 100).")
    ap.add_argument("--size", type=int, default=2,
                    help="Taille de batch (verifier déployé ; défaut: 2).")
    ap.add_argument("--rpc", default=None,
                    help="URL RPC (défaut: WS_ADDRESS du .env).")
    ap.add_argument("--confirmations", type=int, default=1,
                    help="Nb de blocs de confirmation à attendre (défaut: 1 = "
                         "dès l'inclusion).")
    ap.add_argument("--delay", type=float, default=5.0,
                    help="Délai (s) entre deux tentatives (défaut: 5).")
    ap.add_argument("--poll", type=float, default=1.0,
                    help="Intervalle de polling du receipt en s (défaut: 1).")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="Timeout d'attente d'un receipt en s (défaut: 300).")
    ap.add_argument("--yes", action="store_true",
                    help="Ne demande pas de confirmation avant de diffuser.")
    ap.add_argument("--outdir", default=str(mbc.PROJECT_ROOT / "bench-out"),
                    help="Dossier de sortie CSV/JSON.")
    return ap.parse_args()


def wait_receipt_timed(w3, tx_hash, poll, timeout):
    """Poll jusqu'au receipt ; renvoie (receipt, wait_seconds)."""
    start = time.perf_counter()
    deadline = start + timeout
    while True:
        try:
            rc = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            rc = None
        if rc is not None:
            return rc, time.perf_counter() - start
        if time.perf_counter() > deadline:
            raise TimeoutError(f"receipt non reçu après {timeout}s")
        time.sleep(poll)


def wait_confirmations(w3, block_number, confirmations, poll, timeout):
    """Attend que la profondeur atteigne `confirmations`. Renvoie le temps d'attente."""
    if confirmations <= 1:
        return 0.0
    target = block_number + confirmations - 1
    start = time.perf_counter()
    deadline = start + timeout
    while w3.eth.block_number < target:
        if time.perf_counter() > deadline:
            raise TimeoutError(f"{confirmations} confirmations non atteintes")
        time.sleep(poll)
    return time.perf_counter() - start


def main():
    args = parse_args()
    load_dotenv(mbc.ENV_PATH)

    rpc_url = args.rpc or os.getenv("WS_ADDRESS")
    rollup_addr = os.getenv("ROLLUP_ADDRESS")
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    verifier_addr = os.getenv(f"CIRCUIT_{args.size}_ADDRESS", "").strip("'\"")

    if not rpc_url or rpc_url == "0":
        sys.exit("[lat] WS_ADDRESS/--rpc manquant.")
    if not rollup_addr or rollup_addr == "0":
        sys.exit("[lat] ROLLUP_ADDRESS manquant dans .env.")
    if not private_key or private_key == "0":
        sys.exit("[lat] WALLET_PRIVATE_KEY manquant dans .env.")
    if not verifier_addr:
        sys.exit(f"[lat] CIRCUIT_{args.size}_ADDRESS manquant (verifier non déployé).")
    rollup_addr = rollup_addr.strip("'\"")

    w3 = mbc.make_w3(rpc_url)
    if not w3.is_connected():
        sys.exit(f"[lat] Connexion impossible à {rpc_url}.")

    account = Account.from_key(private_key)
    abi = json.loads(mbc.ABI_PATH.read_text(encoding="utf-8"))
    rollup = w3.eth.contract(address=Web3.to_checksum_address(rollup_addr), abi=abi)

    chain_id = w3.eth.chain_id
    balance = w3.from_wei(w3.eth.get_balance(account.address), "ether")
    print(f"[lat] chainId={chain_id} compte={account.address} solde={balance} ETH")
    print(f"[lat] Rollup={rollup_addr}  verifier[{args.size}]={verifier_addr}")

    # Préparation preuve + blob (constants pour toutes les tentatives).
    a, b, c, pub = mbc.load_proof_calldata(args.size)
    batch_data = mbc.load_batch_data(args.size)
    blob = mbc.to_canonical_blob(batch_data)
    vh = mbc.blob_versioned_hash(blob)
    print(f"[lat] blob : {len(batch_data)} o utiles / {mbc.BLOB_BYTES} o  "
          f"versioned_hash=0x{vh.hex()}")

    # Validité de la preuve (eth_call) avant de dépenser quoi que ce soit.
    verifier = w3.eth.contract(
        address=Web3.to_checksum_address(verifier_addr), abi=mbc.VERIFIER_ABI
    )
    if not verifier.functions.verifyProof(a, b, c, pub).call():
        sys.exit("[lat] Preuve invalide selon le verifier déployé.")
    print("[lat] Preuve validée on-chain (eth_call verifyProof=true).")

    max_blob_fee = max(mbc.get_blob_base_fee(w3) * 2, w3.to_wei(1, "gwei"))

    # Garde-fou de coût.
    max_fee, _ = mbc.dynamic_fees(w3)
    per_tx_wei = max_fee * mbc.SUBMIT_GAS_LIMIT + max_blob_fee * mbc.BLOB_BYTES
    total_eth = (per_tx_wei * args.repeats) / 1e18
    bal_eth = float(balance)
    print(f"[lat] Coût MAX estimé : ~{total_eth:.5f} ETH pour {args.repeats} tx "
          f"(réel généralement bien moindre). Solde : {bal_eth:.5f} ETH")
    if total_eth > bal_eth:
        sys.exit(f"[lat] Coût max ({total_eth:.5f} ETH) > solde ({bal_eth:.5f} ETH).")
    if not args.yes:
        resp = input(f"[lat] Diffuser {args.repeats} settlements sur "
                     f"chainId={chain_id} ? [y/N] ").strip().lower()
        if resp not in ("y", "yes", "o", "oui"):
            sys.exit("[lat] Annulé.")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = outdir / f"settlement_latency_{args.size}_{ts}.csv"
    json_path = outdir / f"settlement_latency_{args.size}_{ts}.json"

    rows = []
    print(f"\n[lat] Démarrage : {args.repeats} settlement(s), "
          f"confirmations={args.confirmations}, délai={args.delay:g}s\n")

    for i in range(args.repeats):
        current_root = bytes(rollup.functions.stateRoot().call())
        root_after = mbc.next_state_root(w3, current_root, i)

        try:
            tx = mbc.build_submit_tx(
                w3, rollup, account, args.size, a, b, c, pub,
                current_root, root_after, vh, max_blob_fee,
            )
            signed = account.sign_transaction(tx, blobs=[blob])
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction

            t_send = time.perf_counter()
            tx_hash = w3.eth.send_raw_transaction(raw)
            receipt, wait_incl = wait_receipt_timed(
                w3, tx_hash, args.poll, args.timeout)
            wait_conf = wait_confirmations(
                w3, receipt.blockNumber, args.confirmations, args.poll, args.timeout)
            latency = time.perf_counter() - t_send

            if receipt.status != 1:
                raise RuntimeError(f"tx {tx_hash.hex()} status=0 (revert)")

            # Vérifie que la state root a bien été mise à jour on-chain.
            new_root = bytes(rollup.functions.stateRoot().call())
            root_ok = (new_root == root_after)
        except Exception as e:
            print(f"  [{i+1:>3}/{args.repeats}] ÉCHEC : {e}", flush=True)
            continue

        row = {
            "i": i,
            "N_b": args.size,
            "latency_s": round(latency, 3),
            "inclusion_s": round(wait_incl, 3),
            "confirmations": args.confirmations,
            "gasUsed": receipt.gasUsed,
            "block": receipt.blockNumber,
            "state_root_updated": root_ok,
            "tx_hash": receipt.transactionHash.hex(),
        }
        rows.append(row)

        running = statistics.mean(r["latency_s"] for r in rows)
        pct = 100 * (i + 1) / args.repeats
        flag = "" if root_ok else "  [!root non maj]"
        print(f"  [{i+1:>3}/{args.repeats}] ({pct:5.1f}%) latence={latency:6.2f}s  "
              f"moy.={running:6.2f}s  bloc={receipt.blockNumber}"
              f"  gasUsed={receipt.gasUsed}{flag}", flush=True)

        if args.delay > 0 and i < args.repeats - 1:
            time.sleep(args.delay)

    if not rows:
        sys.exit("[lat] Aucune mesure réussie.")

    lat_vals = [r["latency_s"] for r in rows]
    incl_vals = [r["inclusion_s"] for r in rows]
    mean_lat = statistics.mean(lat_vals)
    median_lat = statistics.median(lat_vals)
    stdev_lat = statistics.pstdev(lat_vals) if len(lat_vals) > 1 else 0.0

    fields = ["i", "N_b", "latency_s", "inclusion_s", "confirmations",
              "gasUsed", "block", "state_root_updated", "tx_hash"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "timestamp": ts,
        "chain_id": chain_id,
        "rollup": rollup_addr,
        "N_b": args.size,
        "repeats_requested": args.repeats,
        "repeats_success": len(rows),
        "confirmations": args.confirmations,
        "latency_mean_s": mean_lat,
        "latency_median_s": median_lat,
        "latency_stdev_s": stdev_lat,
        "latency_min_s": min(lat_vals),
        "latency_max_s": max(lat_vals),
        "inclusion_mean_s": statistics.mean(incl_vals),
        "gas_used_mean": statistics.mean(r["gasUsed"] for r in rows),
        "state_root_always_updated": all(r["state_root_updated"] for r in rows),
        "csv": str(csv_path),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  Latence de settlement (N_b = {args.size}, n = {len(rows)}, "
          f"{args.confirmations} conf.)")
    print("=" * 60)
    print(f"  moyenne   : {mean_lat:>8.2f} s")
    print(f"  médiane   : {median_lat:>8.2f} s")
    print(f"  écart-type: {stdev_lat:>8.2f} s")
    print(f"  min / max : {min(lat_vals):.2f} / {max(lat_vals):.2f} s")
    print(f"  inclusion (send->minée) moy. : {statistics.mean(incl_vals):.2f} s")
    print(f"  state root mise à jour partout : "
          f"{'oui' if summary['state_root_always_updated'] else 'NON'}")
    print("-" * 60)
    print(f"  CSV  -> {csv_path}")
    print(f"  JSON -> {json_path}")


if __name__ == "__main__":
    main()
