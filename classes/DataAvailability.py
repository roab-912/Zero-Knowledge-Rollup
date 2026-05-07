import json
import os
import struct
import zipfile
import zlib

from models.Batch import Batch
from models.State import State

class DataAvailability:

    def __init__(self, batch_dir="batch", blob_dir="blob", epoch=1000):
        """
        Initialise la couche de disponibilité des données :
        - batch_dir : dossier où les fichiers JSON seront écrits.
        """
        self.batch_dir = batch_dir
        self.blob_dir = blob_dir
        self.epoch_number = 1
        self.epoch = epoch
        os.makedirs(self.batch_dir, exist_ok=True)
        os.makedirs(self.blob_dir, exist_ok=True)

    def publish_batch_data(self, previous_state: State, batch: Batch, new_state: State):
        """
        Écrit un fichier JSON contenant :
        - state_root_before
        - transactions (liste)
        - state_root_after
        - hash des transactions
        """
        state_root_before = previous_state.hash()
        state_root_after = new_state.hash()
        batch_hash = batch.hash_batch()
        batch_timestamp = batch.timestamp
        batch_number = batch.id

        data = {
            "batch_number": batch_number,
            "batch_size": len(batch.transactions),            
            "batch_hash": batch_hash,
            "batch_timestamp": batch_timestamp,
            "state_root_before": state_root_before,
            "state_before": previous_state.to_dict(),
            "batch_transactions": [tx.to_dict() for tx in batch.transactions],
            "state_root_after": state_root_after,
            "state_after": new_state.to_dict()
        }

        filename = f"batch_{batch_number}.json"
        filepath = os.path.join(self.batch_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=4)
        print(f"[DataAvailability] Batch {batch_number} écrit dans {filepath}")

        if batch_number % self.epoch == 0:
            self.__compress_epoch_batch__()

    def __compress_epoch_batch__(self):
        """
        Compresse tous les fichiers JSON correspondant à l'epoch courante dans une archive ZIP,
        puis supprime les fichiers JSON originaux pour libérer de l'espace disque.

        L'epoch est définie comme un ensemble de `self.epoch` batchs consécutifs.
        Cette méthode suppose que `self.epoch_number` a été correctement incrémenté
        et correspond à l'epoch que l'on souhaite compresser.

        Exemple :
            - Si epoch = 1000 et epoch_number = 2,
            alors on compresse les fichiers de batch_1001.json à batch_2000.json
            dans epoch_2.zip
        """
        start = ((self.epoch_number - 1) * self.epoch) + 1
        end = self.epoch_number * self.epoch

        zip_filename = f"epoch_{self.epoch_number}.zip"
        zip_filepath = os.path.join(self.batch_dir, zip_filename)
        print(f"[DataAvailability] Compression des fichiers de l'epoch {self.epoch_number} ({start} à {end})...")

        with zipfile.ZipFile(zip_filepath, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
            for i in range(start, end+1):
                filename = f"batch_{i}.json"
                filepath = os.path.join(self.batch_dir, filename)
                if os.path.exists(filepath):
                    zipf.write(filepath, arcname=filename)
                    os.remove(filepath)

        self.epoch_number += 1

    def build_blob_payload(self, batch: Batch) -> bytes:
        """
        Construit la charge utile binaire d'un batch destinée à un blob EIP-4844.

        Format :
        - batch_id (uint32, 4B, big endian)
        - tx_count (uint32, 4B, big endian)
        - Pour chaque transaction :
            - length (uint32, 4B)
            - payload UTF-8 (JSON sérialisé du tx)

        Le tout est ensuite compressé zlib pour maximiser le nombre de tx
        que l'on peut emballer dans un seul blob (126 976 octets utiles max).
        """
        buf = bytearray()
        buf += struct.pack(">I", batch.id)
        buf += struct.pack(">I", len(batch))
        for tx in batch:
            tx_bytes = json.dumps(tx.to_dict(), sort_keys=True).encode("utf-8")
            buf += struct.pack(">I", len(tx_bytes))
            buf += tx_bytes
        return zlib.compress(bytes(buf), level=9)

    def write_blob_locally(self, batch: Batch, payload: bytes) -> str:
        """
        Persiste un payload de blob déjà construit (utilisé après confirmation
        on-chain pour ne pas laisser de blob orphelin si la soumission échoue).
        """
        blob_bin_filename = os.path.join(self.blob_dir, f"blob_{batch.id}.bin")
        with open(blob_bin_filename, "wb") as f:
            f.write(payload)
        print(
            f"[DataAvailability] Blob binaire ({len(payload)} octets compressés) "
            f"écrit pour batch {batch.id} dans {blob_bin_filename}"
        )
        return blob_bin_filename

    def publish_blob_data(self, batch: Batch) -> bytes:
        """
        Construit le payload du blob ET l'écrit sur disque immédiatement.
        Conservé pour rétro-compat : préférer build_blob_payload + write_blob_locally
        si l'on veut différer l'écriture jusqu'à confirmation on-chain.
        """
        payload = self.build_blob_payload(batch)
        self.write_blob_locally(batch, payload)
        return payload
