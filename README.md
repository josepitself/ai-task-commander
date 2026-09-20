# ai-cli-watcher

Dos watchers de PowerShell que permeten a Claude Code delegar feines a un altre model a través de la seva CLI, mitjançant una cua de fitxers:

| Script | CLI que executa | Directori de cua |
|---|---|---|
| `gpt-watcher.ps1` | `codex` (OpenAI) | `C:\Claude\HomeAssistant\gpt-queue` |
| `claude-watcher.ps1` | `claude` (Claude Code) | `C:\Claude\HomeAssistant\claude-queue` |

Els dos funcionen igual: només canvien la CLI, el directori de cua i els valors acceptats a `model` i `effort`.

## Requisits

- Windows amb PowerShell 5.1 o superior.
- Per a `gpt-watcher.ps1`: la CLI `codex` al `PATH`, amb sessió iniciada.
- Per a `claude-watcher.ps1`: la CLI `claude` al `PATH`, amb sessió iniciada.

## Execució

```powershell
.\gpt-watcher.ps1               # arrencada normal
.\gpt-watcher.ps1 -Force        # accepta un directori de cua que ja tingui contingut

.\claude-watcher.ps1
.\claude-watcher.ps1 -Force
```

Els scripts no acaben mai per si sols. Atura'ls amb `Ctrl+C`.

### Si PowerShell no deixa executar scripts

Si apareix l'error `... no se puede cargar porque la ejecución de scripts está deshabilitada en este sistema` (o l'equivalent en anglès), la política d'execució ho bloqueja. Hi ha dues opcions:

Només per a aquesta execució, sense canviar res al sistema:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\gpt-watcher.ps1
powershell.exe -ExecutionPolicy Bypass -File .\claude-watcher.ps1
powershell.exe -ExecutionPolicy Bypass -File .\claude-watcher.ps1 -Force
```

De manera permanent per a l'usuari actual (permet scripts locals, i signats si vénen d'Internet):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

## Directori de cua

Cada watcher té la seva ruta fixa (variable `$q` dins l'script). En arrencar fa aquestes comprovacions:

| Estat de la ruta | Comportament |
|---|---|
| No existeix | Es crea el directori i s'arrenca el watcher. |
| Existeix i és buit | S'arrenca el watcher. |
| Existeix i no és buit | Avís i sortida amb codi `1`. Amb `-Force` es mostra l'avís però s'arrenca igualment. |
| Existeix però és un fitxer | Error i sortida amb codi `1`. |

Els fitxers ocults també compten com a contingut. Cap directori existent no s'esborra ni se sobreescriu mai.

Nota: els resultats de les feines (`.md`, `.err`, `.json.done`) es queden dins la cua. Per tant, després de la primera feina, tornar a arrencar el watcher requereix `-Force` o buidar el directori abans.

## Format d'una feina

Un fitxer `<nom>.json` dins la cua, codificat en UTF-8:

```json
{
  "model": "gpt-5.5",
  "effort": "medium",
  "prompt": "Text de la petició"
}
```

| Camp | `gpt-watcher.ps1` | `claude-watcher.ps1` |
|---|---|---|
| `model` | Model que rep `codex exec --model`. | Model que rep `claude --model` (per exemple `claude-sonnet-5`, `claude-opus-5`). |
| `effort` | Valor de `model_reasoning_effort`. | Valor de `claude --effort`: `low`, `medium`, `high`, `xhigh` o `max`. |
| `prompt` | Petició completa que s'envia a la CLI. | Igual. |

Les feines es processen per ordre de data de modificació, cada 5 segons. La CLI s'executa amb el directori de treball `C:\Claude\HomeAssistant`.

`claude-watcher.ps1` crida `claude -p` (mode no interactiu). En aquest mode, les eines que necessitarien confirmació es deneguen; si la feina ha de modificar fitxers, cal configurar els permisos a la `settings.json` del directori de treball.

## Fitxers de sortida

Per cada `<nom>.json` processat apareixen a la cua:

| Fitxer | Contingut |
|---|---|
| `<nom>.md` | Resposta final del model. |
| `<nom>.err` | Sortida d'error estàndard de la CLI. Buit si tot ha anat bé. |
| `<nom>.json.done` | La feina original, reanomenada per marcar-la com a consumida. |

## Ús des de Claude Code

1. Claude escriu un `<nom>.json` a la cua del watcher que toqui.
2. Espera que aparegui `<nom>.json.done`.
3. Llegeix `<nom>.md` amb la resposta.
