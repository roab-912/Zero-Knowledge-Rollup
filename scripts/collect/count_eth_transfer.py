#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
count_eth_transfer.py
=====================

Estime le NOMBRE de transferts de valeur monétaire sur Ethereum L1 sur une
période donnée (par défaut : l'année 2025, du 2025-01-01 au 2025-12-31).

« Transfert monétaire » = ETH natif + ERC-20 :
  - ETH natif  : transaction avec value > 0 et input vide ('0x'), c.-à-d. un
                 transfert d'ETH simple (pas un appel de contrat).
  - ERC-20     : chaque log d'événement `Transfer(address,address,uint256)`
                 émis dans les reçus de bloc (topic0 = ERC20_TRANSFER_TOPIC).
                 Une même transaction peut émettre plusieurs transferts.

Méthode : ÉCHANTILLONNAGE. Compter chaque bloc de l'année (~2,6 M blocs) est
prohibitif en requêtes API. On échantillonne donc n blocs répartis uniformément
sur la période, on mesure la moyenne de transferts par bloc, puis on extrapole
au nombre total de blocs de la période. Méthode reproductible ; la précision
croît avec --samples (loi des grands nombres).

La détection ERC-20 nécessite les reçus de bloc (eth_getBlockReceipts). Si cet
endpoint est indisponible sur le plan Etherscan utilisé, seuls les transferts
ETH natifs sont comptés (l'estimation ERC-20 est alors marquée indisponible).

Usage
-----
  export ETHERSCAN_API_KEY="votreCle"
  python scripts/collect/count_eth_transfer.py
  python scripts/collect/count_eth_transfer.py --start 2025-01-01 --end 2025-12-31 --samples 400
  python scripts/collect/count_eth_transfer.py --start 2025-06-01 --end 2025-06-30

Dépendances : requests  (pip install requests)
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("Le module 'requests' est requis : pip install requests")

# Charge le .env du projet (si python-dotenv est installé), comme
# inspect_rollup.py. Sans dotenv, on retombe sur les variables d'environnement.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def get_api_key(cli_value):
    """Récupère la clé Etherscan depuis --api-key ou le .env/env (casse libre)."""
    if cli_value:
        return cli_value
    for name in ("ETHERSCAN_API_KEY", "etherscan_api_key"):
        val = os.environ.get(name)
        if val:
            return val
    return None

# --------------------------------------------------------------------------- #
# Constantes                                                                    #
# --------------------------------------------------------------------------- #
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1
BLOCK_TIME_S = 12.0  # un slot toutes les 12 s (post-Merge)

# topic0 du log Transfer(address,address,uint256) — commun à tous les ERC-20
ERC20_TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a1"
                        "1628f55a4df523b3ef")

# Sélecteurs (4 premiers octets du calldata) des transferts ERC-20 canoniques,
# utilisés pour identifier une TRANSACTION dont le but est un transfert de token
# (unité pertinente pour mesurer un COÛT par transaction, contrairement au
# comptage ERC-20 qui, lui, se fait par log d'événement).
ERC20_TRANSFER_SELECTOR = "0xa9059cbb"      # transfer(address,uint256)
ERC20_TRANSFER_FROM_SELECTOR = "0x23b872dd"  # transferFrom(address,address,uint256)
G_NATIVE_TRANSFER = 21_000                  # coût protocolaire exact d'un transfert ETH


# --------------------------------------------------------------------------- #
# Utilitaires HTTP (mêmes conventions que collect_send_eth_erc_20.py)          #
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Petit limiteur : Etherscan gratuit = 5 req/s ; on reste prudent."""
    def __init__(self, min_interval=0.22):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        delta = time.time() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.time()


def etherscan_get(params, api_key, limiter, retries=4):
    """Appel Etherscan V2 avec retry/backoff. Renvoie le champ 'result'."""
    params = dict(params)
    params.update({"chainid": CHAIN_ID, "apikey": api_key})
    url = f"{ETHERSCAN_BASE}?{urlencode(params)}"
    for attempt in range(retries):
        limiter.wait()
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Echec requête Etherscan: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
            continue
        msg = str(data.get("message", ""))
        result = data.get("result")
        if data.get("status") == "0" and "rate limit" in msg.lower():
            time.sleep(1.5 * (attempt + 1))
            continue
        if "result" in data:
            return result
        if data.get("status") == "1":
            return result
        raise RuntimeError(f"Etherscan a répondu: status={data.get('status')} "
                           f"message='{msg}' result='{result}'")
    raise RuntimeError("Etherscan: retries épuisés")


def hex_to_int(h):
    if h is None:
        return None
    if isinstance(h, int):
        return h
    return int(h, 16)


# --------------------------------------------------------------------------- #
# Backend JSON-RPC (Infura / n'importe quel noeud) — activé si RPC_URL est fixé #
# --------------------------------------------------------------------------- #
# Quand une URL RPC est fournie (--rpc), toutes les lectures on-chain passent
# par le JSON-RPC standard plutôt que par l'API Etherscan. Avantage : le noeud
# expose eth_getBlockReceipts, donc gasUsed + effectiveGasPrice sont disponibles
# pour TOUTES les transactions monétaires (natif ET ERC-20).
RPC_URL = None  # défini dans main() depuis --rpc


def json_rpc(method, params, limiter, retries=4):
    """Appel JSON-RPC POST avec retry/backoff. Renvoie le champ 'result'."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        limiter.wait()
        try:
            r = requests.post(RPC_URL, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Echec requête RPC ({method}): {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
            continue
        if "error" in data and data["error"]:
            err = data["error"]
            # 429 / limite de débit -> backoff et retry
            if isinstance(err, dict) and err.get("code") in (-32005, 429):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"RPC {method} a renvoyé une erreur: {err}")
        return data.get("result")
    raise RuntimeError(f"RPC {method}: retries épuisés")


def rpc_latest_block(limiter):
    return hex_to_int(json_rpc("eth_blockNumber", [], limiter))


def rpc_block_by_time(ts, closest, limiter):
    """
    Recherche dichotomique du bloc dont le timestamp est le plus proche de `ts`.
    `closest` = 'before' ou 'after' pour lever l'ambiguïté aux bornes.
    """
    lo, hi = 1, rpc_latest_block(limiter)
    lo_blk = json_rpc("eth_getBlockByNumber", [hex(lo), False], limiter)
    if lo_blk and hex_to_int(lo_blk["timestamp"]) >= ts:
        return lo
    best = hi
    while lo <= hi:
        mid = (lo + hi) // 2
        blk = json_rpc("eth_getBlockByNumber", [hex(mid), False], limiter)
        if not blk:
            hi = mid - 1
            continue
        t = hex_to_int(blk["timestamp"])
        if t == ts:
            return mid
        if t < ts:
            lo = mid + 1
        else:
            best = mid
            hi = mid - 1
    # `best` est le premier bloc avec timestamp >= ts (borne 'after').
    if closest == "before":
        return max(1, best - 1)
    return best


# --------------------------------------------------------------------------- #
# Résolution de blocs par date                                                 #
# --------------------------------------------------------------------------- #
def date_to_ts(d, end_of_day=False):
    t = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
    if end_of_day:
        t += dt.timedelta(hours=23, minutes=59, seconds=59)
    return int(t.timestamp())


def block_by_time(ts, closest, api_key, limiter):
    """Numéro de bloc le plus proche d'un timestamp (RPC dichotomie / Etherscan)."""
    if RPC_URL:
        return rpc_block_by_time(ts, closest, limiter)
    res = etherscan_get(
        {"module": "block", "action": "getblocknobytime",
         "timestamp": ts, "closest": closest},
        api_key, limiter)
    return int(res)


def get_block(block_no, api_key, limiter, full_tx=True):
    if RPC_URL:
        return json_rpc("eth_getBlockByNumber",
                        [hex(block_no), bool(full_tx)], limiter)
    return etherscan_get(
        {"module": "proxy", "action": "eth_getBlockByNumber",
         "tag": hex(block_no), "boolean": "true" if full_tx else "false"},
        api_key, limiter)


def get_block_receipts(block_no, api_key, limiter):
    """eth_getBlockReceipts : tous les reçus d'un bloc (logs). Peut être None."""
    if RPC_URL:
        try:
            res = json_rpc("eth_getBlockReceipts", [hex(block_no)], limiter)
        except RuntimeError:
            return None
        return res if isinstance(res, list) else None
    try:
        res = etherscan_get(
            {"module": "proxy", "action": "eth_getBlockReceipts",
             "blockNumber": hex(block_no)},
            api_key, limiter)
    except RuntimeError:
        return None
    if isinstance(res, dict):
        res = res.get("receipts") or res.get("result")
    return res if isinstance(res, list) else None


def get_tx_receipt(tx_hash, api_key, limiter):
    """eth_getTransactionReceipt : receipt d'UNE transaction (gasUsed, prix)."""
    if RPC_URL:
        try:
            return json_rpc("eth_getTransactionReceipt", [tx_hash], limiter)
        except RuntimeError:
            return None
    try:
        return etherscan_get(
            {"module": "proxy", "action": "eth_getTransactionReceipt",
             "txhash": tx_hash},
            api_key, limiter)
    except RuntimeError:
        return None


# Page maximale d'Etherscan pour module=logs (1000 records/page).
LOGS_PAGE_SIZE = 1000


def count_erc20_via_logs(block_no, api_key, limiter):
    """
    Compte les logs Transfer ERC-20 d'UN bloc via module=logs / getLogs
    (disponible sur le plan gratuit, contrairement à eth_getBlockReceipts).

    Filtre sur topic0 = ERC20_TRANSFER_TOPIC, fromBlock == toBlock == block_no.
    Pagine par pages de 1000 tant qu'une page pleine est renvoyée, afin de ne
    pas sous-compter les blocs très actifs (>1000 transferts).

    Renvoie le nombre de transferts, ou None si l'endpoint échoue.
    """
    if RPC_URL:
        try:
            res = json_rpc(
                "eth_getLogs",
                [{"fromBlock": hex(block_no), "toBlock": hex(block_no),
                  "topics": [ERC20_TRANSFER_TOPIC]}],
                limiter)
        except RuntimeError:
            return None
        return len(res) if isinstance(res, list) else None

    total = 0
    page = 1
    while True:
        try:
            res = etherscan_get(
                {"module": "logs", "action": "getLogs",
                 "fromBlock": block_no, "toBlock": block_no,
                 "topic0": ERC20_TRANSFER_TOPIC,
                 "page": page, "offset": LOGS_PAGE_SIZE},
                api_key, limiter)
        except RuntimeError:
            return None if total == 0 else total
        if not isinstance(res, list):
            # 'No records found' renvoie souvent [] ; tout le reste -> abandon
            return total
        total += len(res)
        if len(res) < LOGS_PAGE_SIZE:
            break
        page += 1
    return total


# --------------------------------------------------------------------------- #
# Comptage par bloc                                                             #
# --------------------------------------------------------------------------- #
def _method_id(tx):
    inp = (tx.get("input") or "0x").lower()
    return inp[:10] if len(inp) >= 10 else inp


def count_block(block, receipts, api_key=None, limiter=None, erc20_fetch_cap=0):
    """
    Compte, pour un bloc :
      n_native : transferts ETH natifs (value > 0, input vide)
      n_erc20  : logs Transfer ERC-20 (None si reçus indisponibles)
      n_all    : nombre total de transactions

    Et agrège le COÛT (gas + frais) des transactions monétaires :
      - ETH natif      : gasUsed du receipt (à défaut 21000, exact par protocole)
      - ERC-20 (tx)    : transaction dont le sélecteur est transfer/transferFrom ;
                         gasUsed lu dans le receipt.
    Le coût ERC-20 nécessite les receipts ; si `receipts` est indisponible mais
    que (api_key, limiter) sont fournis, on récupère le receipt par transaction.

    Renvoie un dict avec les compteurs et les agrégats de coût.
    """
    txs = block.get("transactions", []) or []
    n_all = len(txs)

    # Index transactionIndex -> receipt (si receipts de bloc disponibles).
    rc_by_index = {}
    if isinstance(receipts, list):
        for rc in receipts:
            if isinstance(rc, dict) and rc.get("transactionIndex") is not None:
                rc_by_index[hex_to_int(rc["transactionIndex"])] = rc

    # Comptage ERC-20 par LOG d'événement (unité du comptage historique).
    n_erc20 = None
    if isinstance(receipts, list):
        n_erc20 = 0
        for rc in receipts:
            if not isinstance(rc, dict):
                continue
            for log in rc.get("logs", []) or []:
                topics = log.get("topics") or []
                if topics and topics[0].lower() == ERC20_TRANSFER_TOPIC:
                    n_erc20 += 1

    agg = {
        "n_native": 0,
        "gas_native_sum": 0, "gas_native_n": 0, "fee_native_wei": 0,
        "gas_erc20_sum": 0, "gas_erc20_n": 0, "fee_erc20_wei": 0,
    }

    fetched = 0  # nb de receipts ERC-20 récupérés par tx dans ce bloc (borné)

    for tx in txs:
        value = hex_to_int(tx.get("value")) or 0
        inp = tx.get("input", "0x") or "0x"
        idx = hex_to_int(tx.get("transactionIndex"))
        sel = _method_id(tx)

        # --- Transfert ETH natif ---
        # gasUsed = 21000 exact par protocole ; on n'interroge jamais de receipt
        # pour le natif. Le prix effectif n'est agrégé que si le receipt de bloc
        # est disponible (sinon on renonce aux frais ETH pour cette tx).
        if value > 0 and inp in ("0x", ""):
            agg["n_native"] += 1
            rc = rc_by_index.get(idx)
            gas = (hex_to_int(rc.get("gasUsed")) if rc else None) or G_NATIVE_TRANSFER
            agg["gas_native_sum"] += gas
            agg["gas_native_n"] += 1
            if rc:
                price = hex_to_int(rc.get("effectiveGasPrice"))
                if price is not None:
                    agg["fee_native_wei"] += gas * price

        # --- Transfert ERC-20 (identifié par le sélecteur de la transaction) ---
        elif sel in (ERC20_TRANSFER_SELECTOR, ERC20_TRANSFER_FROM_SELECTOR):
            rc = rc_by_index.get(idx)
            if rc is None and api_key and limiter and fetched < erc20_fetch_cap \
                    and tx.get("hash"):
                rc = get_tx_receipt(tx["hash"], api_key, limiter)
                fetched += 1
            if rc:
                gas = hex_to_int(rc.get("gasUsed"))
                price = hex_to_int(rc.get("effectiveGasPrice"))
                if gas is not None:
                    agg["gas_erc20_sum"] += gas
                    agg["gas_erc20_n"] += 1
                    if price is not None:
                        agg["fee_erc20_wei"] += gas * price

    return agg["n_native"], n_erc20, n_all, agg


# --------------------------------------------------------------------------- #
# Échantillonnage + extrapolation                                              #
# --------------------------------------------------------------------------- #
def estimate_transfers(start_date, end_date, api_key, limiter, n_samples,
                       erc20_fetch_cap=8):
    ts0 = date_to_ts(start_date, end_of_day=False)
    ts1 = date_to_ts(end_date, end_of_day=True)
    b0 = block_by_time(ts0, "after", api_key, limiter)
    b1 = block_by_time(ts1, "before", api_key, limiter)
    if b1 <= b0:
        raise RuntimeError(f"Plage de blocs invalide: {b0}..{b1}")

    span = b1 - b0 + 1  # nombre total de blocs de la période
    n_samples = max(10, min(n_samples, span))
    step = (span - 1) / (n_samples - 1)
    sample_blocks = sorted({int(round(b0 + i * step))
                            for i in range(n_samples)})

    print(f"[sampling] période blocs {b0}..{b1} ({span} blocs au total), "
          f"{len(sample_blocks)} échantillons", file=sys.stderr)

    tot_native = tot_all = 0
    tot_erc20 = 0
    ok = 0
    n_with_erc20 = 0
    # Agrégats de coût (gas + frais) des transactions monétaires.
    cost = {
        "gas_native_sum": 0, "gas_native_n": 0, "fee_native_wei": 0,
        "gas_erc20_sum": 0, "gas_erc20_n": 0, "fee_erc20_wei": 0,
    }
    consecutive_receipt_failures = 0
    receipts_disabled = False  # bascule sur getLogs si eth_getBlockReceipts KO
    logs_fallback_announced = False

    for i, bn in enumerate(sample_blocks, 1):
        try:
            block = get_block(bn, api_key, limiter, full_tx=True)
        except RuntimeError as exc:
            print(f"  ! bloc {bn} ignoré: {exc}", file=sys.stderr)
            continue
        if not block:
            continue

        # Voie ERC-20 : eth_getBlockReceipts (rapide) si dispo, sinon getLogs.
        if not receipts_disabled:
            receipts = get_block_receipts(bn, api_key, limiter)
            if receipts is None:
                consecutive_receipt_failures += 1
                if consecutive_receipt_failures >= 5:
                    receipts_disabled = True
                    print("  [!] eth_getBlockReceipts indisponible : bascule "
                          "sur eth_getLogs pour compter les ERC-20.",
                          file=sys.stderr)
            else:
                consecutive_receipt_failures = 0
        else:
            receipts = None

        n_native, n_erc20, n_all, agg = count_block(
            block, receipts, api_key, limiter, erc20_fetch_cap)

        # Repli getLogs si les reçus n'ont pas fourni le compte ERC-20.
        if n_erc20 is None:
            n_erc20 = count_erc20_via_logs(bn, api_key, limiter)
            if n_erc20 is not None and not logs_fallback_announced:
                logs_fallback_announced = True

        # Accumule les agrégats de coût.
        for k in cost:
            cost[k] += agg[k]

        tot_native += n_native
        tot_all += n_all
        if n_erc20 is not None:
            tot_erc20 += n_erc20
            n_with_erc20 += 1
        ok += 1

        if i % 25 == 0:
            print(f"  ... {i}/{len(sample_blocks)} blocs traités",
                  file=sys.stderr)

    if ok == 0:
        raise RuntimeError("Aucun bloc exploitable échantillonné.")

    # Moyennes par bloc et extrapolation à la période complète
    native_per_block = tot_native / ok
    all_per_block = tot_all / ok
    est_native = native_per_block * span
    est_all = all_per_block * span

    # ERC-20 : moyenne uniquement sur les blocs où le compte était disponible
    if n_with_erc20 > 0:
        erc20_per_block = tot_erc20 / n_with_erc20
        est_erc20 = erc20_per_block * span
        est_monetary = est_native + est_erc20
    else:
        erc20_per_block = None
        est_erc20 = None
        est_monetary = None

    erc20_coverage = n_with_erc20 / ok if ok else 0.0

    # ---- Coût moyen des transactions monétaires (gas + frais ETH) ----
    def _mean(s, n):
        return (s / n) if n else None

    mean_gas_native = _mean(cost["gas_native_sum"], cost["gas_native_n"])
    mean_gas_erc20 = _mean(cost["gas_erc20_sum"], cost["gas_erc20_n"])
    n_cost_total = cost["gas_native_n"] + cost["gas_erc20_n"]
    # Moyenne combinée pondérée par le nombre de tx (= g_avg du modèle).
    mean_gas_monetary = _mean(
        cost["gas_native_sum"] + cost["gas_erc20_sum"], n_cost_total)

    mean_fee_native_wei = _mean(cost["fee_native_wei"], cost["gas_native_n"]) \
        if cost["fee_native_wei"] else None
    mean_fee_erc20_wei = _mean(cost["fee_erc20_wei"], cost["gas_erc20_n"]) \
        if cost["fee_erc20_wei"] else None
    total_fee_wei = cost["fee_native_wei"] + cost["fee_erc20_wei"]
    mean_fee_monetary_wei = (total_fee_wei / n_cost_total) \
        if (n_cost_total and total_fee_wei) else None

    def _eth(wei):
        return (wei / 1e18) if wei is not None else None

    return {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "block_start": b0,
        "block_end": b1,
        "n_blocks_period": span,
        "n_samples_ok": ok,
        "erc20_coverage": erc20_coverage,
        "native_per_block": native_per_block,
        "erc20_per_block": erc20_per_block,
        "all_tx_per_block": all_per_block,
        "n_native_sampled": tot_native,
        "n_erc20_sampled": tot_erc20 if n_with_erc20 else None,
        "n_all_sampled": tot_all,
        "estimated_native_transfers": est_native,
        "estimated_erc20_transfers": est_erc20,
        "estimated_monetary_transfers": est_monetary,
        "estimated_total_tx": est_all,
        # ---- Coût moyen (nouveau) ----
        "cost_n_native": cost["gas_native_n"],
        "cost_n_erc20": cost["gas_erc20_n"],
        "mean_gas_native": mean_gas_native,
        "mean_gas_erc20": mean_gas_erc20,
        "mean_gas_monetary": mean_gas_monetary,   # = g_avg (gas/tx)
        "mean_fee_native_eth": _eth(mean_fee_native_wei),
        "mean_fee_erc20_eth": _eth(mean_fee_erc20_wei),
        "mean_fee_monetary_eth": _eth(mean_fee_monetary_wei),
    }


# --------------------------------------------------------------------------- #
# Main                                                                          #
# --------------------------------------------------------------------------- #
def parse_args():
    ap = argparse.ArgumentParser(
        description="Estime le nombre de transferts monétaires (ETH natif + "
                    "ERC-20) sur Ethereum L1 sur une période, par "
                    "échantillonnage de blocs.")
    ap.add_argument("--start", default="2025-01-01", help="Date début YYYY-MM-DD")
    ap.add_argument("--end", default="2025-12-31", help="Date fin YYYY-MM-DD")
    ap.add_argument("--samples", type=int, default=300,
                    help="Nb de blocs échantillonnés. Plus élevé = plus précis "
                         "mais plus lent (défaut: 300).")
    ap.add_argument("--erc20-cost-cap", type=int, default=8,
                    help="Quand eth_getBlockReceipts est indisponible, nombre "
                         "max de receipts ERC-20 récupérés par bloc (par tx) pour "
                         "estimer le gas moyen ERC-20 (défaut: 8 ; 0 = désactive).")
    ap.add_argument("--request-delay", type=float, default=10.0,
                    help="Délai minimal (s) entre deux requêtes réseau "
                         "(défaut: 10). Baisse-le pour accélérer.")
    ap.add_argument("--outdir", default=".", help="Dossier de sortie du JSON")
    ap.add_argument("--api-key", default=None,
                    help="Clé Etherscan (sinon lue depuis .env / "
                         "ETHERSCAN_API_KEY)")
    ap.add_argument(
        "--rpc",
        nargs="?", const="https://sepolia.infura.io/v3/92460d764339458b9176bdcbc4545555",
        default=None,
        help="URL JSON-RPC (Infura/noeud) : bascule la source de données du RPC "
             "au lieu d'Etherscan. Sans valeur, utilise l'endpoint Sepolia par "
             "défaut. NB : Sepolia est un testnet — pour un g_avg réaliste, passe "
             "une URL mainnet (https://mainnet.infura.io/v3/<clé>).")
    return ap.parse_args()


def fmt_int(x):
    if x is None:
        return "(indisponible)"
    return f"{int(round(x)):,}".replace(",", " ")


def main():
    global RPC_URL
    args = parse_args()

    # En mode RPC (--rpc), la clé Etherscan n'est pas requise.
    RPC_URL = args.rpc
    api_key = None
    if RPC_URL:
        print(f"== Source : JSON-RPC {RPC_URL.split('/v3/')[0]}/v3/... ==",
              file=sys.stderr)
    else:
        api_key = get_api_key(args.api_key)
        if not api_key:
            sys.exit("Clé API manquante : ajoute ETHERSCAN_API_KEY au .env, "
                     "exporte-la, passe --api-key, ou utilise --rpc.")

    start_date = dt.date.fromisoformat(args.start)
    end_date = dt.date.fromisoformat(args.end)
    if end_date < start_date:
        sys.exit("La date de fin précède la date de début.")

    os.makedirs(args.outdir, exist_ok=True)
    limiter = RateLimiter(min_interval=args.request_delay)
    if args.request_delay >= 1:
        print(f"== Délai entre requêtes : {args.request_delay:g}s ==",
              file=sys.stderr)

    print(f"== Comptage transferts {start_date} -> {end_date} ==", file=sys.stderr)
    res = estimate_transfers(start_date, end_date, api_key, limiter,
                             args.samples, args.erc20_cost_cap)

    tag = f"{args.start}_{args.end}"
    json_path = os.path.join(args.outdir, f"eth_transfer_count_{tag}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str)

    # Récapitulatif console
    print("\n" + "=" * 64)
    print(f"  PÉRIODE : {args.start} -> {args.end}   (méthode: échantillonnage)")
    print("=" * 64)
    print(f"  Blocs de la période      : {fmt_int(res['n_blocks_period'])}")
    print(f"  Blocs échantillonnés     : {res['n_samples_ok']}")
    print(f"  Couverture ERC-20        : {res['erc20_coverage']*100:.0f} %")
    print("-" * 64)
    print(f"  Transferts ETH natifs    : ~ {fmt_int(res['estimated_native_transfers'])}")
    print(f"  Transferts ERC-20        : ~ {fmt_int(res['estimated_erc20_transfers'])}")
    print(f"  TOTAL monétaire          : ~ {fmt_int(res['estimated_monetary_transfers'])}")
    print("-" * 64)
    print(f"  (Total transactions      : ~ {fmt_int(res['estimated_total_tx'])})")
    print("=" * 64)

    # ---- Coût moyen des transactions monétaires ----
    def fmt_gas(x):
        return "(indisponible)" if x is None else f"{x:,.0f}".replace(",", " ")

    def fmt_eth(x):
        return "(indisponible)" if x is None else f"{x:.8f} ETH"

    print("\n" + "=" * 64)
    print("  COÛT MOYEN PAR TRANSACTION MONÉTAIRE (gas)")
    print("=" * 64)
    print(f"  ETH natif   (n={res['cost_n_native']:>5}) : "
          f"{fmt_gas(res['mean_gas_native'])} gas   {fmt_eth(res['mean_fee_native_eth'])}")
    print(f"  ERC-20      (n={res['cost_n_erc20']:>5}) : "
          f"{fmt_gas(res['mean_gas_erc20'])} gas   {fmt_eth(res['mean_fee_erc20_eth'])}")
    print("-" * 64)
    print(f"  COMBINÉ (g_avg)          : {fmt_gas(res['mean_gas_monetary'])} gas   "
          f"{fmt_eth(res['mean_fee_monetary_eth'])}")
    print("=" * 64)
    if res["mean_gas_erc20"] is None:
        print("  [!] Coût ERC-20 indisponible : aucun receipt exploitable "
              "(essaie --erc20-cost-cap > 0).", file=sys.stderr)
    if res["estimated_erc20_transfers"] is None:
        print("  [!] ERC-20 indisponible : ni eth_getBlockReceipts ni "
              "eth_getLogs accessibles sur ce plan.\n      Seuls les "
              "transferts ETH natifs ont été estimés.", file=sys.stderr)
    print(f"  JSON : {json_path}\n")


if __name__ == "__main__":
    main()
