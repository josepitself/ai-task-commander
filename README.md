# ai-cli-watcher

Permet a Claude Code delegar feines a un altre model a través de la seva CLI (`codex` o `claude`), mitjançant una cua de fitxers.

`cli-watcher-monitor.py` és una interfície de text que mostra la cua i, amb `--run`, també l'executa: un sol terminal, a Windows i a Linux. Per cada feina escriu un fitxer `<nom>.status` amb l'estat, les hores d'inici i fi i el codi de sortida.

Substitueix els antics watchers en PowerShell i bash (`gpt-watcher` / `claude-watcher`, `.ps1` i `.sh`), que ja no formen part del repositori (són a l'historial de git fins al commit `c9e1708`). Les cues que van processar es poden seguir obrint amb el monitor: vegeu [Feines sense .status](#feines-sense-status).

No facis córrer mai dos runners sobre la mateixa cua. El monitor no arrenca amb `--run` si n'hi ha un altre amb `--run`, si hi ha una feina amb `.pid` d'un procés viu, o si detecta un dels antics watchers.

## Sinopsi

```
uv run cli-watcher-monitor.py QUEUE_DIR [--run [-f|--force] [-w|--workdir DIR]]
                                        [-r|--refresh SEGONS] [-n|--jobs N] [--runner auto|codex|claude]
uv run cli-watcher-monitor.py -h|--help

Mostra les feines de la cua QUEUE_DIR i, amb --run, executa la CLI (codex o claude) per cada
feina <nom>.json que hi aparegui. Surt amb q (si hi ha una feina en curs, demana confirmació).

Opcions:
  --run                 Executa també la cua (substitueix gpt-watcher / claude-watcher) i escriu
                        un <nom>.status per feina. Sense --run només llegeix la cua.
  -f, --force           (amb --run) Arrenca encara que hi hagi feines pendents (*.json) i les
                        processa per ordre. Sense aquesta opció, si n'hi ha surt amb codi 1.
  -w, --workdir DIR     (amb --run) Directori de treball de la CLI. Ha d'existir.
                        Per defecte: el directori pare de la cua.
  --runner auto|codex|claude
                        CLI de la cua. Amb auto es dedueix del nom de la cua (gpt o codex →
                        codex, claude → claude) o, sense --run, del runner que hi corre.
  -r, --refresh SEGONS  Interval de refresc de la pantalla (per defecte 15; també amb /).
  -n, --jobs N          Files de feines visibles alhora (1-50, per defecte 5; també amb /);
                        la llista es desplaça.
  -h, --help            Mostra l'ajuda i surt.

Codis de sortida en arrencar:
  1  Feines pendents sense --force, la cua és un fitxer o no existeix (sense --run), el
     directori de treball no existeix, no es troba la CLI, o ja hi ha un runner a la cua.
  2  Opció desconeguda, o --force / --workdir sense --run.
```

| Cua | CLI | Valors de `model` i `effort` |
|---|---|---|
| `...\gpt-queue` | `codex` (OpenAI) | `codex exec --model` i `model_reasoning_effort` |
| `...\claude-queue` | `claude` (Claude Code) | `claude --model` i `claude --effort` (`low`, `medium`, `high`, `xhigh`, `max`) |

## Requisits

- [uv](https://docs.astral.sh/uv/). Les dependències del monitor (typer, textual, psutil) van declarades dins l'script i uv les instal·la soles la primera vegada.
- Amb `--run`, la CLI corresponent al `PATH` amb sessió iniciada. A Windows ha de ser un executable real (`codex.exe`/`codex.cmd`, `claude.exe`/`claude.cmd`), com deixa la instal·lació amb npm o l'instal·lador oficial. A WSL cal una instal·lació Linux de la CLI: el `codex` de npm instal·lat a Windows apareix al `PATH` de WSL però llança el binari de Windows i falla.

## Execució

```powershell
uv run cli-watcher-monitor.py C:\Claude\HomeAssistant\gpt-queue --run           # monitor + runner
uv run cli-watcher-monitor.py C:\Claude\HomeAssistant\gpt-queue --run --force   # i processa les pendents
uv run cli-watcher-monitor.py D:\proves\claude-queue --run -w D:\proves
uv run cli-watcher-monitor.py C:\Claude\HomeAssistant\gpt-queue                 # només mirar
```

```bash
uv run cli-watcher-monitor.py ~/Claude/HomeAssistant/gpt-queue --run
```

## Pantalla

A dalt, totes les feines de la cua, de la més nova a la més antiga, en una taula de N files visibles que es desplaça: estat (`OK`, `ERR`, `RUNNING` o `QUEUED`), model, effort, hora d'entrada, inici, fi i durada. A sota, la petició de la feina seleccionada (esquerra) i el `.md` i el `.err` (dreta). Amb `--run`, la barra superior mostra `▶ RUNNER` i el directori de treball, i cada inici i final de feina surt com a notificació (`▶ Started`, `✔ ... OK`, `✖ ... ERROR`). La línia d'estat mostra el runner, el temps que fa que corre (i la feina en curs), l'interval de refresc i la ruta de la cua.

| Tecla | Acció |
|---|---|
| `↑` `↓` `PgUp` `PgDn` `Home` `End`, `1`-`9` | Tria feina (la llista es desplaça per tota la cua) |
| `Tab`, `←` `→` | Canvia de panell; `↑` `↓` `PgUp` `PgDn` `Home` `End` desplacen el panell actiu |
| `z` / `Esc` | Amplia el panell actiu / torna |
| `m` | Markdown renderitzat o text en brut |
| `r` | Refresca ara |
| `/` | Configuració (només per a la sessió): interval de refresc en segons i files de feines visibles (`-n`). `Tab` canvia de camp, `Enter` aplica |
| `q` | Surt. Amb una feina en curs demana confirmació: `y` atura la CLI i la feina queda pendent |

Sense `--run`, el monitor detecta pel fitxer de bloqueig el monitor amb `--run` que executa la cua i en mostra el temps d'execució.

## Directori de cua

En arrencar amb `--run`, el monitor fa aquestes comprovacions abans d'obrir la pantalla:

| Estat de la ruta | Comportament |
|---|---|
| No existeix | Es crea el directori i arrenca. |
| Existeix sense feines pendents | Mostra el recompte de feines fetes i pendents i arrenca. |
| Existeix amb feines pendents | Avís amb els noms dels `.json` pendents i sortida amb codi `1`. Amb `--force` es mostra l'avís, arrenca i es processen. |
| Existeix amb `.json` que no són feines | Avís per cada fitxer; no compten com a pendents i no bloquegen l'arrencada. |
| Ja hi corre un runner (un altre `--run`, un watcher antic, o un `.pid` d'un procés viu) | Error i sortida amb codi `1`. |
| Existeix però és un fitxer | Error i sortida amb codi `1`. |

Només compten com a pendents els fitxers `*.json` que són feines vàlides. L'històric (`*.json.done`, `*.status`, `*.md`, `*.err`) es conserva i no bloqueja l'arrencada. Cap directori existent no s'esborra ni se sobreescriu mai.

**Contracte de la cua:** el directori de cua només conté feines, el seu històric, el `<nom>.pid` de la feina en curs, i el bloqueig i el `cli-watcher-monitor.pid` del runner. Els fitxers que la feina necessiti (dades, diffs, prompts llargs) van a un altre lloc dins del directori de treball, o a la cua només si no tenen extensió `.json`. Un `.json` que no és una feina s'avisa un cop i s'ignora fins que canviï; és una xarxa de seguretat, no una invitació a guardar-hi dades.

## Format d'una feina

Un fitxer `<nom>.json` dins la cua, codificat en UTF-8:

```json
{
  "model": "gpt-5.5",
  "effort": "medium",
  "prompt": "Text de la petició"
}
```

`model` i `effort` només poden contenir lletres, xifres i `._:/@+[]-`, perquè arriben a la línia d'ordres de la CLI. El `prompt` no té cap límit: s'envia a la CLI per l'entrada estàndard, en UTF-8.

Les feines es processen d'una en una, per ordre de data de modificació, mirant la cua cada 5 segons. Un `<nom>.json` que no és JSON vàlid, és buit, li falta algun camp o té caràcters no permesos surt a la pantalla com a `ERR`, es notifica un cop i es deixa a la cua sense reanomenar; es torna a provar quan es modifica.

`claude` s'executa amb `claude -p` (mode no interactiu): les eines que necessitarien confirmació es deneguen, així que si la feina ha de modificar fitxers cal configurar els permisos a la `settings.json` del directori de treball.

## Fitxers de sortida

| Fitxer | Contingut |
|---|---|
| `<nom>.status` | Estat de la feina en JSON (vegeu [Fitxer .status](#fitxer-status)). Si no existeix, la feina és a la cua. |
| `<nom>.md` | Resposta final del model. Amb `codex`, si la feina falla no es crea. Amb `claude`, si la feina falla conté el missatge d'error de la CLI. |
| `<nom>.err` | Sortida d'error estàndard de la CLI, tal com l'escriu. Amb `codex` sempre té contingut (el registre de la sessió) i, si falla, la línia `ERROR: ...` al final. Amb `claude` és buit si tot ha anat bé. |
| `<nom>.json.done` | La feina original, reanomenada quan acaba. |
| `<nom>.pid` | Només mentre la feina s'executa: el PID del procés de la CLI (vegeu [Fitxer .pid](#fitxer-pid)). |
| `.monitor-runner.lock` | Només mentre hi ha un monitor amb `--run`: PID i dades del runner. Impedeix que n'arrenqui un segon. |
| `cli-watcher-monitor.pid` | Només mentre hi ha un monitor amb `--run`: el PID del monitor, només el número en una línia (com el [`<nom>.pid`](#fitxer-pid)). S'esborra en sortir; si el monitor mor de cop es queda, i el següent `--run` el sobreescriu. Per això `cli-watcher-monitor` no és un nom de feina vàlid: un `cli-watcher-monitor.json` surt com a `ERR` i no s'executa. |

### Fitxer .status

JSON en UTF-8, escrit de manera atòmica (fitxer temporal i reanomenat), així que qui el llegeix mai no en veu una versió a mitges:

```json
{
  "job": "revisio-v3",
  "state": "OK",
  "reason": "exit code 0",
  "exit_code": 0,
  "queued": "2026-11-10T08:33:07+01:00",
  "start": "2026-11-10T08:49:29+01:00",
  "stop": "2026-11-10T08:55:39+01:00",
  "duration_s": 370.2,
  "runner": "codex",
  "model": "gpt-5.5",
  "effort": "high",
  "pid": 12345,
  "cli": "C:\\Users\\...\\npm\\codex.cmd",
  "workdir": "C:\\Claude\\HomeAssistant",
  "host": "PC",
  "runner_pid": 6789,
  "output": "revisio-v3.md",
  "errors": "revisio-v3.err"
}
```

| Camp | Valor |
|---|---|
| `state` | `RUNNING` mentre la CLI treballa; `OK` o `ERR` quan acaba. |
| `reason` | `exit code 0`, `exit code N`, `exit code 0 but <nom>.md was not written` o `cannot start the CLI: ...`. |
| `exit_code` | Codi de sortida de la CLI (`null` mentre corre). `OK` vol dir codi `0` i `<nom>.md` escrit. |
| `queued`, `start`, `stop` | Hores ISO 8601 amb zona: entrada a la cua (data del `.json`), inici i fi de la CLI. |
| `duration_s` | Durada en segons. |
| `pid`, `runner_pid`, `host` | PID de la CLI, PID del monitor que l'executa i nom de la màquina. |
| `inferred` | Opcional. `true` en un `.status` generat després a partir dels fitxers (feines dels antics watchers): el monitor marca les hores com a aproximades. |

Ordre dels canvis: en començar s'escriu el `.status` (`RUNNING`) i el `.pid`; en acabar s'esborra el `.pid`, s'escriu el `.status` final i es reanomena el `.json` a `.json.done`. Per tant, si existeix `<nom>.json.done`, el `.status` ja és final.

Si es surt del monitor amb una feina en curs, la CLI s'atura, s'esborren el `.pid` i el `.status` i el `.json` queda pendent: es tornarà a executar en arrencar amb `--run --force`. Si el monitor mor de cop (finestra tancada, procés matat, apagada), el `.status` es queda a `RUNNING` i el `.json` pendent. La CLI s'executa desacoblada de la finestra i pot seguir treballant fins que acabi: mentre el seu `pid` existeixi, la feina surt com a `RUNNING` i un nou `--run` no arrenca; quan el procés ja no existeix, surt com a `QUEUED` amb l'avís `stale` i es tornarà a executar amb `--force`.

#### Feines sense .status

Les feines acabades sense `.status` (fetes amb els antics watchers) també es mostren, però l'estat i les hores es dedueixen dels fitxers i surten marcats com a aproximats: amb `codex` és `OK` si hi ha `.md`; amb `claude`, si el `.md` té contingut, el `.err` és buit i el `.md` no és un missatge d'error curt de la CLI (comença per `API Error` o `Error:`). L'inici és la data de creació del `.err` (a Linux no està disponible) i la fi, l'última modificació del `.md` o el `.err`.

### Fitxer .pid

Mentre la CLI treballa, `<nom>.pid` conté el seu PID: només el número, en una línia. És el PID del procés fill: a Windows, si la CLI és un `codex.cmd`/`claude.cmd` de npm, és el del `cmd.exe` que l'embolcalla, que viu tant com la CLI. Per comprovar si encara existeix:

```powershell
Get-Process -Id (Get-Content C:\Claude\HomeAssistant\gpt-queue\revisio-v3.pid) -ErrorAction SilentlyContinue
```

```bash
kill -0 "$(cat ~/Claude/HomeAssistant/gpt-queue/revisio-v3.pid)" 2>/dev/null && echo "en curs" || echo "el procés no existeix"
```

Els PID es reutilitzen: un `.pid` antic podria apuntar per casualitat a un altre procés. Si cal, comprova'n el nom (`Get-Process` el mostra; a Linux, `ps -o comm= -p <pid>`).

## Ús des de Claude Code

1. Claude escriu un `<nom>.json` a la cua.
2. Espera que `<nom>.status` digui `OK` o `ERR` (o que aparegui `<nom>.json.done`). Mentrestant, si `state` és `RUNNING` i el `pid` existeix, la CLI encara treballa.
3. Si és `OK`, llegeix `<nom>.md`. Si és `ERR`, `reason` diu el motiu i el final de `<nom>.err` el detall.

Si al cap d'uns 20 segons d'haver escrit el `.json` no hi ha `<nom>.status`, el runner no corre o ha rebutjat la feina (mira la pantalla del monitor). A les rutes de Windows dins del `prompt`, vigila les seqüències d'escapament (`\a`, `\b`, `\n`, `\t`...): una ruta com `gpt-queue\aicw-...` escrita en una cadena no literal acaba amb un caràcter de control en lloc de `\a`. Fer servir barres `/` a les rutes ho evita.

### Exemple d'instruccions a CLAUDE.md

Per fer que Claude Code delegui feines a GPT de manera autònoma, afegeix una secció com aquesta al `CLAUDE.md` del projecte. Ajusta la ruta de la cua a la que faci servir el monitor.

````markdown
## Delegar feines a GPT (codex) via la cua

Quan et demani una segona opinió, una revisió o una anàlisi de GPT, delega-la
a la cua de codex en comptes de respondre-la tu:

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

   El codex s'executa al directori de treball del runner, així que al `prompt`
   pots referir-te als fitxers per ruta relativa. Si has de passar-li molt text
   o dades, deixa-ho en un fitxer a part dins del directori de treball (mai un
   altre `.json` a la cua) i esmenta la ruta al `prompt`.

2. Espera llegint `C:/Claude/HomeAssistant/gpt-queue/<nom>.status` (JSON):

   - `"state": "RUNNING"` i el `pid` existeix (a PowerShell,
     `Get-Process -Id <pid>` no dóna error): la feina és en curs, segueix
     esperant sense límit de temps.
   - `"state": "RUNNING"` però el `pid` no existeix: el runner ha mort a mig fer.
     Atura't i avisa'm perquè el torni a arrencar amb `--run --force`.
   - Al cap d'uns 20 segons encara no hi ha `<nom>.status`: atura't i avisa'm,
     el runner no corre o ha rebutjat la feina.

3. Quan `state` sigui `OK`, llegeix `<nom>.md`: és la resposta de GPT. Si és
   `ERR`, digues-me el `reason` i la línia `ERROR:` del final de `<nom>.err`.
````

Amb la cua `claude-queue` funciona igual, amb els valors de `model` i `effort` de Claude Code.
