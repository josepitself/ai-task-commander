# Vigila C:\Claude\HomeAssistant\gpt-queue\*.json i executa codex per cada feina.
# Format del JSON: { "model": "gpt-5.5", "effort": "medium", "prompt": "..." }
# Resultat: <nom>.md (resposta), <nom>.err (stderr), <nom>.json.done (feina consumida)
# Ús: .\gpt-watcher.ps1 [-Force]
#   -Force  Fa servir el directori de cua encara que ja existeixi i no estigui buit.
param(
  [switch]$Force
)

$q = 'C:\Claude\HomeAssistant\gpt-queue'

# --- Preparació del directori de cua ---------------------------------------
if (Test-Path -LiteralPath $q -PathType Leaf) {
  Write-Error "La ruta $q existeix però és un fitxer, no un directori."
  exit 1
}

if (-not (Test-Path -LiteralPath $q -PathType Container)) {
  New-Item -ItemType Directory -Path $q | Out-Null
  Write-Host "Creat el directori de cua $q"
}
else {
  $existing = @(Get-ChildItem -LiteralPath $q -Force)
  if ($existing.Count -gt 0) {
    if ($Force) {
      Write-Warning "El directori $q ja existeix i no està buit ($($existing.Count) elements). Es fa servir igualment per -Force."
    }
    else {
      Write-Warning "El directori $q ja existeix i no està buit ($($existing.Count) elements). Buida'l o executa l'script amb -Force."
      exit 1
    }
  }
}

# --- Bucle de vigilància ----------------------------------------------------
Write-Host "Vigilant $q (Ctrl+C per aturar)"
while ($true) {
  Get-ChildItem "$q\*.json" | Sort-Object LastWriteTime | ForEach-Object {
    $job = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    $base = $_.BaseName
    Write-Host "[$(Get-Date -Format HH:mm:ss)] $base -> $($job.model)/$($job.effort)"
    codex exec --skip-git-repo-check --model $job.model --cd 'C:\Claude\HomeAssistant' `
      -o "$q\$base.md" -c "model_reasoning_effort=`"$($job.effort)`"" -c 'model_reasoning_summary="none"' `
      $job.prompt 2> "$q\$base.err" | Out-Null
    Rename-Item $_.FullName "$($_.FullName).done"
  }
  Start-Sleep -Seconds 5
}
