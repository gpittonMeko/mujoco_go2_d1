# Hermes disk skills (Jetson / PC repo)

Hermes legge **procedure in markdown** da questa cartella e le appende al **system prompt** (insieme al soul fisso). Non sostituisce le API OpenAI “hosted Skills” (Responses + shell); replica il pattern **percorso locale** descritto per gli agenti ([Agent Skills](https://agentskills.io/home), [OpenAI tools-skills](https://developers.openai.com/api/docs/guides/tools-skills)).

## Layout

1. **File in radice** — `*.md` / `*.txt`, ordinati per nome (usa prefissi `00_`, `01_`, …).  
   Ignorati: `README*`, nomi che iniziano con `.` o `_`.

2. **Bundle (una skill per cartella)** — sottocartella diretta che contiene **`SKILL.md`** (maiuscole/minuscole indifferenti).  
   Opzionale front matter YAML tra `---`:

   ```yaml
   ---
   name: Lab pick conventions
   description: Short English instructions for OpenVLA when facing the bench.
   ---
   ```

   Il corpo dopo il front matter è ciò che vede il modello.

## Variabili ambiente (processo dashboard)

| Variabile | Default | Significato |
|-----------|---------|-------------|
| `GO2_HERMES_SKILLS_DIR` | *(repo)* `data/hermes_skills` | Altra cartella assoluta o relativa alla root repo |
| `GO2_HERMES_SKILLS_DISABLE` | off | `1` = non caricare nulla |
| `GO2_HERMES_SKILLS_MAX_CHARS` | `14000` | Tetto totale testo skill nel prompt |
| `GO2_HERMES_SKILL_FILE_MAX_CHARS` | `4500` | Tetto per singolo file |
| `GO2_HERMES_SKILLS_MAX_FILES` | `32` | Massimo numero di file sorgente |
| `GO2_HERMES_APPEND_RUNTIME_CONTEXT` | `1` | `0` = non appendere lo snapshot Sport/stack al messaggio Hermes |
| `GO2_HERMES_RUNTIME_CONTEXT_MAX_CHARS` | `1800` | Tetto caratteri per quel blocco |

## Knowledge creation (skill persistenti locali, analogia Nous)

Questo dashboard **non** incorpora il runtime [Nous Hermes Agent](https://github.com/NousResearch/hermes-agent). La **conoscenza stabile** che vuoi sempre nel modello va qui (markdown in repo → **system prompt**), come le skill su disco che altri agent caricano da path locale.

| Livello | Dove finisce | Contenuto tipico |
|--------|----------------|------------------|
| **Disk skills** | System prompt | Convenzioni laboratorio, lessico IT↔JSON, regole sicurezza Hermes. |
| **Memoria operatore** | User message (se UI on) | Preferenze, note, `turn_log`. |
| **Live dashboard context** | User message (ogni POST) | Ultimo Sport RPC di questo processo + stack NX — `hermes_runtime_context_block()` in `hermes_agent.py`. |

Per gateway Telegram / loop evolutivi del prodotto Nous: installazione separata + HTTP verso questa dashboard — `docs/HERMES_NOUS_INTEGRATION.md`.

## Sicurezza

Tratta i file come **istruzioni privilegiate**: solo personale di laboratorio; non esporre upload arbitrario da utenti finali senza revisione (rischio prompt injection, come avvisa la doc OpenAI sulle Skills).

## Verifica

`GET /api/hermes/status` include `skills_root`, `skills_source_files`, `skills_prompt_chars`, più `hermes_runtime_context` e `hermes_knowledge_layers_it` per il contesto live / livelli di conoscenza.
