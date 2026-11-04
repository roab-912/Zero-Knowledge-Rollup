"""Measurement schemas and inventory of this repository's rollup workflow."""

LOCAL_V1 = ("batch_build", "compute_batch", "input_prepare", "state_hash",
            "da_encode", "da_persist", "snapshot_persist")
LOCAL_V2 = ("transaction_build", "txpool_insert", "txpool_select", "batch_build",
            "txpool_assign", "state_copy", "compute_batch", "input_prepare",
            "state_hash_before", "state_hash_after", "da_serialize", "da_compress",
            "da_json_persist", "da_blob_persist", "snapshot_persist")
LOCAL_SCHEMAS = {1: LOCAL_V1, 2: LOCAL_V2}
LOCAL_LABELS = {
    "transaction_build": "Transaction construction + hashes",
    "txpool_insert": "Txpool insertion (SQLite)",
    "txpool_select": "Txpool selection (SQLite)",
    "batch_build": "Batch construction",
    "txpool_assign": "Txpool batch assignment",
    "state_copy": "Global state copy",
    "compute_batch": "New state computation (Executor)",
    "input_prepare": "Circuit input preparation",
    "state_hash_before": "Previous state hash",
    "state_hash_after": "New state hash",
    "da_serialize": "DA serialization",
    "da_compress": "DA compression",
    "da_json_persist": "DA JSON persistence",
    "da_blob_persist": "DA blob persistence",
    "snapshot_persist": "Snapshot persistence",
    # V1 durations remain combined; never silently reinterpret old measurements.
    "state_hash": "State hashes (before + after, v1)",
    "da_encode": "DA serialization + compression (v1)",
    "da_persist": "DA persistence (v1)",
}


def workflow_inventory(measured_stages):
    """State explicitly which workflow costs were actually measured in a run.

    A stage can use several physical resources. This is a workflow inventory,
    not a claim to identify every hardware bottleneck from elapsed times.
    """
    cpu = "CPU;RAM"
    disk = "CPU;RAM;disk"
    specs = [
        ("http_ingress", "HTTP ingestion and request validation", "live", "CPU;RAM;network", "classes/Sequencer.py:setup_routes"),
        ("transaction_build", LOCAL_LABELS["transaction_build"], "local", cpu, "models/Transaction.py:__init__"),
        ("txpool_insert", LOCAL_LABELS["txpool_insert"], "local", disk, "classes/TxPool.py:add"),
        ("batch_wait", "Waiting for batch release / queueing", "policy", "latency;queue occupancy", "classes/ZKRollup.py:_run_batch_loop"),
        ("txpool_select", LOCAL_LABELS["txpool_select"], "local", disk, "classes/TxPool.py:fetch_batch_unique_senders"),
        ("batch_build", LOCAL_LABELS["batch_build"], "local", cpu, "models/Batch.py:__init__"),
        ("txpool_assign", LOCAL_LABELS["txpool_assign"], "local", disk, "classes/TxPool.py:assign_batch_id"),
        ("state_copy", LOCAL_LABELS["state_copy"], "local", cpu, "models/State.py:copy"),
        ("compute_batch", LOCAL_LABELS["compute_batch"], "local", "CPU;RAM;log I/O", "classes/Executor.py:compute_batch"),
        ("input_prepare", LOCAL_LABELS["input_prepare"], "local", disk, "classes/BatchInput.py:write_batch_input"),
        ("witness", "Witness calculation", "zk", disk, "snarkjs wtns calculate"),
        ("proof", "Groth16 proof generation", "zk", disk, "snarkjs groth16 prove / rapidsnark"),
        ("witness_and_proof", "Combined witness + proof (legacy)", "zk", disk, "snarkjs groth16 fullprove"),
        ("proof_archive", "Proof archive copy", "live", disk, "classes/Prover.py:__store_proof__"),
        ("verify", "Local proof verification", "zk", disk, "snarkjs groth16 verify"),
        ("public_result", "Public signal parsing", "live", disk, "classes/Prover.py:__read_public_file__"),
        ("state_hash_before", LOCAL_LABELS["state_hash_before"], "local", cpu, "models/State.py:hash"),
        ("state_hash_after", LOCAL_LABELS["state_hash_after"], "local", cpu, "models/State.py:hash"),
        ("da_serialize", LOCAL_LABELS["da_serialize"], "local", cpu, "classes/DataAvailability.py:serialize_blob_payload"),
        ("da_compress", LOCAL_LABELS["da_compress"], "local", cpu, "classes/DataAvailability.py:compress_blob_payload"),
        ("calldata_export", "Solidity calldata export + ABI encoding", "live", disk, "classes/Prover.py:submit_on_chain"),
        ("blob_padding", "Blob padding and field encoding", "live", cpu, "classes/L1Client.py:encode_payload_to_blob"),
        ("kzg_commitment", "KZG commitment", "live", cpu, "classes/L1Client.py:build_blob_artifacts"),
        ("kzg_proof", "KZG blob proof + versioned hash", "live", cpu, "classes/L1Client.py:build_blob_artifacts"),
        ("rpc_preparation", "Nonce retrieval + gas estimate", "live", "CPU;network;RPC latency", "classes/L1Client.py:send_blob_call"),
        ("l1_signing", "L1 transaction signing + serialization", "live", cpu, "classes/L1Client.py:send_blob_call"),
        ("l1_broadcast", "Transaction + blob broadcast", "live", "CPU;network", "classes/L1Client.py:send_blob_call"),
        ("l1_inclusion", "Receipt / inclusion wait", "latency", "RPC latency;chain inclusion", "classes/L1Client.py:send_blob_call"),
        ("l1_execution", "L1 verification + contract state update", "chain", "EVM gas;blob capacity", "smart_contract/Rollup.sol:submitBatch"),
        ("receipt_events", "Receipt validation + event decoding", "live", cpu, "classes/Prover.py:submit_on_chain"),
        ("da_json_persist", LOCAL_LABELS["da_json_persist"], "local", disk, "classes/DataAvailability.py:publish_batch_data"),
        ("da_blob_persist", LOCAL_LABELS["da_blob_persist"], "local", disk, "classes/DataAvailability.py:write_blob_locally"),
        ("epoch_archive", "Periodic epoch ZIP compression", "periodic", disk, "classes/DataAvailability.py:__compress_epoch_batch__"),
        ("state_commit", "In-memory state commit", "live", cpu, "classes/ZKRollup.py:_compute_batch_locked"),
        ("snapshot_persist", LOCAL_LABELS["snapshot_persist"], "local", disk, "classes/ZKRollup.py:_save_snapshot"),
        ("setup", "One-time circuit / ceremony / keys setup", "setup", disk, "scripts/bench/measure_zk_resources.py:env_commands"),
    ]
    specs.extend((name, LOCAL_LABELS[name], "local_v1", cpu if name == "state_hash" else disk,
                  "scripts/bench/measure_rollup_local.py (v1)")
                 for name in ("state_hash", "da_encode", "da_persist"))
    measured = set(measured_stages)
    return [dict(stage=stage, label=label, collection=collection, resources=resources,
                 source=source, status="measured" if stage in measured else "not_measured")
            for stage, label, collection, resources, source in specs]
