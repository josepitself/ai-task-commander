# Vigila C:\Claude\HomeAssistant\claude-queue\*.json i executa claude (Claude Code) per cada feina.
# Format del JSON: { "model": "claude-sonnet-5", "effort": "medium", "prompt": "..." }
# Resultat: <nom>.md (resposta), <nom>.err (stderr), <nom>.json.done (feina consumida)
# Ús: .\claude-watcher.ps1 [-Force]
#   -Force  Fa servir el directori de cua encara que ja existeixi i no estigui buit.
param(
  [switch]$Force
)

$q = 'C:\Claude\HomeAssistant\claude-queue'
$workdir = 'C:\Claude\HomeAssistant'

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
    # claude no té --cd: es canvia el directori de treball mentre dura la feina.
    Push-Location $workdir
    try {
      claude -p --model $job.model --effort $job.effort --output-format text $job.prompt `
        2> "$q\$base.err" | Out-File -FilePath "$q\$base.md" -Encoding UTF8
    }
    finally {
      Pop-Location
    }
    Rename-Item $_.FullName "$($_.FullName).done"
  }
  Start-Sleep -Seconds 5
}
