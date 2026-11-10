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
| Existeix amb `.json` que no són feines | Avís per cada fitxer (`El fitxer <nom> no és una feina (...): s'ignora`), no compten com a pendents i no bloquegen l'arrencada. |
| Existeix però és un fitxer | Error i sortida amb codi `1`. |

Només compten com a pendents els fitxers `*.json` que són feines vàlides (JSON correcte amb `model`, `effort` i `prompt`). L'històric (`*.json.done`, `*.md`, `*.err`) i qualsevol `*.pid` que hagi quedat d'una execució interrompuda es conserven al directori i no bloquegen l'arrencada, així es pot revisar més tard. Cap directori existent no s'esborra ni se sobreescriu mai.

**Contracte de la cua:** el directori de cua només conté feines, el seu històric i, mentre una feina s'executa, el seu `<nom>.pid` (vegeu [Fitxer .pid](#fitxer-pid-feina-en-curs)). Els fitxers que la feina necessiti (dades, diffs, prompts llargs) van a un altre lloc dins del directori de treball, o a la cua només si no tenen extensió `.json`. Un `.json` que no és una feina (per exemple un fitxer de dades deixat a la cua per error) no atura el watcher: s'avisa un cop i s'ignora, tant a l'arrencada com durant la vigilància. Aquesta tolerància és una xarxa de seguretat, no una invitació a guardar-hi dades.

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

Si un `<nom>.json` no és JSON vàlid, és buit o li falta algun dels camps `model`, `effort` o `prompt`, el watcher mostra una línia d'error en pantalla (`ERROR feina <nom> ignorada: ...`, o un avís si ja hi era en arrencar) i el salta sense aturar-se ni reanomenar-lo. El fitxer es queda a la cua i només es torna a provar si es modifica, així no es repeteix l'avís cada 5 segons. Un fitxer que encara s'està escrivint pot provocar un avís puntual i es processa igualment quan l'escriptura acaba.

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
| `<nom>.pid` | Només existeix mentre la feina s'executa: conté el PID del procés de la CLI. S'esborra quan la CLI acaba, abans de crear el `.json.done`. |

### Fitxer .pid (feina en curs)

Just després de la línia `Inici feina <nom>`, el watcher escriu `<nom>.pid` a la cua amb el PID del procés de la CLI (`codex` o `claude`) que està processant la feina. És el PID del procés fill, no el del watcher: a Windows, si la CLI és un `codex.cmd`/`claude.cmd` de npm, és el PID del `cmd.exe` que l'embolcalla, que viu exactament tant com la CLI. El fitxer conté només el número, en una sola línia i sense BOM (el `.sh` hi afegeix un salt de línia final). Quan la CLI acaba, el watcher esborra el `.pid` i tot seguit reanomena el `.json` a `.json.done`: si existeix `<nom>.json.done`, ja no existeix `<nom>.pid`.

Amb això un observador extern pot saber en quin estat és una feina sense mirar la pantalla del watcher:

| Fitxers presents | Estat |
|---|---|
| `<nom>.json`, sense `.pid` | Pendent: encara no ha començat, el watcher no corre, o l'ha rebutjat per invàlida (mira la pantalla del watcher). |
| `<nom>.json` i `<nom>.pid` amb un PID que existeix | En curs: la CLI encara treballa. |
| `<nom>.json` i `<nom>.pid` amb un PID que no existeix | El watcher va morir de cop a mig fer (finestra tancada, `kill -9`, apagada). La feina segueix pendent i es tornarà a executar en arrencar amb `-Force`/`--force`, que sobreescriurà el `.pid`. |
| `<nom>.json.done` | Acabada: mira la línia `Fi feina` o el `<nom>.md`. |

Per comprovar si el PID existeix:

```powershell
Get-Process -Id (Get-Content C:\Claude\HomeAssistant\gpt-queue\revisio-v3.pid) -ErrorAction SilentlyContinue
```

```bash
kill -0 "$(cat ~/Claude/HomeAssistant/gpt-queue/revisio-v3.pid)" 2>/dev/null && echo "en curs" || echo "el procés no existeix"
```

Si s'atura el watcher amb Ctrl+C a mitja feina, tant el watcher com la CLI acaben i el `.pid` s'esborra; el `.json` queda pendent perquè no s'ha arribat a reanomenar. A Linux, un `kill` del watcher fa el mateix (el trap de sortida atura la CLI). A Windows, si es mata el procés del watcher amb `Stop-Process` o des del Gestor de tasques, la CLI continua i el `.pid` es queda amb el seu PID, encara viu: la taula anterior segueix sent certa. Els PID es reutilitzen: un `.pid` antic podria apuntar per casualitat a un altre procés, així que, si cal, comprova que el procés sigui realment la CLI (`Get-Process` mostra el nom; a Linux, `ps -o comm= -p <pid>`).

## Monitor de la cua (`cli-watcher-monitor.py`)

Interfície de text per seguir una cua des d'una altra finestra. Només llegeix: no toca cap fitxer de la cua. Cal [uv](https://docs.astral.sh/uv/); les dependències (typer, textual, psutil) van declarades dins l'script i uv les instal·la soles.

```
uv run cli-watcher-monitor.py QUEUE_DIR [--refresh|-r SEGONS] [--jobs|-n N] [--runner auto|codex|claude]
```

Llista totes les feines de la cua, de la més nova a la més antiga, en una taula de N files visibles (5 per defecte) que es desplaça amb les fletxes. Per cada feina mostra l'estat (`OK`, `ERR`, `RUNNING` o `QUEUED`), l'hora d'entrada, inici, fi i durada. A sota, la petició de la feina seleccionada (esquerra) i el `.md` i el `.err` (dreta). La línia d'estat mostra el runner, el temps que fa que corre el watcher, el temps del monitor, l'interval de refresc i la ruta de la cua.

| Tecla | Acció |
|---|---|
| `↑` `↓` `PgUp` `PgDn` `Home` `End`, `1`-`9` | Tria feina (la llista es desplaça per tota la cua) |
| `Tab`, `←` `→` | Canvia de panell; `↑` `↓` `PgUp` `PgDn` `Home` `End` desplacen el panell actiu |
| `z` / `Esc` | Amplia el panell actiu / torna |
| `m` | Markdown renderitzat o text en brut |
| `r` | Refresca ara |
| `/` | Configuració: interval de refresc en segons (només per a la sessió) |
| `q` | Surt |

Els watchers no guarden el codi de sortida, així que el monitor dedueix l'estat de les feines acabades a partir dels fitxers, seguint la secció [Fitxers de sortida](#fitxers-de-sortida). Amb `codex` és `OK` si hi ha `.md`. Amb `claude` és `OK` si el `.md` té contingut, el `.err` és buit i el `.md` no és un missatge d'error curt de la CLI (menys d'1 KB que comença per `API Error` o `Error:`). Les hores d'inici i de fi d'una feina acabada també són aproximades: l'inici és la data de creació del `.err` (a Linux no està disponible i surt `—`) i la fi, l'última modificació del `.md` o el `.err`. Un `.json` amb `.pid` d'un procés que ja no existeix surt com a `QUEUED` amb l'avís `stale pid`, i un `.json` que no és una feina surt com a `ERR`.

El watcher es detecta pel procés que té el nom de l'script a la línia d'ordres (`powershell -File .\gpt-watcher.ps1`, `./gpt-watcher.sh`) i serveix la mateixa cua. Si s'ha arrencat escrivint `.\gpt-watcher.ps1` en una consola oberta, només es detecta mentre hi ha una feina en curs i no se'n sap el temps d'execució.

## Ús des de Claude Code

1. Claude escriu un `<nom>.json` a la cua del watcher que toqui.
2. Espera que aparegui `<nom>.json.done`. Mentrestant, `<nom>.pid` indica que la feina és en curs: si hi és i el PID que conté existeix, la CLI encara treballa i val la pena seguir esperant.
3. Comprova que la línia `Fi feina <nom>` digui `OK` (o que existeixi `<nom>.md`) i llegeix `<nom>.md` amb la resposta.

Si al cap d'uns 20 segons d'haver escrit el `.json` no hi ha ni `<nom>.pid` ni `<nom>.json.done`, el watcher no corre o ha rebutjat la feina: mira la pantalla del watcher abans de seguir esperant. Si hi ha `<nom>.pid` però el PID que conté no existeix, el watcher va morir a mig fer i cal tornar-lo a arrencar amb `-Force`/`--force`. A les rutes de Windows dins del `prompt`, vigila les seqüències d'escapament (`\a`, `\b`, `\n`, `\t`...): una ruta com `gpt-queue\aicw-...` escrita en una cadena no literal acaba amb un caràcter de control en lloc de `\a`. Fer servir barres `/` a les rutes ho evita.

### Exemple d'instruccions a CLAUDE.md

Per fer que Claude Code delegui feines a GPT de manera autònoma, afegeix una secció com aquesta al `CLAUDE.md` del projecte. Ajusta la ruta de la cua si has arrencat el watcher amb `-Queue`.

````markdown
## Delegar feines a GPT (codex) via el watcher

Quan et demani una segona opinió, una revisió o una anàlisi de GPT, delega-la
al `gpt-watcher` en comptes de respondre-la tu:

1. Escriu un fitxer de feina a `C:/Claude/HomeAssistant/gpt-queue/<nom>.json` amb
   l'eina Write (així queda en UTF-8; `Set-Content` o `>` de PowerShell no ho
   garanteixen). Fes servir un nom únic, per exemple `aicw-<tema>`. Contingut:

   ```json
   {
     "model": "gpt-5.5",
     "effort": "high",
     "prompt": "Text de la petició"
   }
   ```

   El codex s'executa al directori de treball del watcher, així que al `prompt`
   pots referir-te als fitxers per ruta relativa. Si has de passar-li molt text
   o dades, deixa-ho en un fitxer a part dins del directori de treball (mai un
   altre `.json` a la cua) i esmenta la ruta al `prompt`.

2. Espera que aparegui `C:/Claude/HomeAssistant/gpt-queue/<nom>.json.done`.
   Mentre la feina s'executa hi ha un `<nom>.pid` a la cua amb el PID del procés
   de codex; el watcher l'esborra quan codex acaba, abans de crear el `.json.done`.
   Fes-lo servir per decidir si has de seguir esperant:

   - Hi ha `<nom>.pid` i el PID existeix (a PowerShell,
     `Get-Process -Id (Get-Content <nom>.pid)` no dóna error): la feina és en
     curs, segueix esperant sense límit de temps.
   - Hi ha `<nom>.pid` però el PID no existeix: el watcher ha mort a mig fer.
     Atura't i avisa'm perquè el torni a arrencar amb `-Force`.
   - Al cap d'uns 20 segons no hi ha ni `<nom>.pid` ni `<nom>.json.done`:
     atura't i avisa'm, el watcher no corre o ha rebutjat la feina.

3. Si existeix `<nom>.md`, llegeix-lo: és la resposta de GPT. Si no hi és, la
   feina ha fallat; llegeix el final de `<nom>.err` per veure la línia `ERROR:`.
````

El `claude-watcher` funciona igual amb la cua `claude-queue` i els valors de `model` i `effort` de Claude Code.
