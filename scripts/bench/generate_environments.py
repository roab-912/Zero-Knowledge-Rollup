import os
import argparse
import time
import json
import shutil
import shlex
import sys
import uuid
from typing import List, Dict
from pathlib import Path

# Gabarit de circuit : scripts/circuit_template/ (independant du CWD).
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "circuit_template"
REQUIRED_ENVIRONMENT = ("circuit_js/circuit.wasm", "circuit_final.zkey", "verification_key.json")


def missing_environment_sizes(base_path, sizes):
    return [n for n in sizes if not all(
        (Path(base_path) / str(n) / name).is_file() and
        (Path(base_path) / str(n) / name).stat().st_size > 0
        for name in REQUIRED_ENVIRONMENT)]


def generate_missing_environments(base_path, sizes, work_dir, snarkjs, circom,
                                  run_step, on_progress, template_dir=TEMPLATE_DIR):
    """Generate only absent/incomplete environments, instrumented by the caller.

    Reuse the repository's setup sequence without invoking the legacy plotting
    loop. Build in isolation, verify the key, then publish a coherent environment.
    Existing incomplete directories are archived; complete environments are never
    regenerated. Setup steps and their logs remain outside batch measurements.
    """
    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from scripts.bench import measure_zk_resources as resources

    base_path, work_dir = Path(base_path).resolve(), Path(work_dir).resolve()
    missing = missing_environment_sizes(base_path, sizes)
    if not missing:
        return []
    if not snarkjs or not circom:
        raise RuntimeError("missing setup executable: snarkjs and circom are required")
    generated = []
    base_path.mkdir(parents=True, exist_ok=True)
    for n in missing:
        if n < 1 or n > 8192 or n & (n - 1):
            raise ValueError("setup sizes must be powers of two from 1 to 8192")
        attempt = uuid.uuid4().hex
        scratch = work_dir / (f"generation-{n}-" + attempt)
        target = base_path / str(n)
        # Preserve an existing circuit source (including local circuit changes).
        template = target if (target / "circuit.circom").is_file() else Path(template_dir)
        stage = Path(resources.prepare_circuit_dir(n, str(scratch), str(template)))
        commands = resources.env_commands(n, resources.ptau_power(n, 8),
                                          "__SNARKJS__", "__CIRCOM__", "some random text")
        for command in commands:
            argv = shlex.split(command.cmd)
            prefix = snarkjs if argv[0] == "__SNARKJS__" else circom
            argv = list(prefix) + argv[1:]
            metadata = {"n": n, "attempt": attempt, "step": command.step,
                        "group": command.group, "directory": str(stage)}
            on_progress({**metadata, "status": "running", "command": argv})
            result = run_step(argv, stage, command.step)
            on_progress({**metadata, **result})
            if result["status"] != "success":
                raise RuntimeError(f"setup failed for N={n}, step={command.step}: "
                                   f"{result.get('message')}; see {result.get('log')}")
        if missing_environment_sizes(scratch, [n]):
            raise RuntimeError(f"setup finished without all required artifacts for N={n}")

        # Copy only reusable artifacts; huge intermediate ceremonies stay in setup/.
        published = base_path / (f".publish-{n}-" + attempt)
        published.mkdir()
        for name in ("circuit_js", "circomlib"):
            if (stage / name).is_dir():
                shutil.copytree(stage / name, published / name)
        for name in ("circuit.circom", "circuit.r1cs", "circuit.r1cs.json", "circuit.sym",
                     "circuit_final.zkey", "verification_key.json", "verifier.sol", "input.json"):
            if (stage / name).is_file():
                shutil.copy2(stage / name, published / name)
        backup = None
        if target.exists():
            backup = base_path / (f".before-setup-{n}-" + attempt)
            target.rename(backup)
        try:
            published.rename(target)
        except OSError:
            if backup and not target.exists():
                backup.rename(target)
            raise
        generated.append(n)
        on_progress({"n": n, "attempt": attempt, "step": "publish", "status": "success",
                     "directory": str(target), "previous_environment": str(backup) if backup else None})
    return generated

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
    import numpy as np
    import matplotlib.pyplot as plt
    from scipy.interpolate import make_interp_spline
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
