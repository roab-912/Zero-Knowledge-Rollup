import json
import os
import threading
import time
from pathlib import Path

from classes.Sequencer import Sequencer
from classes.Executor import Executor
from classes.Prover import Prover
from classes.DataAvailability import DataAvailability

from models.State import State
from models.Batch import Batch


SNAPSHOT_PATH = Path("storage/last_committed.json")


class ZKRollup:
    def __init__(
        self,
        duration: int = 10,
        initial_balances: dict = None,
        epoch: int = 1_000,
        mode: str = "rapidsnark",
    ):
        self.duration = duration
        self.running = False
        self.batch_thread = None
        self._compute_lock = threading.Lock()

        # Charge le state depuis le snapshot disque si présent (résume après
        # un arrêt/relance), sinon utilise les balances initiales.
        loaded = self._load_snapshot()
        if loaded is not None:
            self.state = State(loaded["balances"])
            Batch._id_counter = max(Batch._id_counter, loaded["batch_id"])
            print(
                f"[ZKRollup] Snapshot rechargé depuis {SNAPSHOT_PATH} — "
                f"batch_id={loaded['batch_id']}, root=0x{self.state.hash()}"
            )
        else:
            self.state = State(initial_balances=initial_balances)
            print("[ZKRollup] Aucun snapshot — démarrage avec les balances initiales.")

        self.sequencer = Sequencer(duration=self.duration)
        self.executor = Executor()
        self.prover = Prover(self.state, mode=mode)
        self.da = DataAvailability(epoch=epoch)

        # Vérifie que le state local correspond au state on-chain. Si non,
        # on prévient bruyamment plutôt que d'envoyer des batches qui seront
        # tous rejetés en `stale stateRoot`.
        self._check_state_against_chain()

        print(f"[ZKRollup] Instance initialisée avec une durée de batch de {self.duration} secondes")

    # ------------------------------------------------------------------
    #  Snapshot disque (résume après un arrêt/relance)
    # ------------------------------------------------------------------
    def _load_snapshot(self) -> dict:
        # 1. Snapshot rapide écrit après chaque commit.
        if SNAPSHOT_PATH.exists():
            try:
                return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[ZKRollup] Snapshot illisible ({exc}) — fallback DA.")

        # 2. Fallback : reconstitue depuis le dernier batch_*.json publié par DA.
        #    Indispensable pour récupérer après un arrêt si aucun snapshot
        #    n'a encore été écrit (premiers runs).
        return self._load_from_da_files()

    def _load_from_da_files(self) -> dict:
        batch_dir = Path("batch")
        if not batch_dir.exists():
            return None
        files = list(batch_dir.glob("batch_*.json"))
        if not files:
            return None
        try:
            latest = max(files, key=lambda p: int(p.stem.split("_")[1]))
            data = json.loads(latest.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[ZKRollup] Lecture {batch_dir} échouée : {exc}")
            return None
        return {
            "batch_id": int(data["batch_number"]),
            "state_root": data["state_root_after"],
            # state_after : dict {"address": balance, ...}
            "balances": data["state_after"],
        }

    def _save_snapshot(self, batch_id: int) -> None:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "batch_id": batch_id,
            "state_root": self.state.hash(),
            "balances": self.state.to_dict(),
            "saved_at": time.time(),
        }
        tmp = SNAPSHOT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, SNAPSHOT_PATH)

    def _check_state_against_chain(self) -> None:
        """
        Compare self.state.hash() au stateRoot du contrat. Si divergence,
        log un WARNING explicite — l'utilisateur peut alors choisir de
        repartir d'un snapshot ou de réinitialiser.
        """
        if not self.prover.can_submit_on_chain() or self.prover.rollup is None:
            return
        try:
            on_chain_root = self.prover.rollup.functions.stateRoot().call()
            local_root = bytes.fromhex(self.state.hash())
            if on_chain_root != local_root:
                print("=" * 70)
                print("[ZKRollup] ⚠ DIVERGENCE STATE LOCAL vs ON-CHAIN")
                print(f"  local    : 0x{local_root.hex()}")
                print(f"  on-chain : 0x{on_chain_root.hex()}")
                print("  Les prochains submitBatch seront rejetés (stale stateRoot).")
                print("  → soit supprimer storage/last_committed.json + redéployer un Rollup,")
                print("  → soit restaurer un snapshot cohérent.")
                print("=" * 70)
            else:
                print(f"[ZKRollup] state local = state on-chain (root=0x{local_root.hex()})")
        except Exception as exc:
            print(f"[ZKRollup] Lecture stateRoot on-chain impossible : {exc}")

    # ------------------------------------------------------------------
    #  Boucle de batching
    # ------------------------------------------------------------------
    def start_batch_loop(self):
        if not self.running:
            self.running = True
            self.batch_thread = threading.Thread(target=self._run_batch_loop, daemon=True)
            self.batch_thread.start()
            print("[ZKRollup] Boucle de batching démarrée")

    def _run_batch_loop(self):
        def run_once():
            if not self.running:
                return
            thread = threading.Thread(target=self.compute_batch)
            thread.start()
            threading.Timer(self.duration, run_once).start()

        run_once()

    def stop(self):
        self.running = False
        if self.batch_thread:
            self.batch_thread.join()
            print("[ZKRollup] Boucle de batching arrêtée")
        print("[ZKRollup] ZKRollup arrêté.")

    def compute_batch(self):
        # Empêche le chevauchement de deux compute_batch (sinon : double-spend
        # côté state local, et `stale stateRoot` côté contrat).
        if not self._compute_lock.acquire(blocking=False):
            print("[ZKRollup] compute_batch précédent toujours en cours — skip")
            return
        try:
            self._compute_batch_locked()
        finally:
            self._compute_lock.release()

    def _compute_batch_locked(self):
        batch = self.sequencer.get_batch()
        if batch is None:
            print("[ZKRollup] Erreur lors de la récupération du Batch")
            return

        if len(batch.transactions) == 0:
            print("[ZKRollup] Pas de transactions dans le batch")
            return

        print(f"[ZKRollup] Batch récupéré : {batch}")

        new_state = self.executor.compute_batch(batch, self.state.copy())
        if not new_state:
            print("[ZKRollup] Erreur lors du traitement du batch")
            return
        print(f"[ZKRollup] State après exécution du batch : {new_state}")

        # 1. Génération de la preuve Groth16 (off-chain)
        start = time.time()
        proof_ok = self.prover.prove_batch(batch, new_state)
        print(f"[ZKRollup] prove_batch en {time.time() - start:.2f} s.")
        if not proof_ok:
            print("[ZKRollup] Preuve invalide — état non mis à jour")
            return

        # 2. Construction du payload blob en mémoire (pas d'écriture disque
        #    tant que la soumission on-chain n'a pas réussi, sinon un batch
        #    rejeté laisserait un fichier orphelin et fausserait la reprise).
        blob_payload = self.da.build_blob_payload(batch)

        # 3. Soumission on-chain (blob tx EIP-4844 + Rollup.submitBatch).
        #    Si la chaîne n'est pas configurée, on commit le state localement.
        if self.prover.can_submit_on_chain():
            ok = self.prover.submit_on_chain(
                batch=batch,
                state_root_before=bytes.fromhex(self.state.hash()),
                state_root_after=bytes.fromhex(new_state.hash()),
                blob_payload=blob_payload,
            )
            if not ok:
                print("[ZKRollup] Soumission on-chain refusée — state non mis à jour")
                return
        else:
            print("[ZKRollup] Mode off-chain (pas de Rollup déployé) — state mis à jour localement")

        # 4. Persistance disque APRÈS confirmation : DA JSON + blob, puis snapshot
        self.da.publish_batch_data(self.state, batch, new_state)
        self.da.write_blob_locally(batch, blob_payload)

        # 5. Commit du nouvel état + snapshot persistant pour resume
        self.state = new_state
        self.prover.state = new_state
        self._save_snapshot(batch_id=batch.id)
