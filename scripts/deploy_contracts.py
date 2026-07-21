"""
Déploiement automatique sur Sepolia :
  1. Compile chaque circuits/{N}/verifier.sol (snarkjs Groth16) avec solc 0.8.24.
  2. Déploie chaque verifier et écrit son adresse dans .env (CIRCUIT_{N}_ADDRESS).
  3. Compile et déploie smart_contract/Rollup.sol avec un state root initial
     dérivé du dictionnaire de balances initial (NB_INITIAL_WALLET × INITIAL_BALANCE).
  4. Enregistre chaque verifier auprès du Rollup via setVerifier.
  5. Écrit ROLLUP_ADDRESS dans .env.

Usage :
    python scripts/deploy_contracts.py [--sizes 1,2,4,8,16,32,64]
    python scripts/deploy_contracts.py --reset          # repart d'un .env propre
    python scripts/deploy_contracts.py --skip-existing   # ne redéploie que le manquant

Pré-requis dans .env :
    WS_ADDRESS=https://sepolia.infura.io/v3/<key>   (HTTP ou WSS)
    WALLET_ADDRESS=0x...
    WALLET_PRIVATE_KEY=0x...
    INITIAL_BALANCE, NB_INITIAL_WALLET (pour calculer le state root initial)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv, set_key
from web3 import Web3
from eth_account import Account
import solcx


SOLC_VERSION = "0.8.24"
DEFAULT_SIZES = [1, 2, 4, 8, 16, 32, 64]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CIRCUITS_DIR = PROJECT_ROOT / "circuits"
ROLLUP_SOL = PROJECT_ROOT / "smart_contract" / "Rollup.sol"
ENV_PATH = PROJECT_ROOT / ".env"


def ensure_solc(version: str = SOLC_VERSION) -> None:
    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if version not in installed:
        print(f"[deploy] solc {version} non installe -- installation...")
        solcx.install_solc(version)
    solcx.set_solc_version(version)


def compile_solidity(path: Path, contract_name: str) -> dict:
    """Compile un .sol et retourne {'abi': [...], 'bin': '0x...'} pour `contract_name`."""
    source = path.read_text(encoding="utf-8")
    compiled = solcx.compile_source(
        source,
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
        # Cancun requis pour `blobhash(...)` (opcode BLOBHASH, EIP-4844).
        evm_version="cancun",
    )
    # solcx renvoie une clé du type "<stdin>:Groth16Verifier"
    target_key = next(
        (k for k in compiled.keys() if k.endswith(f":{contract_name}")),
        None,
    )
    if target_key is None:
        raise RuntimeError(
            f"Contrat '{contract_name}' introuvable dans {path}. "
            f"Clés compilées : {list(compiled.keys())}"
        )
    return compiled[target_key]


def wait_receipt(w3: Web3, tx_hash, label: str, timeout: int = 300):
    print(f"[deploy] {label} -> tx {tx_hash.hex()} (attente...)")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    if receipt.status != 1:
        raise RuntimeError(f"{label}: transaction echouee (status=0)")
    return receipt


def deploy_contract(w3: Web3, account, abi: list, bytecode: str, args: tuple, label: str) -> str:
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = contract.constructor(*args).build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": w3.eth.chain_id,
            # EIP-1559
            "maxFeePerGas": w3.to_wei(50, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(2, "gwei"),
        }
    )
    # Estimation de gas après build pour bénéficier de tous les champs
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = account.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = wait_receipt(w3, tx_hash, label)
    print(f"[deploy] {label} déployé à {receipt.contractAddress}")
    return receipt.contractAddress


def send_call(w3: Web3, account, contract, fn_name: str, args: tuple, label: str):
    fn = contract.functions[fn_name](*args)
    tx = fn.build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": w3.eth.chain_id,
            "maxFeePerGas": w3.to_wei(50, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(2, "gwei"),
        }
    )
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = account.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    wait_receipt(w3, tx_hash, label)


def reset_env_addresses() -> None:
    """
    Remet à zéro (chaîne vide) toutes les adresses de contrats déjà présentes
    dans le .env : chaque CIRCUIT_{N}_ADDRESS et ROLLUP_ADDRESS. Permet de
    repartir d'un environnement propre avant un redéploiement complet.

    Met à jour à la fois le fichier .env et os.environ pour que le reste du
    script ne voie plus les anciennes adresses.
    """
    addr_key_re = re.compile(r"^(CIRCUIT_\d+_ADDRESS|ROLLUP_ADDRESS)$")
    keys: list[str] = []
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if addr_key_re.match(key):
                keys.append(key)

    if not keys:
        print("[deploy] --reset : aucune adresse de contrat à réinitialiser dans .env.")
        return

    for key in keys:
        set_key(str(ENV_PATH), key, "")
        os.environ.pop(key, None)
    print(f"[deploy] --reset : {len(keys)} adresse(s) réinitialisée(s) dans .env "
          f"({', '.join(keys)}).")


def initial_state_root() -> bytes:
    """
    Reproduit State.hash() sur les balances initiales pour garantir
    la cohérence avec ce que le Prover envoie.
    """
    initial_balance = float(os.getenv("INITIAL_BALANCE"))
    nb_initial_wallet = int(os.getenv("NB_INITIAL_WALLET"))
    balances = {str(i): initial_balance for i in range(nb_initial_wallet)}
    state_json = json.dumps(dict(sorted(balances.items())), sort_keys=True)
    digest_hex = hashlib.sha256(state_json.encode()).hexdigest()
    return bytes.fromhex(digest_hex)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sizes",
        type=str,
        default=",".join(map(str, DEFAULT_SIZES)),
        help="Tailles de batch à déployer, séparées par des virgules.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Ne redéploie pas un verifier dont CIRCUIT_{N}_ADDRESS est déjà non nul dans .env.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Vide toutes les adresses de contrats (CIRCUIT_*_ADDRESS et ROLLUP_ADDRESS) "
             "du .env avant de redéployer, pour repartir d'un environnement propre.",
    )
    args = parser.parse_args()

    if args.reset and args.skip_existing:
        sys.exit("[deploy] --reset et --skip-existing sont incompatibles "
                 "(--reset force un redéploiement complet).")

    load_dotenv(ENV_PATH)

    if args.reset:
        reset_env_addresses()
    rpc_url = os.getenv("WS_ADDRESS")
    wallet_address = os.getenv("WALLET_ADDRESS")
    private_key = os.getenv("WALLET_PRIVATE_KEY")

    if not rpc_url or rpc_url == "0":
        sys.exit("[deploy] WS_ADDRESS manquant dans .env (URL HTTP ou WSS Sepolia).")
    if not private_key or private_key == "0":
        sys.exit("[deploy] WALLET_PRIVATE_KEY manquant dans .env.")

    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    for s in sizes:
        if not (CIRCUITS_DIR / str(s) / "verifier.sol").exists():
            sys.exit(f"[deploy] circuits/{s}/verifier.sol introuvable.")

    if rpc_url.startswith("ws"):
        ws_provider = (
            getattr(Web3, "LegacyWebSocketProvider", None)
            or getattr(Web3, "WebsocketProvider", None)
        )
        if ws_provider is None:
            sys.exit("[deploy] Aucun provider WebSocket disponible dans web3.py.")
        provider = ws_provider(rpc_url)
    else:
        provider = Web3.HTTPProvider(rpc_url)
    w3 = Web3(provider)
    if not w3.is_connected():
        sys.exit(f"[deploy] Impossible de se connecter à {rpc_url}.")

    account = Account.from_key(private_key)
    if wallet_address and wallet_address != "0" and wallet_address.lower() != account.address.lower():
        print(
            f"[deploy] WARNING : WALLET_ADDRESS ({wallet_address}) "
            f"ne correspond pas à la clé privée ({account.address})."
        )

    chain_id = w3.eth.chain_id
    balance_eth = w3.from_wei(w3.eth.get_balance(account.address), "ether")
    print(f"[deploy] connecté chainId={chain_id} compte={account.address} solde={balance_eth} ETH")
    if chain_id != 11155111:
        print(f"[deploy] WARNING : chainId={chain_id} (Sepolia attendu = 11155111).")

    ensure_solc()

    # 1) Compilation et déploiement de chaque verifier.
    verifier_addresses: dict[int, str] = {}
    for size in sizes:
        env_key = f"CIRCUIT_{size}_ADDRESS"
        existing = os.getenv(env_key)
        if args.skip_existing and existing and existing != "0":
            print(f"[deploy] {env_key} déjà = {existing}, skip.")
            verifier_addresses[size] = existing
            continue

        sol_path = CIRCUITS_DIR / str(size) / "verifier.sol"
        compiled = compile_solidity(sol_path, "Groth16Verifier")
        addr = deploy_contract(
            w3,
            account,
            compiled["abi"],
            compiled["bin"],
            args=(),
            label=f"Verifier[size={size}]",
        )
        verifier_addresses[size] = addr
        set_key(str(ENV_PATH), env_key, addr)
        # Petite pause pour laisser monter le nonce sur le mempool de Sepolia
        time.sleep(1)

    # 2) Compilation et déploiement du Rollup.
    rollup_compiled = compile_solidity(ROLLUP_SOL, "Rollup")
    init_root = initial_state_root()
    rollup_address = deploy_contract(
        w3,
        account,
        rollup_compiled["abi"],
        rollup_compiled["bin"],
        args=(init_root,),
        label="Rollup",
    )
    set_key(str(ENV_PATH), "ROLLUP_ADDRESS", rollup_address)

    # Persiste l'ABI du Rollup pour le Prover.
    abi_dir = PROJECT_ROOT / "smart_contract" / "abi"
    abi_dir.mkdir(parents=True, exist_ok=True)
    (abi_dir / "Rollup.json").write_text(json.dumps(rollup_compiled["abi"], indent=2))

    # 3) Enregistrement des verifiers auprès du Rollup.
    rollup = w3.eth.contract(address=rollup_address, abi=rollup_compiled["abi"])
    for size, addr in verifier_addresses.items():
        send_call(
            w3,
            account,
            rollup,
            "setVerifier",
            (size, addr),
            label=f"setVerifier(size={size}, {addr})",
        )
        time.sleep(1)

    print("[deploy] Déploiement terminé.")
    print(f"[deploy] state_root initial : 0x{init_root.hex()}")
    print(f"[deploy] Rollup : {rollup_address}")
    for size, addr in verifier_addresses.items():
        print(f"[deploy] Verifier[{size}] : {addr}")


if __name__ == "__main__":
    main()
