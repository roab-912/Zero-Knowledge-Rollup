"""
Client L1 minimal pour Sepolia :
  - encode un blob EIP-4844 à partir des bytes d'un batch,
  - calcule le commitment + proof KZG,
  - envoie une transaction de type 3 portant à la fois la calldata
    `Rollup.submitBatch(...)` et le blob,
  - attend la confirmation et renvoie le receipt.

La même transaction transporte la calldata et le blob, ce qui garantit que
`blobhash(0)` côté contrat correspond bien au versioned hash que l'on passe
en argument de `submitBatch`.

Dépendances :
  - web3 >= 6.15 (support des blob tx)
  - ckzg (trusted setup KZG d'Ethereum)
  - eth-account >= 0.11
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

import ckzg
from eth_account import Account
from web3 import Web3


# Constantes EIP-4844
BLOB_FIELD_ELEMENTS = 4096
BYTES_PER_FIELD_ELEMENT = 32
BLOB_BYTES = BLOB_FIELD_ELEMENTS * BYTES_PER_FIELD_ELEMENT  # 131_072
USABLE_BYTES_PER_FIELD = 31  # on réserve 1 octet à 0x00 par mot pour rester < r
MAX_PAYLOAD_PER_BLOB = USABLE_BYTES_PER_FIELD * BLOB_FIELD_ELEMENTS  # 126_976

VERSIONED_HASH_VERSION_KZG = 0x01


class BlobTooLargeError(Exception):
    pass


def encode_payload_to_blob(payload: bytes) -> bytes:
    """
    Encode `payload` dans un blob de 131072 octets en respectant la contrainte
    "chaque field element < r" : on insère un octet 0x00 en tête de chaque mot
    de 32 octets. Le buffer est zero-paddé jusqu'à atteindre 128 KB.
    """
    if len(payload) > MAX_PAYLOAD_PER_BLOB:
        raise BlobTooLargeError(
            f"payload de {len(payload)} octets > {MAX_PAYLOAD_PER_BLOB} (un seul blob)"
        )

    blob = bytearray(BLOB_BYTES)
    for i in range(BLOB_FIELD_ELEMENTS):
        chunk = payload[i * USABLE_BYTES_PER_FIELD : (i + 1) * USABLE_BYTES_PER_FIELD]
        if not chunk:
            break
        # Mot 32B : [0x00 | chunk (31B max, zero-paddé)]
        slot = blob[i * BYTES_PER_FIELD_ELEMENT : (i + 1) * BYTES_PER_FIELD_ELEMENT]
        slot[0] = 0x00
        slot[1 : 1 + len(chunk)] = chunk
        blob[i * BYTES_PER_FIELD_ELEMENT : (i + 1) * BYTES_PER_FIELD_ELEMENT] = slot
    return bytes(blob)


def kzg_versioned_hash(commitment: bytes) -> bytes:
    """
    versioned_hash = 0x01 || sha256(commitment)[1:]  (EIP-4844 §helpers).
    """
    h = hashlib.sha256(commitment).digest()
    return bytes([VERSIONED_HASH_VERSION_KZG]) + h[1:]


class L1Client:
    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        trusted_setup_path: str,
        chain_id: Optional[int] = None,
    ):
        if rpc_url.startswith("ws"):
            # web3.py 7.x : LegacyWebSocketProvider (sync) ; 6.x : WebsocketProvider
            ws_provider = (
                getattr(Web3, "LegacyWebSocketProvider", None)
                or getattr(Web3, "WebsocketProvider", None)
            )
            if ws_provider is None:
                raise RuntimeError("Aucun provider WebSocket disponible dans web3.py")
            self.w3 = Web3(ws_provider(rpc_url))
        else:
            self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self.w3.is_connected():
            raise RuntimeError(f"L1Client : connexion impossible à {rpc_url}")

        self.account = Account.from_key(private_key)
        self.chain_id = chain_id if chain_id is not None else self.w3.eth.chain_id

        if not Path(trusted_setup_path).exists():
            raise FileNotFoundError(
                f"Trusted setup KZG introuvable : {trusted_setup_path}. "
                "À télécharger depuis https://github.com/ethereum/c-kzg-4844 "
                "(src/trusted_setup.txt)."
            )
        # ckzg.load_trusted_setup(path, precompute=0) — signature ckzg ≥ 1.0
        try:
            self.kzg_setup = ckzg.load_trusted_setup(trusted_setup_path, 0)
        except TypeError:
            # versions plus anciennes du binding (single arg)
            self.kzg_setup = ckzg.load_trusted_setup(trusted_setup_path)

    def build_blob_artifacts(self, payload: bytes) -> dict:
        """
        À partir d'un payload (binaire batch), produit le blob 128 KB,
        son commitment KZG, sa proof KZG et son versioned hash.
        """
        blob = encode_payload_to_blob(payload)
        commitment = ckzg.blob_to_kzg_commitment(blob, self.kzg_setup)
        proof = ckzg.compute_blob_kzg_proof(blob, commitment, self.kzg_setup)
        v_hash = kzg_versioned_hash(commitment)
        return {
            "blob": blob,
            "commitment": commitment,
            "proof": proof,
            "versioned_hash": v_hash,
        }

    def send_blob_call(
        self,
        to_address: str,
        calldata: bytes,
        blob_artifacts: dict,
        max_fee_per_gas_gwei: float = 50,
        max_priority_fee_gwei: float = 2,
        max_fee_per_blob_gas_gwei: float = 1,
        gas_limit: Optional[int] = None,
        timeout: int = 600,
    ):
        """
        Envoie une transaction de type 3 portant à la fois `calldata` et un blob.
        `blobhash(0)` côté contrat sera donc égal à blob_artifacts['versioned_hash'].
        """
        nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")

        if gas_limit is None:
            try:
                gas_limit = int(
                    self.w3.eth.estimate_gas(
                        {
                            "from": self.account.address,
                            "to": to_address,
                            "data": calldata,
                        }
                    )
                    * 1.3
                )
            except Exception:
                gas_limit = 1_500_000

        tx = {
            "type": 3,
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": Web3.to_checksum_address(to_address),
            "value": 0,
            "data": calldata,
            "gas": gas_limit,
            "maxFeePerGas": self.w3.to_wei(max_fee_per_gas_gwei, "gwei"),
            "maxPriorityFeePerGas": self.w3.to_wei(max_priority_fee_gwei, "gwei"),
            "maxFeePerBlobGas": self.w3.to_wei(max_fee_per_blob_gas_gwei, "gwei"),
            "blobVersionedHashes": [blob_artifacts["versioned_hash"]],
        }

        signed = self.account.sign_transaction(
            tx,
            blobs=[blob_artifacts["blob"]],
        )
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        print(f"[L1Client] Blob tx envoyée : {tx_hash.hex()}")
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        if receipt.status != 1:
            raise RuntimeError(f"Blob tx échouée (status=0) — hash={tx_hash.hex()}")
        return receipt

    @classmethod
    def from_env(cls) -> Optional["L1Client"]:
        """
        Construit un L1Client à partir des variables d'environnement, ou retourne
        None si la configuration on-chain n'est pas (encore) renseignée.
        """
        rpc = os.getenv("WS_ADDRESS")
        pk = os.getenv("WALLET_PRIVATE_KEY")
        setup_path = os.getenv("TRUSTED_SETUP_PATH", "trusted_setup.txt")
        if not rpc or rpc == "0" or not pk or pk == "0":
            return None
        return cls(rpc_url=rpc, private_key=pk, trusted_setup_path=setup_path)
