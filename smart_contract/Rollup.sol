// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Interface Groth16 générée par snarkjs.
/// Tous les circuits du projet exposent exactement 1 signal public (bool de
/// validité), donc la signature est `uint[1]` quelle que soit la taille de batch.
interface IGroth16Verifier {
    function verifyProof(
        uint[2] calldata a,
        uint[2][2] calldata b,
        uint[2] calldata c,
        uint[1] calldata input
    ) external view returns (bool);
}

/// @title Rollup minimal — vérification Groth16 + state root
/// @notice Stocke le state_root courant et un mapping (taille de batch ⇒ verifier).
///         Pour chaque batch soumis :
///           - récupère le verifier de la bonne taille,
///           - vérifie la preuve,
///           - vérifie que stateRootBefore == stateRoot courant,
///           - vérifie que blobVersionedHash correspond au blob de la transaction
///             (opcode BLOBHASH, EIP-4844, requiert Solidity ≥ 0.8.24),
///           - met à jour stateRoot ← stateRootAfter et émet BatchSubmitted.
contract Rollup {
    address public owner;
    bytes32 public stateRoot;
    uint256 public batchCount;

    /// @dev taille de batch (1, 2, 4, … 16384) ⇒ adresse du verifier déployé
    mapping(uint256 => address) public verifiers;

    event BatchSubmitted(
        uint256 indexed batchId,
        uint256 indexed batchSize,
        bytes32 stateRootBefore,
        bytes32 stateRootAfter,
        bytes32 blobVersionedHash
    );

    event VerifierRegistered(uint256 indexed batchSize, address verifier);
    event StateRootInitialized(bytes32 stateRoot);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor(bytes32 _initialStateRoot) {
        owner = msg.sender;
        stateRoot = _initialStateRoot;
        emit StateRootInitialized(_initialStateRoot);
    }

    /// @notice Enregistre le verifier Groth16 pour une taille de batch donnée.
    function setVerifier(uint256 batchSize, address verifier) external onlyOwner {
        require(verifier != address(0), "verifier=0");
        require(_isPowerOfTwo(batchSize), "size not pow2");
        verifiers[batchSize] = verifier;
        emit VerifierRegistered(batchSize, verifier);
    }

    /// @notice Soumet un batch et applique la transition d'état si la preuve est valide.
    /// @param batchSize Nombre de transactions du batch (puissance de 2).
    /// @param a Composante A de la preuve Groth16.
    /// @param b Composante B de la preuve Groth16.
    /// @param c Composante C de la preuve Groth16.
    /// @param input Signal public du circuit (uint[1], le bool de validité).
    /// @param stateRootBefore Doit être égal à stateRoot courant.
    /// @param stateRootAfter Nouveau state root.
    /// @param blobVersionedHash Hash versionné EIP-4844 du blob attendu en index 0.
    function submitBatch(
        uint256 batchSize,
        uint[2] calldata a,
        uint[2][2] calldata b,
        uint[2] calldata c,
        uint[1] calldata input,
        bytes32 stateRootBefore,
        bytes32 stateRootAfter,
        bytes32 blobVersionedHash
    ) external {
        address verifier = verifiers[batchSize];
        require(verifier != address(0), "no verifier for size");
        require(stateRootBefore == stateRoot, "stale stateRoot");

        // Vérifie que la transaction porte bien le blob attendu en index 0.
        // blobhash(0) renvoie 0 si aucun blob n'est attaché.
        require(blobhash(0) == blobVersionedHash, "blob hash mismatch");

        bool ok = IGroth16Verifier(verifier).verifyProof(a, b, c, input);
        require(ok, "invalid proof");

        stateRoot = stateRootAfter;
        unchecked {
            batchCount += 1;
        }

        emit BatchSubmitted(
            batchCount,
            batchSize,
            stateRootBefore,
            stateRootAfter,
            blobVersionedHash
        );
    }

    function _isPowerOfTwo(uint256 x) internal pure returns (bool) {
        return x != 0 && (x & (x - 1)) == 0;
    }
}
