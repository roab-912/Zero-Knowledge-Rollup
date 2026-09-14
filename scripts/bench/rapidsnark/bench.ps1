<#
.SYNOPSIS
    Lance measure_zk_resources.py dans le conteneur de bench (Windows + Docker Desktop).

.DESCRIPTION
    Construit l'image si besoin, démarre le conteneur persistant `zk_bench`, puis
    exécute le script à l'intérieur. Tous les arguments passés à ce wrapper sont
    transmis tels quels au script Python.

    Le script tourne dans le conteneur — et pas sur l'hôte Windows — parce que sa
    mesure de CPU, RAM et I/O s'appuie sur /proc.

.EXAMPLE
    .\scripts\rapidsnark\bench.ps1 --sizes 1,2,4,8 --skip-setup --repeat 5 --prover both

.EXAMPLE
    # cycle complet, setup compris, sur le système de fichiers Linux du conteneur
    .\scripts\rapidsnark\bench.ps1 --sizes 1,2,4 --work-dir /bench/circuits --repeat 5 --prover both
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = 'Stop'
$compose = Join-Path $PSScriptRoot 'docker-compose.yml'

docker compose -f $compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $ScriptArgs -or $ScriptArgs.Count -eq 0) {
    # défaut : les circuits déjà présents dans le dépôt, snarkjs contre rapidsnark
    $ScriptArgs = @(
        '--sizes', '1,2,4,8',
        '--skip-setup',
        '--circuits-dir', 'circuits',
        '--repeat', '5',
        '--prover', 'both',
        '--require-rapidsnark'
    )
    Write-Host "Aucun argument fourni, exécution par défaut :`n  $($ScriptArgs -join ' ')`n"
}

docker compose -f $compose exec -T bench python3 scripts/bench/measure_zk_resources.py @ScriptArgs
exit $LASTEXITCODE
