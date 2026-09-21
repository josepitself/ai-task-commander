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
.\gpt-watcher.ps1 -Force        # arrenca encara que hi hagi feines pendents i les processa

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
| Existeix sense feines pendents | Mostra el recompte de feines fetes i pendents i s'arrenca el watcher. |
| Existeix amb feines pendents | Avís amb els noms dels `.json` pendents i sortida amb codi `1`. Amb `-Force` es mostra l'avís, s'arrenca i es processen. |
| Existeix però és un fitxer | Error i sortida amb codi `1`. |

Només compten com a pendents els fitxers `*.json`. L'històric (`*.json.done`, `*.md`, `*.err`) es conserva al directori i no bloqueja l'arrencada, així es pot revisar més tard. Cap directori existent no s'esborra ni se sobreescriu mai.

La comprovació de pendents evita executar per error feines antigues que s'havien quedat a la cua. Si són vàlides, arrenca amb `-Force` i es processaran per ordre.

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

Per cada feina el watcher mostra dues línies en pantalla, amb hora: `Inici feina <nom> (model/effort)` en començar i `Fi feina <nom> (...)` en acabar, amb la durada, el codi de sortida de la CLI i un avís si `<nom>.err` no és buit. Així es pot seguir el progrés sense mirar la cua.

Si un `<nom>.json` no és JSON vàlid, és buit o li falta algun dels camps `model`, `effort` o `prompt`, el watcher mostra una línia d'error en pantalla (`ERROR feina <nom> ignorada: ...`) i el salta sense aturar-se ni reanomenar-lo. El fitxer es queda a la cua i només es torna a provar si es modifica, així no es repeteix l'avís cada 5 segons. Un fitxer que encara s'està escrivint pot provocar un avís puntual i es processa igualment quan l'escriptura acaba.

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
