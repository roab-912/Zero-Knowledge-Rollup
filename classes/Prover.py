from models.Batch import Batch
from models.State import State

import ast
import json
import os
import shutil
import time
from pathlib import Path

from classes.L1Client import L1Client


class Prover:
    """
    Génère et vérifie les preuves Groth16 pour un batch, puis (optionnellement)
    soumet le résultat sur Sepolia via une transaction blob EIP-4844 portant
    à la fois la calldata `Rollup.submitBatch(...)` et le blob de données.

    La séparation est volontaire :
      - prove_batch     : phase off-chain (snarkjs / rapidsnark)
      - submit_on_chain : phase on-chain (L1Client + Rollup.sol)
    """

    def __init__(self, state: State, mode: str = "rapidsnark"):
        self.state = state
        self.size = 0
        self.mode = mode

        # Chargement (best-effort) du Rollup déployé : ABI + adresse depuis .env.
        self.l1_client: L1Client = None
        self.rollup = None
        self.rollup_address: str = None
        self._load_rollup_from_env()

    # ------------------------------------------------------------------
    #  Initialisation L1
    # ------------------------------------------------------------------
    def _load_rollup_from_env(self) -> None:
        """
        Charge l'ABI et l'adresse du Rollup, ainsi qu'un L1Client.
        Si l'une des informations manque, on continue en mode off-chain only
        (les méthodes prove_batch resteront fonctionnelles).
        """
        try:
            self.l1_client = L1Client.from_env()
        except Exception as exc:
            print(f"[Prover] L1Client indisponible : {exc}")
            self.l1_client = None

        rollup_address = os.getenv("ROLLUP_ADDRESS")
        if not rollup_address or rollup_address == "0":
            print("[Prover] ROLLUP_ADDRESS non défini — soumission on-chain désactivée.")
            return

        abi_path = Path("smart_contract/abi/Rollup.json")
        if not abi_path.exists():
            print(
                "[Prover] smart_contract/abi/Rollup.json introuvable — "
                "exécutez scripts/deploy_contracts.py."
            )
            return

        if self.l1_client is None:
            return

        abi = json.loads(abi_path.read_text(encoding="utf-8"))
        self.rollup_address = self.l1_client.w3.to_checksum_address(rollup_address)
        self.rollup = self.l1_client.w3.eth.contract(address=self.rollup_address, abi=abi)
        print(f"[Prover] Rollup connecté à {self.rollup_address}")

    def can_submit_on_chain(self) -> bool:
        return self.rollup is not None and self.l1_client is not None

    # ------------------------------------------------------------------
    #  Off-chain : preuve + vérif locale
    # ------------------------------------------------------------------
    def prove_batch(self, batch: Batch, new_state: State) -> bool:
        """
        Génère la preuve, la vérifie localement, et retourne True si elle
        est valide. Ne touche pas à `self.state` — la mise à jour d'état est
        décidée par ZKRollup après soumission on-chain.
        """
        self.size = len(batch.transactions)
        if self.size == 0:
            return None

        self.__write_input_file__(batch, new_state)

        start = time.time()
        if self.mode == "rapidsnark":
            self.__create_witness__()
            self.__create_proof_rapidsnark__()
        elif self.mode == "snarkjs":
            self.__create_witness_and_proof__()
        print(f"[Prover] Proof generated in {time.time() - start:.2f} s.")

        self.__store_proof__(str(batch.id))

        start = time.time()
        self.__verify_proof__()
        print(f"[Prover] Proof verify in {time.time() - start:.2f} s.")

        ok = self.__read_public_file__()
        print(f"[Prover] Preuve {'acceptée' if ok else 'refusée'}")
        return ok

    # ------------------------------------------------------------------
    #  On-chain : submitBatch via blob tx
    # ------------------------------------------------------------------
    def submit_on_chain(
        self,
        batch: Batch,
        state_root_before: bytes,
        state_root_after: bytes,
        blob_payload: bytes,
    ) -> bool:
        """
        Construit la calldata Groth16 via snarkjs, prépare le blob KZG,
        et envoie une transaction de type 3 à Rollup.submitBatch.
        Retourne True si l'événement BatchSubmitted est émis avec succès.
        """
        if not self.can_submit_on_chain():
            print("[Prover] Soumission on-chain désactivée (Rollup ou L1Client non chargé).")
            return False

        a, b, c, public_inputs = self.__export_calldata__()

        artifacts = self.l1_client.build_blob_artifacts(blob_payload)
        versioned_hash = artifacts["versioned_hash"]

        submit_args = [
            self.size,
            a,
            b,
            c,
            public_inputs,
            state_root_before,
            state_root_after,
            versioned_hash,
        ]
        # web3.py 7.x : encode_abi(abi_element_identifier=...)
        # web3.py 6.x : encodeABI(fn_name=...)
        if hasattr(self.rollup, "encode_abi"):
            calldata_hex = self.rollup.encode_abi(
                abi_element_identifier="submitBatch", args=submit_args
            )
        else:
            calldata_hex = self.rollup.encodeABI(fn_name="submitBatch", args=submit_args)
        calldata = bytes.fromhex(calldata_hex[2:] if calldata_hex.startswith("0x") else calldata_hex)

        try:
            receipt = self.l1_client.send_blob_call(
                to_address=self.rollup_address,
                calldata=calldata,
                blob_artifacts=artifacts,
            )
        except Exception as exc:
            print(f"[Prover] Soumission on-chain échouée : {exc}")
            return False

        # Vérifie qu'un événement BatchSubmitted a été émis.
        events = self.rollup.events.BatchSubmitted().process_receipt(receipt)
        if not events:
            print("[Prover] Receipt OK mais aucun événement BatchSubmitted détecté.")
            return False
        ev = events[0]["args"]
        print(
            f"[Prover] Batch {ev['batchId']} soumis on-chain — "
            f"newStateRoot=0x{ev['stateRootAfter'].hex()}"
        )
        return True

    def __export_calldata__(self):
        """
        Exporte la calldata Solidity via snarkjs et la parse en tuples Python
        (a, b, c, public_inputs) au format attendu par Rollup.submitBatch.
        """
        out_path = f"./circuits/{self.size}/calldata.txt"
        cmd = (
            f"snarkjs zkey export soliditycalldata "
            f"./circuits/{self.size}/public.json "
            f"./circuits/{self.size}/proof.json > {out_path}"
        )
        os.system(cmd)
        raw = Path(out_path).read_text(encoding="utf-8").strip()

        # snarkjs renvoie une liste Python valide : [["0x..","0x.."],[[..],[..]],..]
        parts = ast.literal_eval(raw)
        a = [int(parts[0][0], 16), int(parts[0][1], 16)]
        b = [
            [int(parts[1][0][0], 16), int(parts[1][0][1], 16)],
            [int(parts[1][1][0], 16), int(parts[1][1][1], 16)],
        ]
        c = [int(parts[2][0], 16), int(parts[2][1], 16)]
        public_inputs = [int(parts[3][0], 16)]
        return a, b, c, public_inputs

    # ------------------------------------------------------------------
    #  Helpers existants (préservés)
    # ------------------------------------------------------------------
    def __write_input_file__(self, batch: Batch, new_state: State):
        amount = [tx.amount for tx in batch.transactions]
        src = [tx.from_address for tx in batch.transactions]
        dest = [tx.to_adress for tx in batch.transactions]

        srcBalance = [self.state.get_balance(tx.from_address) for tx in batch.transactions]
        destBalance = [self.state.get_balance(tx.to_adress) for tx in batch.transactions]
        srcBalanceAfter = [new_state.get_balance(tx.from_address) for tx in batch.transactions]
        destBalanceAfter = [new_state.get_balance(tx.to_adress) for tx in batch.transactions]

        data = {
            "src": src,
            "srcBalance": srcBalance,
            "srcBalanceAfter": srcBalanceAfter,
            "dest": dest,
            "destBalance": destBalance,
            "destBalanceAfter": destBalanceAfter,
            "amount": amount,
        }

        filepath = os.path.join(f"./circuits/{self.size}/", "input.json")
        with open(filepath, "w") as f:
            json.dump(data, f, indent=4)
        print(f"[Prover] Fichier d'input écrit dans {filepath}")

    def __create_witness__(self):
        print("[Prover] Creating witness ...")
        cmd = (
            f"snarkjs wtns calculate "
            f"./circuits/{self.size}/circuit_js/circuit.wasm "
            f"./circuits/{self.size}/input.json "
            f"./circuits/{self.size}/witness.wtns"
        )
        os.system(cmd)

    def __create_witness_and_proof__(self):
        print("[Prover] Creating Groth16 witness and proof ...")
        cmd = (
            f"snarkjs groth16 fullprove "
            f"./circuits/{self.size}/input.json "
            f"./circuits/{self.size}/circuit_js/circuit.wasm "
            f"./circuits/{self.size}/circuit_final.zkey "
            f"./circuits/{self.size}/proof.json "
            f"./circuits/{self.size}/public.json"
        )
        os.system(cmd)

    def __create_proof_rapidsnark__(self):
        print("[Prover] Creating Rapidsnark proof ...")
        cmd = (
            f"docker exec debian_rapidsnark mnt/projet/rapidsnark/package/bin/prover "
            f"mnt/projet/{self.size}/circuit_final.zkey "
            f"mnt/projet/{self.size}/witness.wtns "
            f"mnt/projet/{self.size}/proof.json "
            f"mnt/projet/{self.size}/public.json"
        )
        os.system(cmd)
        self.__clean_file__()

    def __verify_proof__(self):
        print("[Prover] Verify Groth16 proof ...")
        cmd = (
            f"snarkjs groth16 verify "
            f"./circuits/{self.size}/verification_key.json "
            f"./circuits/{self.size}/public.json "
            f"./circuits/{self.size}/proof.json"
        )
        os.system(cmd)

    def __read_public_file__(self) -> bool:
        filepath = os.path.join(f"./circuits/{self.size}/", "public.json")
        with open(filepath, "r") as f:
            public = json.load(f)
        # public[0] est une string décimale ("1" ou "0").
        # bool("0") vaut True en Python — il faut comparer la valeur entière.
        result = int(public[0]) != 0
        print(f"[Prover] Proof: {result}")
        return result

    def __clean_file__(self):
        for fname in ("public.json", "proof.json"):
            path = f"./circuits/{self.size}/{fname}"
            with open(path, "rb") as file:
                raw_data = file.read()
            cleaned = raw_data.replace(b"\x00", b"")
            data = json.loads(cleaned.decode("utf-8"))
            with open(path, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=1)

    def __store_proof__(self, id: str):
        shutil.copy2(f"./circuits/{self.size}/proof.json", f"./proofs/proof_{id}.json")
