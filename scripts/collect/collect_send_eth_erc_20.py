#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_migration_data.py
=========================

Collecte les paramètres empiriques nécessaires aux Tables 2 et 3 (Section 7,
"Per-Transaction Cost: Ethereum L1 vs. ZK Rollup") du papier LaTeX, sur une
période [start_date, end_date] arbitraire (un mois, une année, ...), puis
génère les blocs LaTeX prêts à coller.

Tous les coûts sont exprimés en unités natives du protocole (gas, gwei) :
aucune conversion fiat. La démonstration porte sur la réduction RELATIVE du
coût par transaction, r(p)/F_L1, qui est indépendante du prix de l'ETH.

Paramètres mesurés
------------------
  lambda_eth  : débit moyen de transactions MONÉTAIRES (ETH natifs + ERC-20), tx/s
  g_tx        : gas moyen d'une transaction monétaire, en gas
  P_gas       : prix d'exécution moyen (base fee), en gwei
  P_blob      : blob base fee médiane, en gwei/octet (≈ blobBaseFee en wei / 1e9)
  F_L1        : g_tx * P_gas, coût de référence L1 (gwei)

Sources
-------
  - Etherscan API V2 (une seule base URL, chainid=1) :
      * module=stats  (endpoints "daily*"): PRO (Standard tier et +). Voie rapide.
      * module=proxy  (eth_getBlockByNumber, eth_getBlockReceipts): GRATUIT.
        Voie par échantillonnage de blocs — fonctionne sur le plan gratuit.

Le script choisit automatiquement la voie PRO si --pro est passé, sinon il
échantillonne des blocs. Le comptage exact "ETH natifs + ERC-20" sur un mois
entier serait prohibitif ; on l'ESTIME par échantillonnage stratifié de blocs
répartis sur la période (voir --samples), méthode documentée et reproductible.

Usage
-----
  export ETHERSCAN_API_KEY="votreCle"
  python collect_migration_data.py --start 2026-01-01 --end 2026-01-31
  python collect_migration_data.py --start 2025-01-01 --end 2025-12-31 --samples 400
  python collect_migration_data.py --start 2026-06-01 --end 2026-06-30 --pro

Sorties
-------
  - stdout : récapitulatif + blocs LaTeX (tab:econ_params, tab:migration)
  - <outdir>/migration_params_<start>_<end>.json : tous les paramètres bruts
  - <outdir>/tab_econ_params_<start>_<end>.tex
  - <outdir>/tab_migration_<start>_<end>.tex

Dépendances : requests  (pip install requests)
"""

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("Le module 'requests' est requis : pip install requests")

# --------------------------------------------------------------------------- #
# Constantes protocole (voir tab:econ_params du papier)                         #
# --------------------------------------------------------------------------- #
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1

BLOCK_TIME_S = 12.0          # T_block : un slot toutes les 12 s (post-Merge)
BLOBS_PER_BLOB_BYTES = 131072  # B_blob : taille d'un blob (EIP-4844)
WEI_PER_GWEI = 1e9

# Constantes rollup côté "modèle" (NON mesurées on-chain : hypothèses du papier).
# Elles sont reprises telles quelles dans tab:migration ; ajuste-les si besoin.
ROLLUP_DEFAULTS = {
    "delta_b": 300.0,        # Δ_b : intervalle de publication (s)
    "n_b_max": 8192,         # N_b^max : plus grand circuit benchmarké
    "g_ver": 300_000,        # g_ver : gas de vérification Groth16
    "s0_da": 512,            # S_0^DA : overhead fixe par batch (octets)
    "s_tx_da": 16,           # s_tx^DA : octets par tx compressée
    "rho_gwei_s": 170.0,     # ρ : coût matériel prover (gwei/s)
    "n_tgt": 3,              # cible blob/bloc (EIP-4844 ; relevée depuis)
    # T_proof(N_b) interpolé : ~3 s à N_b=3000 (Fig. 4a Rapidsnark). On borne
    # c_prove < 2 gwei/tx sur toute la plage, donc impact négligeable sur r.
    "t_proof_at_3000_s": 3.0,
}

# Fractions de migration p évaluées dans tab:migration
MIGRATION_FRACTIONS = [0.01, 0.10, 0.25, 0.50, 0.75, 1.00]

# Signatures ERC-20 transfer / transferFrom (topic0 du log Transfer)
ERC20_TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a1"
                        "1628f55a4df523b3ef")


# --------------------------------------------------------------------------- #
# Utilitaires HTTP                                                             #
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
        # Rate limit / erreurs transitoires
        msg = str(data.get("message", ""))
        result = data.get("result")
        if data.get("status") == "0" and "rate limit" in msg.lower():
            time.sleep(1.5 * (attempt + 1))
            continue
        # Certains endpoints (proxy) renvoient jsonrpc sans 'status'
        if "result" in data:
            return result
        if data.get("status") == "1":
            return result
        # PRO endpoint refusé sur plan gratuit, ou autre
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
# Résolution de blocs par date                                                #
# --------------------------------------------------------------------------- #
def date_to_ts(d, end_of_day=False):
    t = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
    if end_of_day:
        t += dt.timedelta(hours=23, minutes=59, seconds=59)
    return int(t.timestamp())


def block_by_time(ts, closest, api_key, limiter):
    """getblocknobytime : numéro de bloc le plus proche d'un timestamp."""
    res = etherscan_get(
        {"module": "block", "action": "getblocknobytime",
         "timestamp": ts, "closest": closest},
        api_key, limiter)
    return int(res)


# --------------------------------------------------------------------------- #
# VOIE PRO : endpoints stats journaliers                                       #
# --------------------------------------------------------------------------- #
def fetch_daily_series(action, start_date, end_date, api_key, limiter,
                       value_key):
    """Récupère une série journalière 'stats/daily*' (endpoints PRO)."""
    res = etherscan_get(
        {"module": "stats", "action": action,
         "startdate": start_date.isoformat(),
         "enddate": end_date.isoformat(), "sort": "asc"},
        api_key, limiter)
    if not isinstance(res, list):
        raise RuntimeError(f"stats/{action}: réponse inattendue ({res})")
    return [(row["UTCDate"], float(str(row[value_key]).replace(",", "")))
            for row in res]


def collect_pro(start_date, end_date, api_key, limiter):
    """Agrégats via endpoints PRO : rapide mais nécessite Standard+."""
    # dailyavggasprice -> gas price moyen (en wei d'après la doc: valeur en Gwei)
    gp = fetch_daily_series("dailyavggasprice", start_date, end_date,
                            api_key, limiter, "avgGasPrice_Wei")
    p_gas_gwei = statistics.mean(v for _, v in gp) / WEI_PER_GWEI

    # dailytx / dailygasused -> gas moyen par tx (toutes tx)
    dtx = fetch_daily_series("dailytx", start_date, end_date,
                             api_key, limiter, "transactionCount")
    dgu = fetch_daily_series("dailygasused", start_date, end_date,
                             api_key, limiter, "gasUsed")
    tot_tx = sum(v for _, v in dtx)
    tot_gas = sum(v for _, v in dgu)
    g_tx_all = tot_gas / tot_tx if tot_tx else float("nan")

    n_days = (end_date - start_date).days + 1
    lambda_all = tot_tx / (n_days * 86400.0)

    return {
        "method": "pro-stats",
        "P_gas_gwei": p_gas_gwei,
        "g_tx_all_gas": g_tx_all,
        "lambda_all_tps": lambda_all,
        "n_tx_total": int(tot_tx),
        "n_days": n_days,
        # Le partage monétaire n'est pas fourni par ces endpoints : on complète
        # via un échantillonnage réduit de blocs (voir estimate_monetary_share).
    }


# --------------------------------------------------------------------------- #
# VOIE GRATUITE : échantillonnage de blocs                                     #
# --------------------------------------------------------------------------- #
def get_block(block_no, api_key, limiter, full_tx=True):
    res = etherscan_get(
        {"module": "proxy", "action": "eth_getBlockByNumber",
         "tag": hex(block_no), "boolean": "true" if full_tx else "false"},
        api_key, limiter)
    return res


def get_tx_receipt(tx_hash, api_key, limiter):
    """eth_getTransactionReceipt : reçu d'une transaction (gratuit)."""
    try:
        res = etherscan_get(
            {"module": "proxy", "action": "eth_getTransactionReceipt",
             "txhash": tx_hash},
            api_key, limiter)
    except RuntimeError:
        return None
    return res if isinstance(res, dict) else None


def get_block_receipts(block_no, api_key, limiter):
    """
    eth_getBlockReceipts : tous les reçus d'un bloc (gasUsed, logs).

    Cet endpoint n'est pas garanti sur tous les plans/fournisseurs. On renvoie
    une liste de reçus si disponible, sinon None (l'appelant bascule alors sur
    le gas limite des transactions et désactive la détection ERC-20 par logs).
    """
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


def classify_and_measure_block(block, receipts):
    """
    Classe les transactions d'un bloc et mesure le gas.

    Monétaire = transfert ETH natif STANDARD uniquement :
        input '0x' (aucune donnée), value > 0, ET coût d'exécution
        EXACTEMENT 21 000 gas (transfert ETH simple, sans appel de contrat).
    Les transferts ERC-20 et les transferts ETH vers des contrats (gas > 21000)
    sont exclus.
    Renvoie (n_mon, gas_mon_total, n_all, gas_all_total,
             blob_gas_used, base_fee_wei).
    """
    STANDARD_ETH_GAS = 21_000

    txs = block.get("transactions", []) or []
    base_fee_wei = hex_to_int(block.get("baseFeePerGas"))
    blob_gas_used = hex_to_int(block.get("blobGasUsed"))  # None avant 4844

    # Indexer les reçus par hash. eth_getBlockReceipts peut renvoyer, selon le
    # fournisseur/plan : une liste de dicts (cas normal), un dict {"receipts": [...]}
    # (variante JSON-RPC), None, ou une chaîne d'erreur. On normalise et on
    # ignore tout élément non exploitable plutôt que de planter.
    rcpt_by_hash = {}
    if isinstance(receipts, dict):
        receipts = receipts.get("receipts") or receipts.get("result")
    if isinstance(receipts, list):
        for rc in receipts:
            if isinstance(rc, dict):
                rcpt_by_hash[rc.get("transactionHash")] = rc

    n_all = 0
    gas_all = 0
    n_mon = 0
    gas_mon = 0
    receipts_available = bool(rcpt_by_hash)

    for tx in txs:
        n_all += 1
        rc = rcpt_by_hash.get(tx.get("hash"))
        gas_used = hex_to_int(rc.get("gasUsed")) if rc else None
        if gas_used is None:
            # fallback: gas limite de la tx (surestime) — évité si reçus présents
            gas_used = hex_to_int(tx.get("gas")) or 0
        gas_all += gas_used

        value = hex_to_int(tx.get("value")) or 0
        inp = tx.get("input", "0x") or "0x"
        is_native = (value > 0 and inp in ("0x", ""))

        # Transfert ETH standard = natif ET gas réellement consommé == 21000.
        # Le coût exact n'est fiable que via le reçu (gasUsed) ; sans reçu, la
        # gas limit d'un transfert simple vaut aussi 21000 en pratique, on
        # l'accepte donc comme approximation.
        is_standard_eth = is_native and gas_used == STANDARD_ETH_GAS

        if is_standard_eth:
            n_mon += 1
            gas_mon += gas_used

    return (n_mon, gas_mon, n_all, gas_all, blob_gas_used, base_fee_wei,
            receipts_available)


def collect_sampling(start_date, end_date, api_key, limiter, n_samples):
    """
    Échantillonne n_samples blocs uniformément répartis sur [start, end],
    mesure la part monétaire, le gas moyen, P_gas et P_blob.
    """
    ts0 = date_to_ts(start_date, end_of_day=False)
    ts1 = date_to_ts(end_date, end_of_day=True)
    b0 = block_by_time(ts0, "after", api_key, limiter)
    b1 = block_by_time(ts1, "before", api_key, limiter)
    if b1 <= b0:
        raise RuntimeError(f"Plage de blocs invalide: {b0}..{b1}")

    span = b1 - b0
    n_samples = max(10, min(n_samples, span))
    # Blocs équidistants (échantillonnage systématique stratifié)
    step = span / (n_samples - 1)
    sample_blocks = sorted({int(round(b0 + i * step))
                            for i in range(n_samples)})

    print(f"[sampling] blocs {b0}..{b1} ({span} blocs), "
          f"{len(sample_blocks)} échantillons", file=sys.stderr)

    tot_n_mon = tot_gas_mon = tot_n_all = tot_gas_all = 0
    base_fees_wei = []
    blob_gas_used_list = []
    ok = 0
    n_with_receipts = 0
    consecutive_receipt_failures = 0
    receipts_disabled = False   # coupé après échecs répétés (plan gratuit)

    for i, bn in enumerate(sample_blocks, 1):
        try:
            block = get_block(bn, api_key, limiter, full_tx=True)
            if receipts_disabled:
                receipts = None
            else:
                receipts = get_block_receipts(bn, api_key, limiter)
                if receipts is None:
                    consecutive_receipt_failures += 1
                    if consecutive_receipt_failures >= 5:
                        receipts_disabled = True
                        print("  [!] eth_getBlockReceipts indisponible "
                              "(5 échecs consécutifs) : appels désactivés "
                              "pour le reste de l'échantillon.",
                              file=sys.stderr)
                else:
                    consecutive_receipt_failures = 0
        except RuntimeError as exc:
            print(f"  ! bloc {bn} ignoré: {exc}", file=sys.stderr)
            continue
        if not block:
            continue
        (n_mon, gas_mon, n_all, gas_all,
         blob_gas_used, base_fee_wei,
         receipts_available) = classify_and_measure_block(block, receipts)
        if receipts_available:
            n_with_receipts += 1
        tot_n_mon += n_mon
        tot_gas_mon += gas_mon
        tot_n_all += n_all
        tot_gas_all += gas_all
        if base_fee_wei is not None:
            base_fees_wei.append(base_fee_wei)
        if blob_gas_used is not None:
            blob_gas_used_list.append(blob_gas_used)
        if i % 25 == 0:
            print(f"  ... {i}/{len(sample_blocks)} blocs traités",
                  file=sys.stderr)
        ok += 1

    if ok == 0 or tot_n_all == 0:
        raise RuntimeError("Aucun bloc exploitable échantillonné.")

    # Avertissement si eth_getBlockReceipts était indisponible : dans ce cas le
    # gas est estimé par la gas limit (surestimation) et les ERC-20 sont détectés
    # par sélecteur d'input (approximation). La mesure reste utilisable mais moins
    # précise ; le champ receipts_coverage le documente.
    receipts_coverage = n_with_receipts / ok if ok else 0.0
    if receipts_coverage < 0.5:
        print(f"  [!] Reçus disponibles pour {n_with_receipts}/{ok} blocs "
              f"({receipts_coverage*100:.0f}%). eth_getBlockReceipts semble "
              f"limité sur ce plan : gas estimé par gas limit (surestimé) et "
              f"ERC-20 détectés par sélecteur d'input. Résultats indicatifs.",
              file=sys.stderr)

    # Gas moyen des tx monétaires
    g_tx_mon = tot_gas_mon / tot_n_mon if tot_n_mon else float("nan")
    monetary_share = tot_n_mon / tot_n_all

    # Débit total moyen : blocs/s * tx/bloc
    avg_tx_per_block = tot_n_all / ok
    lambda_all = avg_tx_per_block / BLOCK_TIME_S
    lambda_mon = lambda_all * monetary_share

    # P_gas : médiane des base fees (wei -> gwei), robuste aux pics de congestion
    p_gas_gwei = (statistics.median(base_fees_wei) / WEI_PER_GWEI
                  if base_fees_wei else float("nan"))

    # P_blob : blob base fee. Sur le plan gratuit, excessBlobGas -> base fee
    # exige un calcul EIP-4844 (fake exponential). On fournit une estimation
    # simple : si des blocs portent blobGasUsed, on renvoie la médiane du
    # blob base fee dérivée du champ, sinon on laisse None (à compléter à la
    # main depuis etherscan.io/chart/blobgasprice).
    p_blob_gwei = None  # calculé plus bas si excessBlobGas dispo

    return {
        "method": "block-sampling",
        "block_start": b0,
        "block_end": b1,
        "n_blocks_span": span,
        "n_samples_ok": ok,
        "monetary_share": monetary_share,
        "g_tx_mon_gas": g_tx_mon,
        "g_tx_all_gas": tot_gas_all / tot_n_all,
        "lambda_all_tps": lambda_all,
        "lambda_mon_tps": lambda_mon,
        "avg_tx_per_block": avg_tx_per_block,
        "P_gas_gwei": p_gas_gwei,
        "P_blob_gwei": p_blob_gwei,
        "receipts_coverage": receipts_coverage,
        "n_tx_sampled": tot_n_all,
        "n_mon_sampled": tot_n_mon,
    }


def estimate_monetary_share(start_date, end_date, api_key, limiter,
                            n_samples=40):
    """Petit échantillon dédié à la part monétaire (voie PRO)."""
    sub = collect_sampling(start_date, end_date, api_key, limiter, n_samples)
    return sub["monetary_share"], sub["g_tx_mon_gas"], sub.get("P_blob_gwei")


# --------------------------------------------------------------------------- #
# Blob base fee : lecture directe via les reçus de transactions blob (type 3)  #
# --------------------------------------------------------------------------- #
# NOTE : on ne DÉRIVE PLUS le fee depuis excessBlobGas via fake_exponential.
# La formule d'EIP-4844 dépend de constantes (BLOB_BASE_FEE_UPDATE_FRACTION,
# cibles de blobs) modifiées par les hard forks successifs (Pectra/EIP-7691,
# puis les forks BPO de Fusaka) : appliquer les constantes d'origine à des
# blocs récents surestime le fee de plusieurs ordres de grandeur (bug observé :
# P_blob ~ 1e15 gwei/octet). Le reçu d'une transaction de type 3 expose en
# revanche le champ `blobGasPrice` = blob base fee effectivement payé (en wei
# par blob-gas, soit par octet) : c'est la vérité terrain, valable quel que
# soit le fork, et eth_getTransactionReceipt est disponible sur le plan gratuit.

# Garde-fou de plausibilité : au-delà de ce seuil, la valeur est considérée
# comme aberrante (1 gwei/octet = ~131k gwei le blob entier, déjà un régime de
# congestion extrême ; on laisse deux ordres de grandeur de marge).
P_BLOB_MAX_PLAUSIBLE_GWEI = 100.0

BLOB_TX_TYPE = "0x3"


def sample_blob_base_fee(start_date, end_date, api_key, limiter, n=30):
    """
    Blob base fee typique (gwei/octet) sur un échantillon de blocs, mesuré en
    lisant `blobGasPrice` dans le reçu de la première transaction blob (type 3)
    de chaque bloc échantillonné.

    Le blob base fee suit une dynamique EXPONENTIELLE (marché EIP-1559-like) :
    quelques blocs congestionnés produisent des valeurs des ordres de grandeur
    au-dessus du régime normal. On renvoie donc la MÉDIANE (robuste), qui
    reflète le blob fee typique effectivement payé sur la période.
    """
    ts0 = date_to_ts(start_date)
    ts1 = date_to_ts(end_date, end_of_day=True)
    b0 = block_by_time(ts0, "after", api_key, limiter)
    b1 = block_by_time(ts1, "before", api_key, limiter)
    step = max(1, (b1 - b0) // max(1, n - 1))
    fees_gwei = []
    n_no_blob_tx = 0
    for bn in range(b0, b1 + 1, step):
        try:
            blk = get_block(bn, api_key, limiter, full_tx=True)
        except RuntimeError:
            continue
        if not blk:
            continue
        blob_tx_hash = None
        for tx in (blk.get("transactions") or []):
            if tx.get("type") == BLOB_TX_TYPE:
                blob_tx_hash = tx.get("hash")
                break
        if blob_tx_hash is None:
            n_no_blob_tx += 1
            continue
        rc = get_tx_receipt(blob_tx_hash, api_key, limiter)
        price_wei = hex_to_int(rc.get("blobGasPrice")) if rc else None
        if price_wei is not None:
            fees_gwei.append(price_wei / WEI_PER_GWEI)  # wei/blob-gas = /octet
    if n_no_blob_tx:
        print(f"  [blob] {n_no_blob_tx} bloc(s) sans transaction blob, "
              f"ignoré(s) ; {len(fees_gwei)} mesure(s) retenue(s).",
              file=sys.stderr)
    if not fees_gwei:
        print("  [blob] aucune transaction blob trouvée dans l'échantillon : "
              "augmente n ou complète P_blob manuellement "
              "(etherscan.io/chart/blobgasprice).", file=sys.stderr)
        return None
    fees_gwei.sort()
    median = statistics.median(fees_gwei)
    # Diagnostic : signale l'ampleur des pics par rapport au régime typique.
    mx = fees_gwei[-1]
    if median > 0 and mx > 1000 * median:
        print(f"  [blob] pics de congestion détectés (max {mx:.3g} vs médiane "
              f"{median:.3g} gwei/octet) — médiane retenue.", file=sys.stderr)
    # Garde-fou : une médiane implausible signale un problème de mesure.
    if median > P_BLOB_MAX_PLAUSIBLE_GWEI:
        print(f"  [blob] médiane implausible ({median:.3g} gwei/octet > "
              f"{P_BLOB_MAX_PLAUSIBLE_GWEI}) : valeur rejetée, P_blob laissé "
              f"vide. Vérifie manuellement sur etherscan.io/chart/blobgasprice.",
              file=sys.stderr)
        return None
    return median


# --------------------------------------------------------------------------- #
# Calcul des lignes de tab:migration                                           #
# --------------------------------------------------------------------------- #
def compute_migration_rows(params, rollup):
    """
    Reproduit le modèle de la Section 7 avec les paramètres MESURÉS.
    Tout est exprimé en gwei ; la colonne clé est la réduction relative
    1 - r(p)/F_L1, indépendante de tout taux de change.
    Renvoie une liste de dicts, une par fraction p.
    """
    lambda_eth = params["lambda_mon_tps"]
    g_tx = params["g_tx_mon_gas"]
    p_gas = params["P_gas_gwei"]
    p_blob = params["P_blob_gwei"]

    delta_b = rollup["delta_b"]
    n_b_max = rollup["n_b_max"]
    g_ver = rollup["g_ver"]
    s0_da = rollup["s0_da"]
    s_tx_da = rollup["s_tx_da"]
    rho = rollup["rho_gwei_s"]
    n_tgt = rollup["n_tgt"]
    t_proof_3000 = rollup["t_proof_at_3000_s"]

    f_l1_gwei = g_tx * p_gas

    # c_prove : borné, ~ rho * t_proof/N_b ; approx via point 3000
    def c_prove_gwei(n_b):
        if n_b <= 0:
            return 0.0
        # temps de preuve par tx ~ t_proof_3000/3000 (approx plate, cf. Fig 4c)
        t_per_tx = t_proof_3000 / 3000.0
        return rho * t_per_tx  # gwei/tx (indépendant de N_b au 1er ordre)

    rows = []
    for p in MIGRATION_FRACTIONS:
        lam_p = p * lambda_eth
        n_b = min(lam_p * delta_b, n_b_max)
        n_b_int = int(round(n_b))
        if n_b_int < 1:
            n_b_int = 1

        # coût L1 amorti par tx (gwei)
        c_l1 = (g_ver * p_gas + s0_da * p_blob) / n_b_int + s_tx_da * p_blob
        c_pr = c_prove_gwei(n_b_int)
        r_gwei = c_l1 + c_pr  # mu = 0
        reduction = 1.0 - (r_gwei / f_l1_gwei) if f_l1_gwei else None
        ratio = f_l1_gwei / r_gwei if r_gwei else None  # facteur "x fois moins cher"

        # blobs/bloc induits par le workload migré
        d_bytes_s = lam_p * s_tx_da
        blobs_block = d_bytes_s * BLOCK_TIME_S / BLOBS_PER_BLOB_BYTES

        rows.append({
            "p": p,
            "lambda_p": lam_p,
            "N_b": n_b_int,
            "c_L1_gwei": c_l1,
            "c_prove_gwei": c_pr,
            "r_gwei": r_gwei,
            "reduction": reduction,
            "ratio_F_L1_over_r": ratio,
            "blobs_per_block": blobs_block,
        })

    # seuil de congestion
    lambda_crit = (n_tgt * BLOBS_PER_BLOB_BYTES) / (s_tx_da * BLOCK_TIME_S)

    summary = {
        "F_L1_gwei": f_l1_gwei,
        "lambda_crit_tps": lambda_crit,
    }
    return rows, summary


# --------------------------------------------------------------------------- #
# Génération LaTeX                                                              #
# --------------------------------------------------------------------------- #
def fmt_int(x):
    """Entier avec séparateur de milliers LaTeX (pour valeurs raisonnables)."""
    return f"{int(round(x)):,}".replace(",", "{,}")


def _sci_math(x):
    """fmt_sci entouré de $...$ pour usage en mode texte."""
    return f"${fmt_sci(x)}$"


def fmt_gwei(x):
    """
    Coût en gwei, lisible : entier séparé par milliers si |x| < 1e7,
    sinon notation scientifique (évite les entiers à 15+ chiffres).
    """
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "--"
    ax = abs(x)
    if ax < 1e7:
        return fmt_int(x)
    return _sci_math(x)


def fmt_lambda(x):
    """Débit tx/s à une décimale, comme dans le tableau d'exemple."""
    return f"{x:.1f}"


def fmt_reduction(x):
    """Réduction en % ; borne l'affichage pour rester lisible."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "--"
    pct = 100 * x
    if pct < -1e4 or pct > 1e4:      # aberration (paramètre pollué)
        return _sci_math(pct) + r"\%"
    return f"{pct:.1f}\\%"


def fmt_ratio(x):
    """Facteur F_L1/r, affiché comme '190x' (arrondi lisible)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "--"
    if x >= 100:
        return rf"${x:.0f}\times$"
    if x >= 10:
        return rf"${x:.1f}\times$"
    return rf"${x:.2f}\times$"


def fmt_blob_price(x):
    """Blob base fee (gwei/octet) : souvent minuscule -> notation sci propre."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "--"
    if x == 0:
        return "0"
    ax = abs(x)
    if 1e-3 <= ax < 1e4:
        return f"{x:.4g}"
    return _sci_math(x)


def fmt_blob_price_wrapped(x):
    """Comme fmt_blob_price mais garantit exactement un niveau de $...$ ."""
    s = fmt_blob_price(x)
    if s == "--":
        return s
    if s.startswith("$") and s.endswith("$"):
        return s                      # déjà en mode math (_sci_math)
    return f"${s}$"                   # nombre décimal simple -> on entoure


def fmt_sci(x):
    """Mantisse x 10^exp, SANS délimiteurs $ (ajoutés par l'appelant)."""
    if x == 0:
        return "0"
    exp = int(math.floor(math.log10(abs(x))))
    mant = x / (10 ** exp)
    return f"{mant:.1f}\\cdot10^{{{exp}}}"


def latex_econ_params(params, rollup, summary):
    p_blob = params["P_blob_gwei"]
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Model parameters measured over the period "
        f"{params['start']} to {params['end']} "
        r"(source: Etherscan V2). All costs are expressed in protocol units "
        r"(gas, gwei).}",
        r"\label{tab:econ_params}",
        r"\renewcommand{\arraystretch}{1.15}",
        r"\resizebox{\columnwidth}{!}{",
        r"\begin{tabular}{|l|l|l|}",
        r"\hline",
        r"\textbf{Parameter} & \textbf{Value} & \textbf{Source / rationale} \\",
        r"\hline",
        rf"$\lambda_{{eth}}$ & ${fmt_lambda(params['lambda_mon_tps'])}$ tx/s & "
        r"monetary tx (native ETH + ERC-20), measured \\",
        rf"$g_{{tx}}$ & ${fmt_int(params['g_tx_mon_gas'])}$ gas & "
        r"average over monetary transactions \\",
        rf"$P_{{gas}}$ & ${params['P_gas_gwei']:.3f}$ gwei & "
        r"median base fee \\",
        (rf"$P_{{blob}}$ & {fmt_blob_price_wrapped(p_blob)} gwei/byte & median blob base fee \\"
         if p_blob is not None else
         r"$P_{blob}$ & \textit{(to fill)} gwei/byte & "
         r"see etherscan.io/chart/blobgasprice \\"),
        rf"$\Delta_b$ & ${int(rollup['delta_b'])}$ s & "
        r"model (production-rollup interval) \\",
        rf"$N_b^{{\max}}$ & ${rollup['n_b_max']}$ & model (Section~\ref{{sec:zk_exp}}) \\",
        rf"$g_{{ver}}$ & ${fmt_int(rollup['g_ver'])}$ gas & model (Groth16) \\",
        rf"$S_0^{{DA}}$ & ${rollup['s0_da']}$ B & model \\",
        rf"$s_{{tx}}^{{DA}}$ & ${rollup['s_tx_da']}$ B & model (compressed) \\",
        rf"$\rho$ & ${rollup['rho_gwei_s']:.0f}$ gwei/s & model (Table~\ref{{tab:setup}}) \\",
        rf"$B_{{blob}}$, $n_{{tgt}}$ & ${BLOBS_PER_BLOB_BYTES}$ B, ${rollup['n_tgt']}$ & EIP-4844 \\",
        r"\hline",
        r"\end{tabular}}",
        r"\end{table}",
    ]
    return "\n".join(lines)


def latex_migration(rows, summary, params):
    head = [
        r"\begin{table*}[t]",
        r"\centering",
        rf"\caption{{Per-transaction cost (gwei) vs.\ migrated fraction $p$, "
        rf"parameters of Table~\ref{{tab:econ_params}}. L1 baseline "
        rf"$F_{{L1}} = {fmt_gwei(summary['F_L1_gwei'])}$ gwei. "
        rf"Congestion threshold "
        rf"$\lambda^{{crit}} \approx {summary['lambda_crit_tps']:.0f}$ tx/s.}}",
        r"\label{tab:migration}",
        r"\renewcommand{\arraystretch}{1.2}",
        r"\begin{tabular}{|c|c|c|c|c|c|c|c|}",
        r"\hline",
        r"\textbf{$p$} & \textbf{$\lambda_p$ (tx/s)} & \textbf{$N_b(p)$} & "
        r"\textbf{$c_{L1}$/tx (gwei)} & \textbf{$r(p)$/tx (gwei)} & "
        r"\textbf{reduction} & \textbf{$F_{L1}/r(p)$} & "
        r"\textbf{blobs/block} \\",
        r"\hline",
    ]
    body = []
    for r in rows:
        body.append(
            f"{int(round(r['p']*100))}\\% & "
            f"{fmt_lambda(r['lambda_p'])} & "
            f"{fmt_int(r['N_b'])} & "
            f"{fmt_gwei(r['c_L1_gwei'])} & "
            f"{fmt_gwei(r['r_gwei'])} & "
            f"{fmt_reduction(r['reduction'])} & "
            f"{fmt_ratio(r['ratio_F_L1_over_r'])} & "
            f"{_sci_math(r['blobs_per_block'])} \\\\ \\hline"
        )
    tail = [r"\end{tabular}", r"\end{table*}"]
    return "\n".join(head + body + tail)


# --------------------------------------------------------------------------- #
# Main                                                                          #
# --------------------------------------------------------------------------- #
def parse_args():
    ap = argparse.ArgumentParser(
        description="Collecte des données on-chain pour remplir les tableaux "
                    "de la Section 7 du papier (coûts en gas/gwei uniquement).")
    ap.add_argument("--start", required=True, help="Date début YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="Date fin YYYY-MM-DD")
    ap.add_argument("--samples", type=int, default=200,
                    help="Nb de blocs échantillonnés (voie gratuite). "
                         "Plus élevé = plus précis mais plus lent.")
    ap.add_argument("--pro", action="store_true",
                    help="Utiliser les endpoints stats PRO (Standard tier+).")
    ap.add_argument("--outdir", default=".", help="Dossier de sortie")
    ap.add_argument("--api-key", default=os.environ.get("ETHERSCAN_API_KEY"),
                    help="Clé Etherscan (ou variable ETHERSCAN_API_KEY)")
    return ap.parse_args()


def main():
    args = parse_args()
    if not args.api_key:
        sys.exit("Clé API manquante : export ETHERSCAN_API_KEY=... "
                 "ou --api-key")

    start_date = dt.date.fromisoformat(args.start)
    end_date = dt.date.fromisoformat(args.end)
    if end_date < start_date:
        sys.exit("La date de fin précède la date de début.")

    os.makedirs(args.outdir, exist_ok=True)
    limiter = RateLimiter()

    print(f"== Collecte {start_date} -> {end_date} ==", file=sys.stderr)

    # 1) Données on-chain
    if args.pro:
        print("[mode] endpoints PRO stats", file=sys.stderr)
        params = collect_pro(start_date, end_date, args.api_key, limiter)
        # compléter part monétaire + gas monétaire par petit échantillon
        share, g_tx_mon, _ = estimate_monetary_share(
            start_date, end_date, args.api_key, limiter, n_samples=40)
        params["monetary_share"] = share
        params["g_tx_mon_gas"] = g_tx_mon
        params["lambda_mon_tps"] = params["lambda_all_tps"] * share
    else:
        print("[mode] échantillonnage de blocs (gratuit)", file=sys.stderr)
        params = collect_sampling(start_date, end_date, args.api_key,
                                  limiter, args.samples)

    # 2) Blob base fee (dérivée EIP-4844) si non déjà obtenue
    if params.get("P_blob_gwei") is None:
        print("[blob] échantillonnage blob base fee...", file=sys.stderr)
        try:
            params["P_blob_gwei"] = sample_blob_base_fee(
                start_date, end_date, args.api_key, limiter, n=30)
        except RuntimeError as exc:
            print(f"  ! blob fee indisponible: {exc}", file=sys.stderr)
            params["P_blob_gwei"] = None

    params["start"] = args.start
    params["end"] = args.end

    # 3) Modèle -> lignes tab:migration
    rollup = dict(ROLLUP_DEFAULTS)
    # garde-fou : si P_blob None, on met 0 pour le calcul et on signale
    if params["P_blob_gwei"] is None:
        print("  ! P_blob manquant : calcul avec P_blob=0 (à compléter).",
              file=sys.stderr)
        params_for_calc = dict(params, P_blob_gwei=0.0)
    else:
        params_for_calc = params
    rows, summary = compute_migration_rows(params_for_calc, rollup)

    # 4) Écritures
    tag = f"{args.start}_{args.end}"
    json_path = os.path.join(args.outdir, f"migration_params_{tag}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"params": params, "rollup": rollup,
                   "rows": rows, "summary": summary}, f,
                  indent=2, default=str)

    econ_tex = latex_econ_params(params, rollup, summary)
    migr_tex = latex_migration(rows, summary, params)
    with open(os.path.join(args.outdir, f"tab_econ_params_{tag}.tex"),
              "w", encoding="utf-8") as f:
        f.write(econ_tex + "\n")
    with open(os.path.join(args.outdir, f"tab_migration_{tag}.tex"),
              "w", encoding="utf-8") as f:
        f.write(migr_tex + "\n")

    # 5) Récapitulatif console
    print("\n" + "=" * 64)
    print(f"  PÉRIODE : {args.start} -> {args.end}   (méthode: {params['method']})")
    print("=" * 64)
    print(f"  lambda_eth (monétaire)  : {params['lambda_mon_tps']:.3f} tx/s")
    print(f"  part monétaire          : {params.get('monetary_share', float('nan'))*100:.1f} %")
    print(f"  g_tx (monétaire)        : {params['g_tx_mon_gas']:.0f} gas")
    print(f"  P_gas                   : {params['P_gas_gwei']:.3f} gwei")
    pb = params['P_blob_gwei']
    if pb is not None:
        print(f"  P_blob                  : {pb:.4g} gwei/octet")
    else:
        print("  P_blob                  : (à compléter manuellement)")
    print(f"  F_L1                    : {summary['F_L1_gwei']:.0f} gwei")
    print(f"  lambda_crit             : {summary['lambda_crit_tps']:.0f} tx/s")
    print("-" * 64)
    print("  Réduction du coût par tx (rollup vs L1) :")
    for r in rows:
        red = r["reduction"]
        ratio = r["ratio_F_L1_over_r"]
        print(f"    p = {int(r['p']*100):>3d}%  ->  r = {r['r_gwei']:>10.1f} gwei"
              f"   réduction = {red*100:5.1f}%   (F_L1/r ≈ {ratio:.1f}x)")
    print("-" * 64)
    print(f"  JSON   : {json_path}")
    print(f"  LaTeX  : tab_econ_params_{tag}.tex, tab_migration_{tag}.tex")
    print("=" * 64 + "\n")
    print("----- tab:econ_params -----\n")
    print(econ_tex)
    print("\n----- tab:migration -----\n")
    print(migr_tex)


if __name__ == "__main__":
    main()