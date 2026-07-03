"""Flask app — telemetria motori Go2 (Mission 1: stato / surriscaldamento)."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request

from go2_dashboard.go2_motor_health import get_lowstate_store
from go2_dashboard.go2_motor_event_log import get_motor_events
from go2_dashboard.go2_motor_sport import invoke_dds_sport_ping, invoke_sport_pose, last_manual_sport_status

try:
    from go2_dashboard.go2_thermal_protect import attach_thermal_protector
    from go2_dashboard.go2_thermal_settings import get_thermal_settings
except ImportError:
    attach_thermal_protector = None  # type: ignore
    get_thermal_settings = None  # type: ignore

_INDEX_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Go2 Motor Health</title>
  <style>
    :root { font-family: Inter, Aptos, system-ui, sans-serif; background: transparent; color: #10334a; }
    body { margin: 1rem 1.25rem 2rem; max-width: none; background: transparent; }
    h1 { font-size: 1.45rem; font-weight: 750; color:#0a659b; letter-spacing:-.03em; }
    .meta { color: #52788e; font-size: 0.9rem; margin-bottom: 1rem; }
    .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem; }
    .ok { background: #d8f7e4; color: #087548; }
    .warn { background: #fff1c9; color: #a06000; }
    .critical { background: #ffe0e3; color: #b62f41; }
    .off { background: #e5eef2; color: #648090; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th, td { text-align: left; padding: 0.48rem 0.55rem; border-bottom: 1px solid rgba(20,111,153,.14); }
    th { color: #52788e; font-weight: 650; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.75rem; margin: 1rem 0; }
    .card { background: linear-gradient(145deg,rgba(255,255,255,.92),rgba(226,248,255,.76)); border:1px solid rgba(255,255,255,.92); border-radius: 16px; padding: 0.85rem 1rem; box-shadow:0 10px 24px rgba(16,102,148,.12); }
    .card h2 { margin: 0 0 0.4rem; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: #4c7890; }
    .card .val { font-size: 1.25rem; font-weight: 600; }
    .card.critical { border: 1px solid #ef9aa4; background: #fff0f2; }
    .card.critical .val { color: #c43d4d; }
    .card.warn { border: 1px solid #f1c36d; background: #fff8df; }
    .card.warn .val { color: #ac6905; }
    .sport-row { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; margin: 0.75rem 0 1rem; }
    .sport-btn {
      font: inherit; font-size: 0.9rem; font-weight: 600;
      padding: 0.55rem 1.1rem; border-radius: 8px; border: none; cursor: pointer;
      color: #fff; background: linear-gradient(180deg,#2ba4eb,#087fc9); box-shadow:0 6px 14px rgba(8,102,166,.18);
    }
    .sport-btn.secondary { background: linear-gradient(180deg,#6b99ad,#476f82); }
    .sport-btn:disabled { opacity: 0.55; cursor: wait; }
    #sportMsg { color: #52788e; font-size: 0.85rem; min-height: 1.2em; }
    .settings-panel {
      background: rgba(255,255,255,.72); border-radius: 16px; padding: 0.95rem 1rem; margin: 0.75rem 0 1rem;
      border: 1px solid rgba(255,255,255,.9); box-shadow:0 10px 24px rgba(16,102,148,.1);
    }
    .settings-panel h2 { margin: 0 0 0.6rem; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: #47758c; }
    .settings-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 0.65rem 1rem; }
    .settings-grid label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.82rem; color: #52788e; }
    .settings-grid input[type="number"] {
      font: inherit; padding: 0.35rem 0.5rem; border-radius: 6px; border: 1px solid #334155;
      background: rgba(255,255,255,.86); color: #10334a; max-width: 8rem;
    }
    .settings-grid input[type="checkbox"] { width: 1rem; height: 1rem; accent-color: #2563eb; }
    .check-row { flex-direction: row !important; align-items: center; gap: 0.5rem !important; color: #10334a !important; }
    .settings-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-top: 0.75rem; }
    .settings-actions button {
      font: inherit; font-size: 0.82rem; font-weight: 600; padding: 0.45rem 0.9rem;
      border-radius: 9px; border: none; cursor: pointer; color: #fff; background: #547f93;
    }
    .settings-actions button.primary { background: #2563eb; }
    #settingsMsg { font-size: 0.82rem; color: #52788e; }
    #weightPlan { font-size: 0.82rem; color: #52788e; margin-top: 0.35rem; }
    .event-log-panel {
      background: #0d1117; border: 1px solid #243040; border-radius: 8px;
      padding: 0.75rem 1rem; margin: 1rem 0; max-height: 280px; overflow-y: auto;
      font-family: ui-monospace, monospace; font-size: 0.78rem; line-height: 1.45;
    }
    .event-log-panel h2 { margin: 0 0 0.5rem; font-family: system-ui, sans-serif; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: #8b9aab; }
    .log-line { padding: 0.2rem 0; border-bottom: 1px solid #1a222c; }
    .log-line .ts { color: #64748b; margin-right: 0.5rem; }
    .log-line.info { color: #94a3b8; }
    .log-line.warn { color: #fbbf24; }
    .log-line.critical { color: #f87171; }
  </style>
</head>
<body>
  <h1>Go2 — stato motori</h1>
  <p class="meta">Topic DDS <code>rt/lowstate</code> · UI aggiornata ogni {{ poll_ms }} ms</p>
  <p id="conn" class="meta">Connessione…</p>
  <p id="thermal" class="meta"></p>
  <div class="sport-row">
    <button type="button" class="sport-btn" id="btnStand">Stand up</button>
    <button type="button" class="sport-btn secondary" id="btnCrouch">Crouch</button>
    <span id="sportMsg"></span>
  </div>
  <div class="settings-panel">
    <h2>Autobilanciamento peso (sur riscaldamento)</h2>
    <div class="settings-grid">
      <label class="check-row"><input type="checkbox" id="setWeightBalance"/> Autobilanciamento attivo</label>
      <label class="check-row"><input type="checkbox" id="setPreBalance"/> BalanceStand prima del crouch</label>
      <label class="check-row"><input type="checkbox" id="setRecoveryStand"/> Recupero automatico con isteresi (bilancio→stand up, crouch→autobilanciamento)</label>
      <label>Soglia autobilanciamento (°C)
        <input type="number" id="setBalanceC" min="40" max="70" step="1"/>
      </label>
      <label>Soglia crouch (°C)
        <input type="number" id="setCrouchC" min="60" max="90" step="1"/>
      </label>
      <label>Isteresi (°C)
        <input type="number" id="setHysteresisC" min="1" max="20" step="1"/>
      </label>
      <p id="derivedThresholds" class="meta" style="grid-column: 1 / -1; margin: 0;"></p>
      <label>Soglia anteriori (%)
        <input type="number" id="setFrontHigh" min="50" max="80" step="1"/>
      </label>
      <label>Soglia posteriori (%)
        <input type="number" id="setFrontLow" min="20" max="50" step="1"/>
      </label>
      <label>Velocità avanti/indietro (m/s)
        <input type="number" id="setShiftVx" min="0.02" max="0.2" step="0.01"/>
      </label>
      <label>Velocità laterale (m/s)
        <input type="number" id="setShiftVy" min="0.02" max="0.15" step="0.01"/>
      </label>
      <label>Durata spostamento (s)
        <input type="number" id="setShiftDur" min="0.2" max="2" step="0.05"/>
      </label>
    </div>
    <div class="settings-actions">
      <button type="button" class="primary" id="btnSaveSettings">Salva impostazioni</button>
      <button type="button" id="btnBalanceNow">Prova bilanciamento ora</button>
      <button type="button" id="btnRecoveryStand">Autobilanciamento recupero (post-crouch)</button>
      <button type="button" id="btnDdsPing">Test DDS Sport</button>
      <span id="settingsMsg"></span>
    </div>
    <div id="weightPlan"></div>
  </div>
  <div class="event-log-panel">
    <h2>Log movimenti e termica</h2>
    <div id="eventLog"><span class="log-line info">In attesa eventi…</span></div>
  </div>
  <div class="grid" id="summary"></div>
  <table>
    <thead>
      <tr>
        <th>Motore</th><th>q (rad)</th><th>dq</th><th>τ est</th><th>Temp °C</th><th>Lost</th><th>Stato</th>
      </tr>
    </thead>
    <tbody id="legs"></tbody>
  </table>
  <script>
    function badge(cls, text) {
      return '<span class="badge ' + cls + '">' + text + '</span>';
    }
    const healthLabel = { ok: 'OK', warn: 'attenzione', critical: 'CRITICO' };
    const POLL_MS = {{ poll_ms }};
    function motorHealthBadge(m, balanceC, crouchC) {
      if (crouchC != null && m.temperature_c >= crouchC) {
        return badge('critical', 'CROUCH ' + crouchC + '°C');
      }
      if (balanceC != null && m.temperature_c >= balanceC) {
        return badge('warn', 'BILANCIO ' + balanceC + '°C');
      }
      const cls = m.health;
      return badge(cls, healthLabel[cls] || cls);
    }
    async function sendSport(mode) {
      const btnStand = document.getElementById('btnStand');
      const btnCrouch = document.getElementById('btnCrouch');
      const msg = document.getElementById('sportMsg');
      btnStand.disabled = true;
      btnCrouch.disabled = true;
      msg.textContent = (mode === 'stand_up' ? 'Stand up' : 'Crouch') + ' in corso…';
      try {
        const r = await fetch('/api/motor/sport/' + (mode === 'stand_up' ? 'stand' : 'crouch'), { method: 'POST' });
        const j = await r.json();
        if (j.ok) {
          msg.innerHTML = badge('ok', 'OK') + ' ' + (j.hint || j.mode || mode);
        } else {
          msg.innerHTML = badge('critical', 'errore') + ' ' + (j.reason || j.hint || JSON.stringify(j));
        }
      } catch (e) {
        msg.textContent = 'Errore: ' + e;
      } finally {
        btnStand.disabled = false;
        btnCrouch.disabled = false;
      }
    }
    ['setBalanceC', 'setCrouchC', 'setHysteresisC'].forEach(function (id) {
      document.getElementById(id).addEventListener('input', updateDerivedThresholds);
    });
    document.getElementById('btnStand').addEventListener('click', function () { sendSport('stand_up'); });
    document.getElementById('btnCrouch').addEventListener('click', function () { sendSport('crouch'); });

    function pctToShare(id) {
      return parseFloat(document.getElementById(id).value) / 100;
    }
    function shareToPct(v) {
      return Math.round(parseFloat(v) * 100);
    }
    function updateDerivedThresholds() {
      const bal = parseInt(document.getElementById('setBalanceC').value, 10);
      const crouch = parseInt(document.getElementById('setCrouchC').value, 10);
      const h = parseInt(document.getElementById('setHysteresisC').value, 10);
      const el = document.getElementById('derivedThresholds');
      if (!el || isNaN(bal) || isNaN(crouch) || isNaN(h)) return;
      const standExit = Math.max(0, bal - h);
      const crouchExit = Math.max(0, crouch - h);
      const crouchStay = crouchExit + 1;
      el.textContent =
        'Soglie derivate: stand up ≤ ' + standExit + '°C (' + bal + '−' + h + ') · ' +
        'crouch resta fino a ' + crouchStay + '°C · autobilanciamento ≤ ' + crouchExit +
        '°C (' + crouch + '−' + h + ') — si aggiornano salvando le caselle sopra';
    }
    function applySettingsToForm(s) {
      if (!s) return;
      document.getElementById('setWeightBalance').checked = !!s.weight_balance_enabled;
      document.getElementById('setPreBalance').checked = !!s.pre_balance_crouch;
      document.getElementById('setRecoveryStand').checked = s.recovery_stand_enabled !== false;
      if (s.balance_threshold_c != null) document.getElementById('setBalanceC').value = s.balance_threshold_c;
      if (s.crouch_threshold_c != null) document.getElementById('setCrouchC').value = s.crouch_threshold_c;
      if (s.threshold_hysteresis_c != null) document.getElementById('setHysteresisC').value = s.threshold_hysteresis_c;
      document.getElementById('setFrontHigh').value = shareToPct(s.imbalance_front_high);
      document.getElementById('setFrontLow').value = shareToPct(s.imbalance_front_low);
      document.getElementById('setShiftVx').value = s.shift_vx_mps;
      document.getElementById('setShiftVy').value = s.shift_vy_mps;
      document.getElementById('setShiftDur').value = s.shift_duration_s;
      updateDerivedThresholds();
    }
    function readSettingsFromForm() {
      return {
        weight_balance_enabled: document.getElementById('setWeightBalance').checked,
        pre_balance_crouch: document.getElementById('setPreBalance').checked,
        recovery_stand_enabled: document.getElementById('setRecoveryStand').checked,
        balance_threshold_c: parseInt(document.getElementById('setBalanceC').value, 10),
        crouch_threshold_c: parseInt(document.getElementById('setCrouchC').value, 10),
        threshold_hysteresis_c: parseInt(document.getElementById('setHysteresisC').value, 10),
        imbalance_front_high: pctToShare('setFrontHigh'),
        imbalance_front_low: pctToShare('setFrontLow'),
        shift_vx_mps: parseFloat(document.getElementById('setShiftVx').value),
        shift_vy_mps: parseFloat(document.getElementById('setShiftVy').value),
        shift_duration_s: parseFloat(document.getElementById('setShiftDur').value),
      };
    }
    async function loadSettings() {
      try {
        const r = await fetch('/api/motor/thermal/settings');
        const j = await r.json();
        if (j.ok && j.settings) applySettingsToForm(j.settings);
      } catch (e) { /* ignore */ }
    }
    document.getElementById('btnSaveSettings').addEventListener('click', async function () {
      const msg = document.getElementById('settingsMsg');
      msg.textContent = 'Salvataggio…';
      try {
        const r = await fetch('/api/motor/thermal/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(readSettingsFromForm()),
        });
        const j = await r.json();
        if (j.ok) {
          applySettingsToForm(j.settings);
          msg.innerHTML = badge('ok', 'salvato');
        } else {
          msg.innerHTML = badge('critical', 'errore') + ' ' + (j.reason || '');
        }
      } catch (e) {
        msg.textContent = 'Errore: ' + e;
      }
    });
    document.getElementById('btnBalanceNow').addEventListener('click', async function () {
      const msg = document.getElementById('settingsMsg');
      msg.textContent = 'Bilanciamento in corso…';
      try {
        const r = await fetch('/api/motor/thermal/balance_now', { method: 'POST' });
        const j = await r.json();
        if (j.ok) {
          var wp = j.weight_plan || (j.steps && j.steps.weight_shift && j.steps.weight_shift.plan);
          var extra = wp && wp.reasons ? wp.reasons.join(', ') : '';
          msg.innerHTML = badge('ok', 'OK') + ' ' + (j.hint || extra || 'bilanciamento eseguito');
        } else {
          var fail = j.reason || 'skip';
          if (j.steps && j.steps.stand_up && !j.steps.stand_up.ok) fail = 'Stand up fallito';
          else if (j.steps && j.steps.weight_shift && !j.steps.weight_shift.ok) fail = 'Spostamento peso fallito';
          msg.innerHTML = badge('warn', fail) + ' ' + (j.hint || '');
        }
      } catch (e) {
        msg.textContent = 'Errore: ' + e;
      }
      refreshEventLog(true);
    });
    document.getElementById('btnRecoveryStand').addEventListener('click', async function () {
      const msg = document.getElementById('settingsMsg');
      msg.textContent = 'Stand up recupero in corso…';
      try {
        const r = await fetch('/api/motor/thermal/recovery_now', { method: 'POST' });
        const j = await r.json();
        if (j.ok) {
          msg.innerHTML = badge('ok', 'OK') + ' ' + (j.hint || 'Stand up recupero eseguito');
        } else {
          msg.innerHTML = badge('critical', 'fail') + ' ' + (j.hint || j.reason || 'stand up fallito');
        }
      } catch (e) {
        msg.textContent = 'Errore: ' + e;
      }
      refreshEventLog(true);
    });
    document.getElementById('btnDdsPing').addEventListener('click', async function () {
      const msg = document.getElementById('settingsMsg');
      msg.textContent = 'Test DDS in corso…';
      try {
        const r = await fetch('/api/motor/sport/dds_ping', { method: 'POST' });
        const j = await r.json();
        if (j.ok) {
          msg.innerHTML = badge('ok', 'DDS OK') + ' ' + (j.hint_it || '');
        } else {
          msg.innerHTML = badge('critical', 'DDS fail') + ' ' +
            (j.motion_switcher_check_meaning || j.reason || j.hint_it || '');
        }
      } catch (e) {
        msg.textContent = 'Errore: ' + e;
      }
      refreshEventLog(true);
    });
    loadSettings();

    function renderWeightPlan(tr) {
      const el = document.getElementById('weightPlan');
      if (!tr || !tr.enabled) { el.textContent = ''; return; }
      const load = tr.weight_hint || {};
      const plan = tr.weight_plan_now;
      let txt = '';
      if (load.front_share != null) {
        txt += 'Carico anteriori ' + Math.round(load.front_share * 100) + '%';
        if (load.left_share != null) txt += ' · sinistra ' + Math.round(load.left_share * 100) + '%';
      }
      if (load.hint_it) txt += (txt ? ' — ' : '') + load.hint_it;
      if (plan) {
        txt += (txt ? '<br>' : '') + badge('warn', 'piano attivo') +
          ' vx=' + plan.vx + ' vy=' + plan.vy + ' (' + (plan.reasons || []).join(', ') + ')';
      } else if (tr.settings && tr.settings.weight_balance_enabled) {
        txt += (txt ? '<br>' : '') + badge('ok', 'bilanciato') + ' nessuno spostamento necessario';
      }
      el.innerHTML = txt;
    }

    let lastLogEpoch = 0;
    async function refreshEventLog(force) {
      try {
        const r = await fetch('/api/motor/events?limit=80');
        const j = await r.json();
        if (!j.ok || !j.events) return;
        const newest = j.events.length ? (j.events[j.events.length - 1].epoch || 0) : 0;
        if (!force && newest <= lastLogEpoch && j.events.length) return;
        lastLogEpoch = newest;
        const box = document.getElementById('eventLog');
        if (!j.events.length) {
          box.innerHTML = '<span class="log-line info">Nessun evento ancora.</span>';
          return;
        }
        box.innerHTML = j.events.slice().reverse().map(function (e) {
          return '<div class="log-line ' + (e.level || 'info') + '">' +
            '<span class="ts">' + e.ts + '</span>' + e.message + '</div>';
        }).join('');
      } catch (err) { /* ignore */ }
    }

    async function tick() {
      try {
        const r = await fetch('/api/motor/state');
        const j = await r.json();
        const conn = document.getElementById('conn');
        if (!j.ok) {
          conn.innerHTML = badge('off', 'offline') + ' ' + (j.error || 'nessun dato');
          return;
        }
        const d = j.data;
        const th = d.thermal;
        const connCls = j.connected ? 'ok' : 'warn';
        conn.innerHTML = badge(connCls, j.connected ? 'live' : 'stale') +
          ' messaggi=' + j.message_count + ' · età ' + (j.last_message_age_s ?? '?') + 's · domain ' + j.dds_domain;
        const tr = await fetch('/api/motor/thermal/status').then(r => r.json()).catch(() => null);
        const tp = document.getElementById('thermal');
        const balanceC = th.balance_threshold_c;
        const crouchC = th.crouch_threshold_c;
        if (tr && tr.enabled) {
          const h = tr.threshold_hysteresis_c || 7;
          const balClear = tr.balance_clear_threshold_c != null ? tr.balance_clear_threshold_c : (tr.balance_threshold_c - h);
          const crouchRec = tr.crouch_recovery_threshold_c != null ? tr.crouch_recovery_threshold_c : (tr.crouch_threshold_c - h);
          const crouchStay = tr.crouch_stay_min_threshold_c != null ? tr.crouch_stay_min_threshold_c : (crouchRec + 1);
          const maxT = tr.max_leg_temp_c;
          const maxM = tr.max_leg_temp_motor || '—';
          const mode = tr.thermal_mode || 'normal';
          const modeLabel = { normal: 'STAND UP', balance: 'AUTOBILANCIAMENTO', crouch: 'CROUCH' };
          const modeCls = { normal: 'ok', balance: 'warn', crouch: 'critical' };
          tp.innerHTML = badge(modeCls[mode] || 'ok', modeLabel[mode] || mode) +
            ' · temp max ' + maxT + '°C (' + maxM + ')';
          if (mode === 'normal') {
            tp.innerHTML += '<br>' + badge('ok', 'regole') +
              ' stand up fino a ' + (tr.balance_threshold_c - 1) + '°C · bilancio ≥ ' + tr.balance_threshold_c +
              '°C · crouch ≥ ' + tr.crouch_threshold_c + '°C';
          } else if (mode === 'balance') {
            tp.innerHTML += '<br>' + badge('warn', 'regole') +
              ' resta ' + (balClear + 1) + '–' + (tr.crouch_threshold_c - 1) + '°C · stand up ≤ ' + balClear +
              '°C (' + tr.balance_threshold_c + '−' + h + ') · crouch ≥ ' + tr.crouch_threshold_c + '°C';
          } else if (mode === 'crouch') {
            const delta = maxT > crouchRec ? (' · mancano ' + (maxT - crouchRec) + '°C') : '';
            tp.innerHTML += '<br>' + badge('critical', 'regole') +
              ' resta fino a ' + crouchStay + '°C · autobilanciamento ≤ ' + crouchRec +
              '°C (' + tr.crouch_threshold_c + '−' + h + ')' + delta;
          }
          if (tr.last_balance_at) {
            tp.innerHTML += '<br>' + badge('warn', 'bilancio auto') + ' ' + tr.last_balance_at +
              ' su ' + (tr.last_balance_motors || []).join(', ');
          }
          if (tr.last_trigger_at) {
            tp.innerHTML += '<br>' + badge('critical', 'crouch auto') + ' ' + tr.last_trigger_at +
              ' su ' + (tr.last_trigger_motors || []).join(', ');
          }
          if (tr.last_recovery_at) {
            tp.innerHTML += '<br>' + badge('ok', 'recupero isteresi') + ' ' + tr.last_recovery_at;
          }
          if (tr.weight_hint && tr.weight_hint.hint_it) {
            tp.innerHTML += '<br><span style="color:#94a3b8">' + tr.weight_hint.hint_it + '</span>';
          }
          renderWeightPlan(tr);
        } else if (tr) {
          tp.innerHTML = badge('off', 'protezione termica OFF') + ' (GO2_THERMAL_PROTECT=1 sulla NX)';
          renderWeightPlan(null);
        }
        const tempCardCls = th.above_crouch_threshold ? 'critical' : (th.above_balance_threshold ? 'warn' : 'ok');
        document.getElementById('summary').innerHTML = [
          ['Temp max', th.max_temperature_c + ' °C (' + (th.max_temperature_motor || '—') + ')', tempCardCls],
          ['SOC', d.bms.soc_percent + ' %', 'ok'],
          ['Bus', d.power.voltage_v + ' V / ' + d.power.current_a + ' A', 'ok'],
          ['Ventole', (th.fan_frequency_hz || []).join(', ') + ' Hz', th.above_balance_threshold ? 'warn' : 'ok'],
          ['NTC scheda', th.temperature_ntc1_c + ' / ' + th.temperature_ntc2_c + ' °C', 'ok'],
        ].map(function (row) {
          const t = row[0], v = row[1], c = row[2];
          return '<div class="card ' + c + '"><h2>' + t + '</h2><div class="val">' + v + '</div></div>';
        }).join('');
        document.getElementById('legs').innerHTML = (d.legs || []).map(function (m) {
          return '<tr><td>' + m.name + '</td><td>' + m.q_rad + '</td><td>' + m.dq_rad_s +
            '</td><td>' + m.tau_est_nm + '</td><td>' + m.temperature_c + '</td><td>' + m.lost +
            '</td><td>' + motorHealthBadge(m, balanceC, crouchC) + '</td></tr>';
        }).join('');
        refreshEventLog();
      } catch (e) {
        document.getElementById('conn').textContent = 'Errore fetch: ' + e;
      }
    }
    tick();
    setInterval(tick, POLL_MS);
  </script>
</body>
</html>
"""


def create_motor_health_app() -> Flask:
    app = Flask(__name__)
    poll_ms = int(os.environ.get("GO2_MOTOR_HEALTH_UI_POLL_MS", "2000"))
    store = get_lowstate_store()
    thermal = attach_thermal_protector(store.snapshot) if attach_thermal_protector else None

    @app.route("/")
    def index() -> str:
        return render_template_string(_INDEX_HTML, poll_ms=poll_ms)

    @app.route("/api/health", methods=["GET"])
    def api_health() -> Any:
        return jsonify({"ok": True, "service": "go2_motor_health"})

    @app.route("/api/motor/state", methods=["GET"])
    def api_motor_state() -> Any:
        return jsonify(store.snapshot())

    @app.route("/api/motor/thermal/status", methods=["GET"])
    def api_motor_thermal_status() -> Any:
        if thermal is None:
            return jsonify({"ok": True, "enabled": False, "reason": "thermal module unavailable"})
        st = thermal.status()
        return jsonify({"ok": True, **st})

    @app.route("/api/motor/thermal/settings", methods=["GET", "POST"])
    def api_motor_thermal_settings() -> Any:
        if thermal is None and get_thermal_settings is None:
            return jsonify({"ok": False, "reason": "thermal module unavailable"}), 503
        if request.method == "GET":
            settings = get_thermal_settings() if get_thermal_settings else {}
            plan = thermal.preview_weight_plan() if thermal else None
            return jsonify({"ok": True, "settings": settings, "weight_plan_preview": plan})
        body = request.get_json(silent=True) or {}
        if thermal is None:
            return jsonify({"ok": False, "reason": "thermal module unavailable"}), 503
        settings = thermal.apply_settings(body)
        return jsonify({"ok": True, "settings": settings})

    @app.route("/api/motor/events", methods=["GET"])
    def api_motor_events() -> Any:
        limit = int(request.args.get("limit", "80"))
        return jsonify({"ok": True, "events": get_motor_events(limit=limit), "poll_ms": poll_ms})

    @app.route("/api/motor/thermal/balance_now", methods=["POST"])
    def api_motor_thermal_balance_now() -> Any:
        if thermal is None:
            return jsonify({"ok": False, "reason": "thermal module unavailable"}), 503
        result = thermal.run_balance_now()
        status = 200 if result.get("ok") else 409
        return jsonify(result), status

    @app.route("/api/motor/thermal/recovery_now", methods=["POST"])
    def api_motor_thermal_recovery_now() -> Any:
        if thermal is None:
            return jsonify({"ok": False, "reason": "thermal module unavailable"}), 503
        result = thermal.run_crouch_recovery_now()
        status = 200 if result.get("ok") else 409
        return jsonify(result), status

    @app.route("/api/motor/sport/last", methods=["GET"])
    def api_motor_sport_last() -> Any:
        return jsonify({"ok": True, **last_manual_sport_status()})

    @app.route("/api/motor/sport/dds_ping", methods=["POST"])
    def api_motor_sport_dds_ping() -> Any:
        result = invoke_dds_sport_ping()
        status = 200 if result.get("ok") else 502
        return jsonify(result), status

    @app.route("/api/motor/sport/stand", methods=["POST"])
    def api_motor_sport_stand() -> Any:
        result = invoke_sport_pose("stand_up")
        status = 200 if result.get("ok") else 502
        return jsonify(result), status

    @app.route("/api/motor/sport/crouch", methods=["POST"])
    def api_motor_sport_crouch() -> Any:
        pre = True
        body = request.get_json(silent=True) or {}
        if body.get("pre_balance") is False:
            pre = False
        result = invoke_sport_pose("crouch", pre_balance_crouch=pre)
        status = 200 if result.get("ok") else 502
        return jsonify(result), status

    @app.route("/api/motor/stream", methods=["GET"])
    def api_motor_stream() -> Response:
        """Server-Sent Events — un JSON per messaggio lowstate (~500 Hz max lato robot)."""

        def generate():
            last_count = -1
            while True:
                snap = store.snapshot()
                count = int(snap.get("message_count") or 0)
                if count != last_count and snap.get("ok"):
                    last_count = count
                    yield f"data: {json.dumps(snap, separators=(',', ':'))}\n\n"
                time.sleep(0.05)

        return Response(generate(), mimetype="text/event-stream")

    return app
