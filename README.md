# A Zero-Knowledge Rollup for Monetary Transactions

> Reference implementation accompanying a CIFRE doctoral thesis (2023–2025) on
> the use of succinct cryptographic proofs to scale on-chain monetary
> settlement systems. The artefact instantiates a Groth16-based zk-rollup
> targeting Ethereum, with EIP-4844 blobs as the data-availability layer.

---

## 1. Abstract

This repository contains an end-to-end implementation of a *validity rollup*
in which a sequence of off-chain monetary transactions is compressed into a
single zk-SNARK proof, posted to a Layer-1 verifier contract together with a
reference to the underlying transaction data published as an EIP-4844 blob.
The rollup is parameterised by the batch cardinality *N* (a power of two), and
a separate arithmetic circuit and trusted-setup ceremony are produced for each
*N ∈ {1, 2, 4, 8, 16, 32, 64, …, 8192}*. The artefact is intended as a
controlled testbed for measuring (i) the proof-generation cost as a function of
batch size, (ii) the on-chain verification cost on the Ethereum Sepolia
testnet, and (iii) the data-availability cost of EIP-4844 blob transactions,
under realistic load conditions reaching 8192 transactions per batch.

The implementation favours methodological transparency over engineering
sophistication: each component (sequencer, executor, prover, data-availability
publisher, L1 settlement client) is deliberately small and self-contained, so
that each measurement can be attributed to a well-identified subsystem.

---

## 2. System model

The system follows the classical four-actor decomposition of a validity
rollup. Let `S_t ∈ Σ` denote the rollup state at logical time *t* (a mapping
from accounts to balances), and let *T_t* denote a finite ordered set of user
transactions submitted between *t* and *t+1*.

```
        ┌────────────┐  POST /add_transaction   ┌─────────────────┐
   user ─▶│ Sequencer  │ ───────────────────────▶│ persistent pool │
        └────────────┘                          │   (SQLite/WAL)  │
                                                 └─────────────────┘
                                                         │
                                       get_batch (N max) ▼
                                                 ┌─────────────────┐
                                                 │  Batch  (size N)│
                                                 └─────────────────┘
                                                         │
                                                         ▼
                                                 ┌─────────────────┐
   off-chain                                     │   Executor      │ S_t  → S_{t+1}
                                                 └─────────────────┘
                                                         │
                                                         ▼
                                                 ┌─────────────────┐
                                                 │   Prover (GR16) │ π,  σ_pub
                                                 └─────────────────┘
                                                         │
                                          build blob,    ▼
                                          KZG commit  ┌──────────────────┐
                                          versioned   │ DataAvailability │
                                          hash h_v    │ (local + EIP-4844)│
                                                       └──────────────────┘
                                                         │
                                                         ▼
   on-chain (Sepolia)                            ┌────────────────────────┐
                                                 │  Rollup.submitBatch    │
                                                 │  blobhash(0) == h_v    │
                                                 │  Verifier_N(π, σ_pub)  │
                                                 │  S_root ← S_root_after │
                                                 └────────────────────────┘
```

State transitions are settled on Ethereum via a single type-3 (EIP-4844)
transaction that simultaneously (i) carries the encoded transaction set as a
single 128 kiB blob and (ii) calls the rollup verifier contract. This binding
ensures that the data-availability commitment and the validity proof refer to
the same logical batch, since the contract enforces
`blobhash(0) = blobVersionedHash` inside the verification function.

---

## 3. Cryptographic constructions

### 3.1 Arithmetic circuits

The transition function for a batch of *N* transfers is encoded as a Circom
circuit
*C_N : F_r^{m(N)} → F_r* over the BN254 scalar field. For each transaction *i*,
the circuit verifies:

* `srcBalance[i] - amount[i] = srcBalanceAfter[i]`
* `destBalance[i] + amount[i] = destBalanceAfter[i]`
* non-negativity of `srcBalanceAfter[i]`,

and outputs a single public signal *σ_pub ∈ {0,1}* certifying the conjunction
of the *N* transition constraints. The artefact ships the witnesses, R1CS
descriptions, and proving/verification keys for *N ∈ {1, 2, 4, 8, 16, 32, 64}*.
Larger sizes can be regenerated via `scripts/generate_environments.py`, which
performs the full Powers-of-Tau and Phase-2 ceremonies and emits the Solidity
verifier through `snarkjs zkey export solidityverifier`.

### 3.2 Proof system

Proofs are produced using **Groth16** in two interchangeable backends:

* `snarkjs` (pure JavaScript) — used as the reference for correctness;
* `rapidsnark` (C++ via Docker) — used for performance benchmarking.

The mode is selected at construction time (`Prover(state, mode="snarkjs"|"rapidsnark")`).
For each batch the prover materialises an `input.json`, runs witness generation
and proof generation, and stores `proof.json`, `public.json`, and a copy of
`proof.json` archived under `proofs/` for *post hoc* audit.

### 3.3 Data availability

A batch is encoded as a length-prefixed binary record (4 B `batch_id`,
4 B `tx_count`, then per-transaction `(length, JSON)` pairs), zlib-compressed,
and packed into a single 4096-element BLS12-381 field-element blob using the
standard 31-byte-per-element encoding (one zero byte prefix per slot to ensure
the canonical inequality `value < r`). The KZG commitment, opening proof, and
versioned hash are computed via the reference `c-kzg-4844` library
(`pip install ckzg`). The blob, calldata, and versioned hash are then
co-located in a single EIP-4844 type-3 transaction.

---

## 4. Settlement layer

### 4.1 Verifier contracts

For each batch size *N*, the snarkjs-generated `Groth16Verifier` is deployed
to Sepolia. All circuits in the present artefact expose a single public
signal, hence the verifier ABI uniformly exhibits the signature
`verifyProof(uint[2], uint[2][2], uint[2], uint[1])`.

### 4.2 Rollup contract

The orchestrating contract (`smart_contract/Rollup.sol`, Solidity 0.8.24,
target EVM Cancun) maintains:

* `bytes32 stateRoot` — the canonical commitment to the L2 account balances,
  initialised at deployment to `SHA-256( JSON(initial_balances) )`;
* `mapping(uint256 ⇒ address) verifiers` — a registry from batch size to
  Groth16 verifier address;
* `uint256 batchCount` — a monotonic batch counter.

The settlement entry point is

```solidity
function submitBatch(
    uint256 batchSize,
    uint[2]   calldata a,
    uint[2][2] calldata b,
    uint[2]   calldata c,
    uint[1]   calldata input,
    bytes32   stateRootBefore,
    bytes32   stateRootAfter,
    bytes32   blobVersionedHash
) external;
```

which (i) asserts liveness against replay via `stateRootBefore == stateRoot`,
(ii) asserts data availability via `blobhash(0) == blobVersionedHash`,
(iii) delegates verification to `verifiers[batchSize]`, (iv) updates the state
root, and (v) emits `BatchSubmitted` for off-chain indexing.

This contract is intentionally minimal: it implements neither deposits nor
withdrawals, and it does not enforce any policy on the *contents* of the blob.
The single state-transition predicate is the validity of the SNARK with
respect to the public signals; the blob commitment ensures that the input to
that predicate is publicly retrievable.

---

## 5. Repository layout

```
zk_rollup/
├── classes/
│   ├── ZKRollup.py            orchestrator (batching loop, snapshot, recovery)
│   ├── Sequencer.py           FastAPI ingestion + power-of-two batch selection
│   ├── TxPool.py              SQLite/WAL persistent transaction pool
│   ├── Executor.py            state transition function (off-chain replay)
│   ├── Prover.py              Groth16 driver + on-chain submission
│   ├── DataAvailability.py    JSON archives + binary blob payload
│   └── L1Client.py            EIP-4844 type-3 transaction builder
├── models/                    Transaction, Batch, State, request DTOs
├── circuits/<N>/              Per-size Circom artefacts (r1cs, zkey, vk, …)
├── smart_contract/
│   ├── Rollup.sol             settlement contract (Cancun)
│   └── abi/Rollup.json        ABI emitted by the deployment script
├── scripts/
│   ├── deploy_contracts.py    automated deployment to Sepolia
│   ├── inspect_rollup.py      on-chain state inspection / event tail
│   ├── generate_environments.py   per-size circuit ceremony driver
│   ├── send_transaction.py    asynchronous load generator
│   └── generate_graph_from_bench_out.py   benchmark plotting
├── batch/                     DA archives (one JSON per committed batch)
├── blob/                      compressed blob payloads (debug / fallback)
├── proofs/                    archived proofs per batch
├── storage/                   txpool.db (SQLite) + last_committed.json
├── bench-out/                 raw measurements
├── main.py                    runtime entry point
└── requirements.txt
```

---

## 6. Reproducibility

### 6.1 Software prerequisites

* Python ≥ 3.11 (tested with 3.14)
* Node.js ≥ 18 with `snarkjs ≥ 0.7`, `circom ≥ 2.1`
* solc 0.8.24 (auto-installed by `py-solc-x`)
* `c-kzg-4844` Ethereum trusted setup (≈ 410 kB, public)
* Optional: Docker, with the `rapidsnark` image, for the C++ prover backend

Python dependencies are pinned in `requirements.txt`:

```
pip install -r requirements.txt
```

### 6.2 Environment configuration

The runtime expects the following entries in `.env`:

| Key                   | Description                                                 |
| --------------------- | ----------------------------------------------------------- |
| `INITIAL_BALANCE`     | Per-account initial balance used to seed `S_0`.             |
| `NB_INITIAL_WALLET`   | Number of accounts in `S_0`.                                |
| `EPOCH`               | Number of batches per DA epoch (controls archival rotation).|
| `WS_ADDRESS`          | Sepolia JSON-RPC endpoint (HTTPS or WSS).                   |
| `WALLET_ADDRESS`      | Deployer / sequencer L1 address.                            |
| `WALLET_PRIVATE_KEY`  | Private key controlling `WALLET_ADDRESS`.                   |
| `TRUSTED_SETUP_PATH`  | Path to the c-kzg-4844 trusted setup (`trusted_setup.txt`). |
| `CIRCUIT_<N>_ADDRESS` | Verifier contract address for batch size *N*.               |
| `ROLLUP_ADDRESS`      | Rollup contract address (settlement entry point).           |

The trusted setup may be obtained from:

```
https://raw.githubusercontent.com/ethereum/c-kzg-4844/main/src/trusted_setup.txt
```

### 6.3 Deployment

A single command compiles every `circuits/<N>/verifier.sol` with solc 0.8.24
under EVM target Cancun, deploys each verifier to Sepolia, deploys the
`Rollup` contract with `S_root_0 = SHA-256(JSON(S_0))`, registers each verifier
through `setVerifier(N, addr)`, and writes all addresses back into `.env`:

```
python scripts/deploy_contracts.py
```

Verifiers already known in `.env` can be skipped with `--skip-existing`,
which is convenient for redeploying solely the Rollup after an experimental
restart.

### 6.4 Operation

```
python main.py                       # launches the rollup loop
python scripts/send_transaction.py   # asynchronous load generator
python scripts/inspect_rollup.py     # on-chain state and event tail
```

Recovery semantics: at boot, `ZKRollup` reloads its state from
`storage/last_committed.json` (written atomically after each successful
on-chain commit). If the snapshot is absent, the latest `batch/batch_*.json`
acts as a fallback. Once initialised, the runtime reads `Rollup.stateRoot()`
on Sepolia and emits a divergence warning if the on-chain root does not match
the local one — a precondition for the next `submitBatch` to succeed.

---

## 7. Experimental design

The artefact is instrumented to support three families of measurements.

### 7.1 Off-chain proof cost

For each *N*, the proving time *T_p(N)* and the size of the resulting witness
are reported by the prover. `scripts/generate_graph_from_bench_out.py` plots
*T_p(N)* under both `snarkjs` and `rapidsnark` backends. Linear behaviour is
expected up to constraints of the Powers-of-Tau ceiling.

### 7.2 On-chain verification cost

Each `Rollup.submitBatch` transaction emits `BatchSubmitted`; gas usage is
reported in the transaction receipt and indexed by Etherscan/Blobscan. Because
all circuits expose a single public input, the per-batch verification gas is
quasi-constant in *N*, providing an interesting baseline against alternative
proving systems (e.g. PLONK, Halo2, STARK-based rollups).

### 7.3 Data-availability cost

Blob gas (`maxFeePerBlobGas`) and effective blob gas price are extracted from
the receipt. Since at most one blob is attached per submission, payload
compression governs the maximum *N* representable in a single blob; this
artefact zlib-compresses a JSON serialisation, which is intentionally
sub-optimal but auditable.

---

## 8. Limitations and threat model

The artefact is a research prototype and **must not** be deployed to mainnet
under its present form. The following limitations are acknowledged
explicitly, in increasing order of severity:

1. *Authentication.* User transactions are accepted by the sequencer over an
   unauthenticated REST endpoint. No signature scheme is enforced at the
   protocol boundary; integrating EdDSA over the BabyJubJub curve at the
   circuit level (already vendored in `circomlib`) is straightforward and
   foreseen as future work.
2. *Single-blob bound.* A single 128 kiB blob caps the JSON-encoded payload at
   ≈ 127 kB. With JSON serialisation this bounds *N* below 8192 in practice.
   A binary serialisation, or a multi-blob extension paired with a
   `blobhash(i)` loop in the contract, lifts this restriction.
3. *Centralised sequencer.* Ordering is controlled by a single FastAPI
   process. Decentralising the sequencer (PBS-style or shared-sequencer
   protocols) is out of scope.
4. *Initial trusted setup.* Each circuit requires a separate Powers-of-Tau
   contribution; the script chains contributions but a real ceremony with
   independent participants remains the user's responsibility.
5. *No rollback on settlement failure.* Although the orchestrator now defers
   local persistence until after on-chain confirmation, a sequencer crash
   between L1 confirmation and snapshot write yields a recoverable but
   ambiguous local state. A two-phase commit on the snapshot would close
   this gap.

---

## 9. Citation

If this artefact informs published work, please cite the accompanying
doctoral thesis:

> Barbier, R. (in preparation). *Amélioration des qualités de la blockchain
> au sein d'un réseau dynamique pour l'industrie 4.0* [Improving the
> properties of the blockchain within a dynamic network for Industry 4.0].
> CIFRE doctoral thesis (2024–2027), IMT Atlantique, École Doctorale SPIN,
> Brest, France. Academic supervisor: Françoise Sailhan (Équipe Madness,
> IMT Atlantique). Industrial supervisor: Loïc Machado (CTO, Vistory).

BibTeX:

```bibtex
@phdthesis{barbier_thesis_2027,
  author       = {Barbier, R{\'e}mi},
  title        = {Am{\'e}lioration des qualit{\'e}s de la blockchain au sein
                  d'un r{\'e}seau dynamique pour l'industrie 4.0},
  school       = {IMT Atlantique, {\'E}cole Doctorale SPIN},
  type         = {CIFRE doctoral thesis (in preparation)},
  year         = {2024--2027},
  address      = {Brest, France},
  note         = {Academic supervisor: Fran{\c c}oise Sailhan
                  ({\'E}quipe Madness, IMT Atlantique).
                  Industrial supervisor: Lo{\"i}c Machado (CTO, Vistory).}
}
```

---

## 10. Acknowledgements

This work was conducted under a CIFRE doctoral grant (Convention Industrielle
de Formation par la Recherche), partnering Vistory with the host academic
laboratory. The implementation depends on `circom`, `snarkjs`, `rapidsnark`,
the `c-kzg-4844` reference implementation of EIP-4844 polynomial commitments,
and the `web3.py` and `eth-account` toolchains; their authors are gratefully
acknowledged.
