# Vigila C:\Claude\HomeAssistant\gpt-queue\*.json i executa codex per cada feina.
# Format del JSON: { "model": "gpt-5.5", "effort": "medium", "prompt": "..." }
# Resultat: <nom>.md (resposta), <nom>.err (stderr), <nom>.json.done (feina consumida)
# Ús: .\gpt-watcher.ps1 [-Force]
#   -Force  Arrenca encara que hi hagi feines pendents (*.json) a la cua i les processa.
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
  # Només compten les feines pendents (*.json). L'històric (*.json.done, *.md, *.err) es conserva.
  $pending = @(Get-ChildItem -LiteralPath $q -File | Where-Object { $_.Extension -eq '.json' })
  $done    = @(Get-ChildItem -LiteralPath $q -File | Where-Object { $_.Name -like '*.json.done' })
  Write-Host "Cua $q : $($done.Count) feines fetes, $($pending.Count) pendents"
  if ($pending.Count -gt 0) {
    if ($Force) {
      Write-Warning "Hi ha $($pending.Count) feines pendents ($($pending.Name -join ', ')). S'executaran ara per -Force."
    }
    else {
      Write-Warning "Hi ha $($pending.Count) feines pendents ($($pending.Name -join ', ')). Revisa-les i executa l'script amb -Force per processar-les, o esborra-les."
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
