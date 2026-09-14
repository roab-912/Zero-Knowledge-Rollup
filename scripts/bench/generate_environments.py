import os
import argparse
import time
import json
import shutil
import numpy as np
import matplotlib.pyplot as plt
from scipy.interpolate import make_interp_spline
from typing import List, Dict
from pathlib import Path

# Gabarit de circuit : scripts/circuit_template/ (independant du CWD).
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "circuit_template"

def run_command(command: str, cwd: str) -> bool:
    """
    Exécute une commande shell dans un dossier donné via os.system.
    """
    original_cwd = os.getcwd()
    try:
        print(f"→ {command}")
        print(f"→ cwd: {cwd}")

        os.chdir(cwd)
        exit_code = os.system(command)

        if exit_code != 0:
            print(f"[ERREUR] Code de retour : {exit_code}")
            return False

        print("[OK]")
        return True

    finally:
        os.chdir(original_cwd)

def run_command_sequence(commands: List[str], cwd: str) -> None:
    start_time = time.time()

    for idx, command in enumerate(commands, start=1):
        print(f"  → Commande {idx}/{len(commands)}")
        success = run_command(command, cwd)

        if not success:
            print("  ⛔ Arrêt de la séquence pour ce dossier")
            break

    end_time = time.time()
    elapsed = end_time - start_time
    print(f"Temps écoulé pour {cwd}: {elapsed:.2f} s")
    return elapsed

def change_value(commands: List[str], value: str) -> List[str]:
    new_commands = [cmd.replace("14", value) for cmd in commands]
    return new_commands

def copy_file(src_path: str, dest_path: str) -> None:
    try:
        shutil.copy2(src_path, dest_path)
        print(f"Fichier copié de '{src_path}' vers '{dest_path}'")
    except Exception as e:
        print(f"Erreur lors de la copie : {e}")

def copy_directory(src_path: str, dest_path: str) -> None:
    try:
        shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
        print(f"Dossier copié de '{src_path}' vers '{dest_path}'")
    except Exception as e:
        print(f"Erreur lors de la copie du dossier : {e}")

def generate_input_file(
    N: int,
    output_dir: str
) -> str:

    if N <= 0:
        raise ValueError("N doit être un entier strictement positif")

    os.makedirs(output_dir, exist_ok=True)

    data: Dict[str, List] = {
        "src": [str(i) for i in range(N)],
        "srcBalance": [1_000_000.0] * N,
        "srcBalanceAfter": [999_999.0] * N,
        "dest": [str(i) for i in range(N, 2 * N)],
        "destBalance": [1_000_000.0] * N,
        "destBalanceAfter": [1_000_001.0] * N,
        "amount": [1.0] * N
    }

    file_path = os.path.join(output_dir, "input.json")

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

    return os.path.abspath(file_path)

def change_tag_circuit(file_path: str, value: str) -> None:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    content = content.replace('XXX', value)
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def experiment_loop(base_path: str, max_exponent: int, commands: List[str]) -> None:
    os.makedirs(base_path, exist_ok=True)

    times = []
    sizes = []
    initial_commands = commands

    result_file = os.path.join(base_path, "execution_times.txt")
    with open(result_file, "w") as f:
        f.write("Taille\tTemps(s)\n")

        for i in range(max_exponent + 1):
            value = 2 ** i
            dir_path = os.path.join(base_path, str(value))
            os.makedirs(dir_path, exist_ok=True)

            match i:
                case 0:
                    commands = change_value(initial_commands, "8") 
                case 1:
                    commands = change_value(initial_commands, "9") 
                case 2:
                    commands = change_value(initial_commands, "10")
                case 3:
                    commands = change_value(initial_commands, "11") 
                case 4:
                    commands = change_value(initial_commands, "12") 
                case 5:
                    commands = change_value(initial_commands, "13") 
                case 6:
                    commands = change_value(initial_commands, "14") 
                case 7:
                    commands = change_value(initial_commands, "15") 
                case 8:
                    commands = change_value(initial_commands, "16") 
                case 9:
                    commands = change_value(initial_commands, "17") 
                case 10:
                    commands = change_value(initial_commands, "18") 
                case 11:
                    commands = change_value(initial_commands, "19") 
                case 12:
                    commands = change_value(initial_commands, "20") 
                case 13:
                    commands = change_value(initial_commands, "21") 
                case 14:
                    commands = change_value(initial_commands, "22") 
                case 15:
                    commands = change_value(initial_commands, "23")       

            print(f"\n=== Dossier 2^{i} ({value}) ===")
            copy_file(str(TEMPLATE_DIR / 'circuit.circom'), os.path.join(dir_path, "circuit.circom"))
            copy_directory(str(TEMPLATE_DIR / 'circomlib'), os.path.join(dir_path, "circomlib"))
            change_tag_circuit(os.path.join(dir_path, "circuit.circom"), str(value))
            generate_input_file(value, dir_path)
            elapsed = run_command_sequence(commands, dir_path)
            times.append(elapsed)
            sizes.append(i)

            f.write(f"{value}\t{elapsed:.4f}\n")

    x = np.array(sizes)
    y = np.array(times)

    x_smooth = np.linspace(x.min(), x.max(), 500)
    spline = make_interp_spline(x, y, k=3)
    y_smooth = spline(x_smooth)

    plt.figure(figsize=(8, 5))
    plt.plot(x_smooth, y_smooth, label="Temps d'exécution lissé")
    plt.scatter(x, y, color='red', label="Mesures brutes")
    plt.xlabel('Taille (2^i)')
    plt.ylabel('Temps d\'exécution (s)')
    plt.title('Temps d\'exécution en fonction de la taille')
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(base_path, 'execution_times.png'))
    plt.show()

def main():
    parser = argparse.ArgumentParser(
        description="Exécution d'une suite de commandes dans des dossiers 2^X (os.system)."
    )

    parser.add_argument("directory", type=str, help="Dossier racine")
    parser.add_argument("X", type=int, help="Exposant maximal X")

    args = parser.parse_args()

    print("Dossier:", args.directory)
    print("Exposant maximal:", args.X)

    command_sequence = [
        "snarkjs powersoftau new bn128 14 pot14_0000.ptau -v",
        (
            'snarkjs powersoftau contribute pot14_0000.ptau pot14_0001.ptau '
            '--name="First contribution" '
            '-v -e="some random text"'
        ),
        (
            'snarkjs powersoftau contribute pot14_0001.ptau pot14_0002.ptau '
            '--name="Second contribution" '
            '-v -e="some random text"'
        ),
        (
            'snarkjs powersoftau contribute pot14_0001.ptau pot14_0002.ptau '
            '--name="Second contribution" '
            '-v -e="some random text"'
        ),
        "snarkjs powersoftau export challenge pot14_0002.ptau challenge_0003",
        (
            'snarkjs powersoftau challenge contribute bn128 challenge_0003 response_0003'
            ' -e="some random text"'
        ),
        (
            'snarkjs powersoftau import response pot14_0002.ptau response_0003 pot14_0003.ptau ' 
            '-n="Third contribution name"'
        ),
        "snarkjs powersoftau verify pot14_0003.ptau",
        (
            'snarkjs powersoftau beacon pot14_0003.ptau pot14_beacon.ptau ' 
            '0102030405060708090a0b0c0d0e0f101112131015161718191a1b1c1d1e1f 10 -n="Final Beacon"'
        ),
        "snarkjs powersoftau prepare phase2 pot14_beacon.ptau pot14_final.ptau -v",
        "snarkjs powersoftau verify pot14_final.ptau",
        "circom --r1cs --wasm --c --sym --inspect circuit.circom",
        "snarkjs r1cs info circuit.r1cs",
        "snarkjs r1cs print circuit.r1cs circuit.sym",
        "snarkjs r1cs export json circuit.r1cs circuit.r1cs.json",
        "snarkjs wtns calculate circuit_js/circuit.wasm input.json witness.wtns",
        "snarkjs groth16 setup circuit.r1cs pot14_final.ptau circuit_0000.zkey",
        (
            'snarkjs zkey contribute circuit_0000.zkey circuit_0001.zkey ' 
            '--name="1st Contributor Name" -v -e="some random text"'
        ),
        (
            'snarkjs zkey contribute circuit_0001.zkey circuit_0002.zkey ' 
            '--name="Second contribution Name" -v -e="Another random entropy"'
        ),
        "snarkjs zkey export bellman circuit_0002.zkey  challenge_phase2_0003",
        (
            'snarkjs zkey bellman contribute bn128 challenge_phase2_0003 response_phase2_0003 -e="some random text"'
        ),
        (
            'snarkjs zkey import bellman circuit_0002.zkey response_phase2_0003 circuit_0003.zkey -n="Third contribution name"'
        ),
        "snarkjs zkey verify circuit.r1cs pot14_final.ptau circuit_0003.zkey",
        (
            'snarkjs zkey beacon circuit_0003.zkey circuit_final.zkey 0102030405060708090a0b0c0d0e0f101112131515161718191a1b1c1d1e1f 10 -n="Final Beacon phase2"'
        ),
        "snarkjs zkey verify circuit.r1cs pot14_final.ptau circuit_final.zkey",
        "snarkjs zkey export verificationkey circuit_final.zkey verification_key.json",
        "snarkjs groth16 prove circuit_final.zkey witness.wtns proof.json public.json",
        "snarkjs groth16 verify verification_key.json public.json proof.json",
        "snarkjs zkey export solidityverifier circuit_final.zkey verifier.sol"
        # "cmd /c",
    ]

    experiment_loop(args.directory, args.X, command_sequence)

if __name__ == "__main__":
    main()