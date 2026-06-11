# Hermes di Nous Research vs Hermes del dashboard Go2

## Due software diversi (omonimia)

| | **Nous Hermes Agent** | **`go2_dashboard/hermes_agent.py`** |
|--|------------------------|-------------------------------------|
| Cos’è | Prodotto standalone ([repo](https://github.com/NousResearch/hermes-agent), [docs](https://hermes-agent.nousresearch.com/docs/)): skill che evolvono, memoria, gateway Telegram/Discord/… | Modulo **interno** del dashboard: linguaggio naturale → JSON intent → esecuzione Sport/DDS, OpenVLA/grasp, braccio |
| Telegram | Supportato nativamente (`hermes gateway`) | No (solo UI web `/api/hermes/command`) |
| “Skill” nel tempo | Loop di apprendimento integrato | Skill su disco `data/hermes_skills/` + memoria JSONL |

Il nome sul dashboard è stato **infelice** perché collide con il marchio Nous; **non** è quello che installa `curl … install.sh` di Nous.

## Conoscenza sul dashboard (skill disk + contesto live)

Oltre alla chat web, Hermes integrato carica:

1. **`data/hermes_skills/`** — markdown nel **system prompt** (conoscenza stabile da versionare in git), stesso *pattern* delle skill locali Agent Skills / shell OpenAI.
2. **Memoria JSONL** — nel messaggio utente se la UI lo abilita.
3. **`hermes_runtime_context_block()`** — nel messaggio utente a ogni richiesta: ultimo comando Sport gestito da questo processo Flask + riepilogo stack NX/DDS (variabile `GO2_HERMES_APPEND_RUNTIME_CONTEXT`, default on).

Questo **non** replica il loop evolutivo automatico del client Nous, ma dà **due binari di knowledge** comparabili: skill persistenti su disco + snapshot operativo per-turno. Il prodotto Nous resta utile per Telegram/MCP e può chiamare gli stessi endpoint HTTP.

## Cosa vuol dire “integrare Nous Hermes” (realistico)

Non si **sostituisce** tutto il backend robot con un comando: l’esecuzione su Unitree (DDS, grasp proxy, preset braccio) resta nel **processo Flask** sulla NX.

Integrazione sensata in **due binari**:

1. **Nous Hermes** gira come **secondo processo** (NX con risorse sufficienti, oppure PC/VPS sulla stessa LAN).
2. **Ponte verso il robot**: Nous invoca strumenti che chiamano **HTTP del dashboard** già esistente (es. `POST /api/hermes/command` o endpoint più piccoli), oppure un **MCP server** (vedi [MCP nel doc Nous](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)).

Così ottieni **Telegram + skill/evoluzione Nous** senza buttare via `_hermes_apply_intent` e la UI operator.

## Telegram (token **mai** in git)

1. Crea il bot con [@BotFather](https://t.me/BotFather), copia il token.
2. **Non** committare file tipo `hermestoken.txt` (anche sotto Download): è nel `.gitignore` del repo.
3. Sulla macchina dove gira il **gateway** Nous, configura `~/.hermes/.env` (Linux/WSL) o il path indicato dall’installer Windows:

   ```bash
   TELEGRAM_BOT_TOKEN=…
   TELEGRAM_ALLOWED_USERS=…
   ```

   (`TELEGRAM_ALLOWED_USERS` = tuo user id numerico, es. da @userinfobot.)

4. Segui la guida ufficiale: [Telegram – Hermes Agent](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/telegram.md) → `hermes gateway setup` poi `hermes gateway start`.

## Installazione Nous (riferimento rapido)

- Linux / macOS / WSL2:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
  ```

- Windows nativo: installer PowerShell nel [README Nous](https://github.com/NousResearch/hermes-agent) (beta).

**Jetson NX**: l’installer “full” può essere pesante (Node, ffmpeg, ecc.). Valuta **WSL2 sul PC di lab** o una **VM/VPS** che raggiunga `192.168.123.18` per il gateway Telegram; il dashboard resta sulla NX.

## Ponte HTTP verso il dashboard (idea minima)

Con Nous sulla stessa LAN della NX:

- Base dashboard: `http://192.168.123.18:5052` (porta tipica da `GO2_DASHBOARD_PORT`).
- Una **skill** o **tool** (o script consentito) può fare `POST /api/hermes/command` con JSON `{"text":"…", "capabilities":{…}}` come fa la UI — così il messaggio Telegram diventa comando robot **riusando** l’orchestratore attuale.

Per workflow più ricchi (solo piano OpenVLA, solo braccio), conviene esporre **endpoint dedicati** in Flask invece di inseguire sempre il mega JSON Hermes.

## Roadmap se vuoi “solo Nous” in UI

Sostituire del tutto il tab Agent web con Nous richiede: endpoint compatibile (Nous espone anche API stile OpenAI in alcuni setup) **e** mapping 1:1 col JSON intent del robot — lavoro di progetto, non swap di file.

---

**In sintesi:** il token va **solo** in `.env` locale Hermes Nous; il repo non deve contenerlo. Per avere **Hermes Nous con Telegram + skill nel tempo**, installi **Nous** a parte e colleghi il robot con un **ponte HTTP/MCP** al dashboard esistente.
