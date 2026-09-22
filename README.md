# ai-cli-watcher

Watchers que permeten a Claude Code delegar feines a un altre model a través de la seva CLI, mitjançant una cua de fitxers. Hi ha versió PowerShell (Windows) i bash (Linux) de cada un.

## Sinopsi

```
gpt-watcher.ps1    [-Force] [-Queue <dir>] [-WorkDir <dir>]          (Windows, PowerShell 5.1+)
claude-watcher.ps1 [-Force] [-Queue <dir>] [-WorkDir <dir>]

gpt-watcher.sh     [-f|--force] [-q|--queue DIR] [-w|--workdir DIR] [-h|--help]   (Linux, bash 4+)
claude-watcher.sh  [-f|--force] [-q|--queue DIR] [-w|--workdir DIR] [-h|--help]

Vigila el directori de cua i executa la CLI (codex o claude) per cada feina <nom>.json
que hi aparegui. No acaba mai per si sol: atura'l amb Ctrl+C.

Opcions:
  -Force, -f, --force        Arrenca encara que hi hagi feines pendents (*.json) a la cua
                             i les processa per ordre. Sense aquesta opció, si hi ha
                             pendents mostra un avís i surt amb codi 1.
  -Queue <dir>, -q, --queue DIR
                             Directori de cua. Es crea si no existeix.
                             Per defecte: C:\Claude\HomeAssistant\gpt-queue    (gpt-watcher)
                                          C:\Claude\HomeAssistant\claude-queue (claude-watcher)
                                          a Linux, $HOME/Claude/HomeAssistant/<gpt|claude>-queue
  -WorkDir <dir>, -w, --workdir DIR
                             Directori de treball des d'on s'executa la CLI. Ha d'existir.
                             Per defecte: C:\Claude\HomeAssistant (Linux: $HOME/Claude/HomeAssistant)
  -h, --help                 (només .sh) Mostra l'ajuda i surt.

Codis de sortida en arrencar:
  1  Feines pendents sense -Force, la cua és un fitxer, el directori de treball no
     existeix, o no es troba la CLI (codex/claude) o python3 (només .sh).
  2  (només .sh) Opció desconeguda o sense valor.
  Si arrenca bé, el watcher no acaba fins a Ctrl+C.
```

| Script | CLI que executa | Cua per defecte |
|---|---|---|
| `gpt-watcher.ps1` / `gpt-watcher.sh` | `codex` (OpenAI) | `C:\Claude\HomeAssistant\gpt-queue` / `$HOME/Claude/HomeAssistant/gpt-queue` |
| `claude-watcher.ps1` / `claude-watcher.sh` | `claude` (Claude Code) | `C:\Claude\HomeAssistant\claude-queue` / `$HOME/Claude/HomeAssistant/claude-queue` |

Els quatre funcionen igual: només canvien la CLI, el directori de cua i els valors acceptats a `model` i `effort`.

## Requisits

Windows (`.ps1`):

- PowerShell 5.1 o superior.
- La CLI corresponent al `PATH` com a executable real (`codex.exe`/`codex.cmd`, `claude.exe`/`claude.cmd`), amb sessió iniciada. La instal·lació amb npm o l'instal·lador oficial ja ho deixa així.

Linux (`.sh`):

- bash 4 o superior i `python3` (per llegir els fitxers JSON de la cua). Ubuntu els porta de sèrie.
- La CLI corresponent al `PATH`, amb sessió iniciada. A WSL cal una instal·lació Linux de la CLI: el `codex` de npm instal·lat a Windows apareix al `PATH` de WSL però llança el binari de Windows i falla.

## Execució

```powershell
.\gpt-watcher.ps1               # arrencada normal
.\gpt-watcher.ps1 -Force        # arrenca encara que hi hagi feines pendents i les processa
.\claude-watcher.ps1 -Queue D:\proves\cua -WorkDir D:\proves
```

```bash
./gpt-watcher.sh
./gpt-watcher.sh --force
./claude-watcher.sh --queue ~/proves/cua --workdir ~/proves
```

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

En arrencar, el watcher fa aquestes comprovacions sobre el directori de cua:

| Estat de la ruta | Comportament |
|---|---|
| No existeix | Es crea el directori i s'arrenca el watcher. |
| Existeix sense feines pendents | Mostra el recompte de feines fetes i pendents i s'arrenca el watcher. |
| Existeix amb feines pendents | Avís amb els noms dels `.json` pendents i sortida amb codi `1`. Amb `-Force`/`--force` es mostra l'avís, s'arrenca i es processen. |
| Existeix però és un fitxer | Error i sortida amb codi `1`. |

Només compten com a pendents els fitxers `*.json`. L'històric (`*.json.done`, `*.md`, `*.err`) es conserva al directori i no bloqueja l'arrencada, així es pot revisar més tard. Cap directori existent no s'esborra ni se sobreescriu mai.

La comprovació de pendents evita executar per error feines antigues que s'havien quedat a la cua. Si són vàlides, arrenca amb `-Force`/`--force` i es processaran per ordre.

## Format d'una feina

Un fitxer `<nom>.json` dins la cua, codificat en UTF-8:

```json
{
  "model": "gpt-5.5",
  "effort": "medium",
  "prompt": "Text de la petició"
}
```

| Camp | `gpt-watcher` | `claude-watcher` |
|---|---|---|
| `model` | Model que rep `codex exec --model`. | Model que rep `claude --model` (per exemple `claude-sonnet-5`, `claude-opus-5`). |
| `effort` | Valor de `model_reasoning_effort`. | Valor de `claude --effort`: `low`, `medium`, `high`, `xhigh` o `max`. |
| `prompt` | Petició completa que s'envia a la CLI. | Igual. |

Les feines es processen per ordre de data de modificació, cada 5 segons. La CLI s'executa amb el directori de treball indicat a `-WorkDir`/`--workdir`. El prompt s'envia a la CLI per l'entrada estàndard, no per la línia d'ordres, així no hi ha límit de mida ni problemes amb les cometes.

Si un `<nom>.json` no és JSON vàlid, és buit o li falta algun dels camps `model`, `effort` o `prompt`, el watcher mostra una línia d'error en pantalla (`ERROR feina <nom> ignorada: ...`) i el salta sense aturar-se ni reanomenar-lo. El fitxer es queda a la cua i només es torna a provar si es modifica, així no es repeteix l'avís cada 5 segons. Un fitxer que encara s'està escrivint pot provocar un avís puntual i es processa igualment quan l'escriptura acaba.

`claude-watcher` crida `claude -p` (mode no interactiu). En aquest mode, les eines que necessitarien confirmació es deneguen; si la feina ha de modificar fitxers, cal configurar els permisos a la `settings.json` del directori de treball.

## Missatges en pantalla

Per cada feina el watcher mostra dues línies, amb hora:

```
[10:32:05] Inici feina revisio-v3 (gpt-5.5/high)
[10:34:41] Fi feina revisio-v3: OK (156,2 s) -> revisio-v3.md
[10:34:41] Inici feina prova (model-inexistent/low)
[10:34:43] Fi feina prova: ERROR (2,2 s, codi de sortida 1, revisa prova.err)
```

El resultat es decideix pel codi de sortida de la CLI: `0` és `OK` (en verd) i qualsevol altre valor és `ERROR` (en vermell). Tant `codex` com `claude` tornen `1` quan la feina falla (model inexistent, error d'API, sessió no iniciada). Si la CLI acaba amb `0` però no ha escrit el `<nom>.md`, també es marca com a `ERROR`. Així es pot seguir el progrés sense mirar la cua.

## Fitxers de sortida

Per cada `<nom>.json` processat apareixen a la cua, en UTF-8:

| Fitxer | Contingut |
|---|---|
| `<nom>.md` | Resposta final del model. Amb `codex`, si la feina falla no es crea. Amb `claude`, si la feina falla conté el missatge d'error de la CLI. |
| `<nom>.err` | Sortida d'error estàndard de la CLI, tal com l'escriu. Amb `codex` sempre té contingut: hi va el registre de la sessió (model, ordres executades, resposta, tokens) i, si falla, la línia `ERROR: ...` al final. Amb `claude` és buit si tot ha anat bé. |
| `<nom>.json.done` | La feina original, reanomenada per marcar-la com a consumida. |

## Ús des de Claude Code

1. Claude escriu un `<nom>.json` a la cua del watcher que toqui.
2. Espera que aparegui `<nom>.json.done`.
3. Comprova que la línia `Fi feina <nom>` digui `OK` (o que existeixi `<nom>.md`) i llegeix `<nom>.md` amb la resposta.
