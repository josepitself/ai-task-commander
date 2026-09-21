# Vigila C:\Claude\HomeAssistant\claude-queue\*.json i executa claude (Claude Code) per cada feina.
# Format del JSON: { "model": "claude-sonnet-5", "effort": "medium", "prompt": "..." }
# Resultat: <nom>.md (resposta), <nom>.err (stderr), <nom>.json.done (feina consumida)
# Ús: .\claude-watcher.ps1 [-Force]
#   -Force  Arrenca encara que hi hagi feines pendents (*.json) a la cua i les processa.
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
# Feines invàlides ja avisades (ruta + data de modificació). Es tornen a provar si el fitxer canvia.
$invalid = @{}
while ($true) {
  Get-ChildItem "$q\*.json" | Sort-Object LastWriteTime | ForEach-Object {
    $file = $_
    $base = $file.BaseName
    $stamp = "$($file.FullName)|$($file.LastWriteTimeUtc.Ticks)"
    if ($invalid.ContainsKey($stamp)) { return }

    # Un JSON invàlid (o a mig escriure) no atura el watcher: s'avisa un cop i s'ignora fins que canviï.
    $job = $null
    $motiu = $null
    try {
      $job = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
      $motiu = "no és JSON vàlid ($($_.Exception.Message))"
    }
    if ($null -eq $motiu) {
      if ($null -eq $job) { $motiu = "el fitxer és buit" }
      elseif (-not $job.model -or -not $job.effort -or -not $job.prompt) { $motiu = "falten els camps model, effort o prompt" }
    }
    if ($null -ne $motiu) {
      Write-Host "[$(Get-Date -Format HH:mm:ss)] ERROR feina $base ignorada: $motiu" -ForegroundColor Red
      $invalid[$stamp] = $true
      return
    }

    Write-Host "[$(Get-Date -Format HH:mm:ss)] Inici feina $base ($($job.model)/$($job.effort))"
    $exitCode = $null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    # claude no té --cd: es canvia el directori de treball mentre dura la feina.
    Push-Location $workdir
    try {
      claude -p --model $job.model --effort $job.effort --output-format text $job.prompt `
        2> "$q\$base.err" | Out-File -FilePath "$q\$base.md" -Encoding UTF8
      $exitCode = $LASTEXITCODE
    }
    finally {
      Pop-Location
    }
    $sw.Stop()
    Rename-Item -LiteralPath $file.FullName "$($file.FullName).done"
    $errSize = (Get-Item -LiteralPath "$q\$base.err" -ErrorAction SilentlyContinue).Length
    $estat = "codi de sortida $exitCode"
    if ($errSize -gt 0) { $estat += ", revisa $base.err" }
    Write-Host "[$(Get-Date -Format HH:mm:ss)] Fi feina $base ($($sw.Elapsed.TotalSeconds.ToString('N1')) s, $estat) -> $base.md"
  }
  Start-Sleep -Seconds 5
}
