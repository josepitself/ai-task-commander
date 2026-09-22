# Vigila C:\Claude\HomeAssistant\gpt-queue\*.json i executa codex per cada feina.
# Format del JSON: { "model": "gpt-5.5", "effort": "medium", "prompt": "..." }
# Resultat: <nom>.md (resposta), <nom>.err (stderr), <nom>.json.done (feina consumida)
# Ús: .\gpt-watcher.ps1 [-Force] [-Queue <dir>] [-WorkDir <dir>]
#   -Force          Arrenca encara que hi hagi feines pendents (*.json) a la cua i les processa.
#   -Queue <dir>    Directori de cua (per defecte C:\Claude\HomeAssistant\gpt-queue).
#   -WorkDir <dir>  Directori de treball de la CLI (per defecte C:\Claude\HomeAssistant).
param(
  [switch]$Force,
  [string]$Queue = 'C:\Claude\HomeAssistant\gpt-queue',
  [string]$WorkDir = 'C:\Claude\HomeAssistant'
)

$q = $Queue
$workdir = $WorkDir

# Start-Process necessita un executable real (.exe o .cmd), no el shim codex.ps1 de npm.
$codexExe = $null
foreach ($n in 'codex.exe', 'codex.cmd') {
  $c = Get-Command $n -ErrorAction SilentlyContinue
  if ($c) { $codexExe = $c.Source; break }
}
if (-not $codexExe) {
  Write-Error "No s'ha trobat la CLI codex (codex.exe o codex.cmd) al PATH."
  exit 1
}

if (-not (Test-Path -LiteralPath $workdir -PathType Container)) {
  Write-Error "El directori de treball $workdir no existeix."
  exit 1
}

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
    # El prompt s'envia per stdin des d'un fitxer temporal (UTF-8 sense BOM): així no passa per la
    # línia d'ordres, que té límit de mida i problemes amb les cometes.
    $promptFile = Join-Path $env:TEMP "gpt-watcher-$PID-$base.prompt"
    $stdoutFile = Join-Path $env:TEMP "gpt-watcher-$PID-$base.stdout"
    [IO.File]::WriteAllText($promptFile, [string]$job.prompt, (New-Object Text.UTF8Encoding $false))
    try {
      # Start-Process passa els fitxers directament al procés fill: el .err queda en brut (UTF-8),
      # sense que PowerShell l'embolcalli com a NativeCommandError ni el converteixi a UTF-16.
      # La resposta final va a <nom>.md via -o; el que codex escriu a stdout es descarta.
      $argList = @(
        'exec', '--skip-git-repo-check', '--color', 'never',
        '--model', $job.model, '--cd', "`"$workdir`"", '-o', "`"$q\$base.md`"",
        '-c', "model_reasoning_effort=$($job.effort)", '-c', 'model_reasoning_summary=none',
        '-'
      )
      $p = Start-Process -FilePath $codexExe -ArgumentList $argList -NoNewWindow -PassThru `
        -RedirectStandardInput $promptFile -RedirectStandardOutput $stdoutFile -RedirectStandardError "$q\$base.err"
      $null = $p.Handle   # necessari perquè ExitCode s'ompli després de WaitForExit
      $p.WaitForExit()
      $exitCode = $p.ExitCode
    }
    catch {
      Write-Host "[$(Get-Date -Format HH:mm:ss)] ERROR no s'ha pogut executar codex: $($_.Exception.Message)" -ForegroundColor Red
      $exitCode = -1
    }
    finally {
      Remove-Item -LiteralPath $promptFile, $stdoutFile -Force -ErrorAction SilentlyContinue
    }
    $sw.Stop()
    Rename-Item -LiteralPath $file.FullName "$($file.FullName).done"
    $durada = "$($sw.Elapsed.TotalSeconds.ToString('N1')) s"
    # El resultat es decideix pel codi de sortida de la CLI (0 = bé; codex i claude tornen 1 si fallen,
    # per exemple amb un model inexistent). Si la CLI acaba amb 0 però no ha escrit el .md, també és error.
    $errSize = (Get-Item -LiteralPath "$q\$base.err" -ErrorAction SilentlyContinue).Length
    if ($exitCode -eq 0 -and (Test-Path -LiteralPath "$q\$base.md")) {
      Write-Host "[$(Get-Date -Format HH:mm:ss)] Fi feina ${base}: OK ($durada) -> $base.md" -ForegroundColor Green
    }
    else {
      $motiu = if ($exitCode -ne 0) { "codi de sortida $exitCode" } else { "no s'ha escrit $base.md" }
      if ($errSize -gt 0) { $motiu += ", revisa $base.err" }
      Write-Host "[$(Get-Date -Format HH:mm:ss)] Fi feina ${base}: ERROR ($durada, $motiu)" -ForegroundColor Red
    }
  }
  Start-Sleep -Seconds 5
}
