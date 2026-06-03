/* Shared D1 teach: API, health, TCP jog modal */
window.D1 = window.D1 || {};

D1.api = function (path) {
  const base = (document.querySelector('base') && document.querySelector('base').href) || '';
  return (base.replace(/\/$/, '') || '') + path;
};

D1.apiJson = async function (path, opts) {
  const o = opts || {};
  const headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
  const init = Object.assign({ method: 'GET' }, o, { headers: headers });
  if (init.body && typeof init.body === 'object') {
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(D1.api(path), init);
  const text = await res.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_) {
      throw new Error('Risposta non JSON (HTTP ' + res.status + ')');
    }
  }
  return { res: res, data: data };
};

D1.pollHealth = async function (badgeId) {
  const id = badgeId || 'healthBadge';
  try {
    const res = await fetch(D1.api('/api/health'));
    const data = await res.json();
    const b = document.getElementById(id);
    if (!b) return;
    b.textContent = data.ok ? 'Robot OK' : 'Errore SDK';
    b.className = 'badge ' + (data.ok ? 'ok' : 'err');
  } catch (_) {
    const b = document.getElementById(id);
    if (b) {
      b.textContent = 'Offline';
      b.className = 'badge err';
    }
  }
};

D1.formatTcp = function (pose) {
  if (!pose || !pose.xyz_m) return 'TCP —';
  const p = pose.xyz_m;
  return (
    'X <span>' + (p[0] * 1000).toFixed(1) + '</span>  Y <span>' +
    (p[1] * 1000).toFixed(1) + '</span>  Z <span>' + (p[2] * 1000).toFixed(1) + '</span> mm'
  );
};

D1.initTcpJogModal = function (opts) {
  const o = opts || {};
  const overlay = document.getElementById('tcpJogModal');
  const btnOpen = document.getElementById('btnTcpJog');
  const btnClose = document.getElementById('tcpModalClose');
  if (!overlay) return;

  let _cartHolding = false;
  let _cartesianActive = false;
  let _statusTimer = 0;
  let _commandBusy = false;
  const KEY_MAP = {
    ArrowUp: { axis: 'x', sign: 1 },
    ArrowDown: { axis: 'x', sign: -1 },
    ArrowLeft: { axis: 'y', sign: 1 },
    ArrowRight: { axis: 'y', sign: -1 },
    PageUp: { axis: 'z', sign: 1 },
    PageDown: { axis: 'z', sign: -1 },
  };

  function setCartStatus(msg, ok) {
    const el = document.getElementById('cartStatus');
    if (!el) return;
    el.textContent = msg;
    el.style.color = ok === true ? 'var(--ok)' : (ok === false ? 'var(--danger)' : '');
  }

  function motionParams() {
    return {
      velocity_pct: parseFloat(document.getElementById('speedPct').value) || 25,
      accel_mm_s2: parseFloat(document.getElementById('accelMm').value) || 120,
      decel_mm_s2: parseFloat(document.getElementById('decelMm').value) || 150,
    };
  }

  function isBlocked() {
    return o.isBlocked && o.isBlocked();
  }

  function refreshTcp() {
    fetch(D1.api('/api/cartesian/pose?_=' + Date.now()))
      .then((r) => r.json())
      .then((d) => {
        if (d.ok) {
          const el = document.getElementById('tcpReadout');
          if (el) el.innerHTML = D1.formatTcp(d);
        }
      })
      .catch(() => {});
  }

  async function cartesianJogStart(axis, sign) {
    if (_commandBusy || isBlocked()) return;
    if (o.requireSynced && !o.requireSynced()) {
      setCartStatus('Prima «Leggi da robot»', false);
      return;
    }
    _commandBusy = true;
    _cartesianActive = true;
    if (o.onCartesianStart) o.onCartesianStart();
    try {
      const body = Object.assign({ axis: axis, sign: sign }, motionParams());
      if (o.getServoDeg) {
        const sd = o.getServoDeg();
        if (sd && sd.length >= 6) body.servo_deg = sd;
      }
      const { data } = await D1.apiJson('/api/cartesian/jog_start', { method: 'POST', body: body });
      setCartStatus(data.ok ? ('→ ' + axis.toUpperCase() + (sign > 0 ? '+' : '−')) : (data.reason || '?'), data.ok);
      if (!data.ok) {
        _cartHolding = false;
        _cartesianActive = false;
        _commandBusy = false;
        if (o.onCartesianEnd) o.onCartesianEnd();
      }
    } catch (e) {
      setCartStatus(String(e), false);
      _cartHolding = false;
      _cartesianActive = false;
      _commandBusy = false;
      if (o.onCartesianEnd) o.onCartesianEnd();
    }
  }

    async function cartesianJogStop() {
      try {
        await D1.apiJson('/api/cartesian/jog_stop', { method: 'POST', body: { hold_after: false } });
        setCartStatus('Fermo', null);
        try {
          const st = await D1.apiJson('/api/cartesian/jog_status');
          const body = {};
          if (st.data && st.data.servo_deg) body.servo_deg = st.data.servo_deg;
          await D1.apiJson('/api/arm/maintain', { method: 'POST', body: body });
        } catch (_) {}
      } catch (_) {
        setCartStatus('Fermo', null);
      }
      _cartesianActive = false;
      _commandBusy = false;
      if (o.onCartesianEnd) o.onCartesianEnd();
    }

  function stopCartHold(sendStop) {
    if (sendStop !== false) cartesianJogStop();
    _cartHolding = false;
    document.querySelectorAll('#cartGrid button.arrow').forEach((b) => b.classList.remove('holding'));
    if (_statusTimer) {
      clearInterval(_statusTimer);
      _statusTimer = 0;
    }
  }

  function startCartHold(axis, sign) {
    if (_cartHolding || _commandBusy || isBlocked()) return;
    _cartHolding = true;
    document.querySelectorAll('#cartGrid button.arrow').forEach((btn) => {
      const match = btn.getAttribute('data-axis') === axis && parseFloat(btn.getAttribute('data-sign')) === sign;
      btn.classList.toggle('holding', match);
    });
    cartesianJogStart(axis, sign);
    _statusTimer = setInterval(async () => {
      try {
        const { data } = await D1.apiJson('/api/cartesian/jog_status?_=' + Date.now());
        if (data.pose && data.pose.xyz_m) {
          const el = document.getElementById('tcpReadout');
          if (el) el.innerHTML = D1.formatTcp(data.pose);
        }
        if (data.last_error) {
          setCartStatus(data.last_error, false);
          stopCartHold(false);
        }
      } catch (_) {}
    }, 1200);
  }

  document.querySelectorAll('#cartGrid button.arrow').forEach((btn) => {
    const axis = btn.getAttribute('data-axis');
    const sign = parseFloat(btn.getAttribute('data-sign'));
    const down = (e) => {
      e.preventDefault();
      if (e.pointerId != null && btn.setPointerCapture) {
        try { btn.setPointerCapture(e.pointerId); } catch (_) {}
      }
      startCartHold(axis, sign);
    };
    const up = (e) => {
      e.preventDefault();
      stopCartHold();
    };
    btn.addEventListener('mousedown', down);
    btn.addEventListener('mouseup', up);
    btn.addEventListener('mouseleave', up);
    btn.addEventListener('touchstart', down, { passive: false });
    btn.addEventListener('touchend', up);
    btn.addEventListener('touchcancel', up);
  });

  const sp = document.getElementById('speedPct');
  const spl = document.getElementById('speedPctLabel');
  if (sp && spl) {
    sp.addEventListener('input', () => {
      spl.textContent = sp.value + '%';
      if (_cartHolding) {
        D1.apiJson('/api/cartesian/jog_update', { method: 'POST', body: motionParams() }).catch(() => {});
      }
    });
  }
  ['accelMm', 'decelMm'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('change', () => {
        if (_cartHolding) {
          D1.apiJson('/api/cartesian/jog_update', { method: 'POST', body: motionParams() }).catch(() => {});
        }
      });
    }
  });

  function openModal() {
    overlay.classList.add('open');
    refreshTcp();
  }
  function closeModal() {
    stopCartHold();
    overlay.classList.remove('open');
    D1.apiJson('/api/arm/maintain', { method: 'POST', body: {} }).catch(() => {});
  }

  if (btnOpen) btnOpen.addEventListener('click', openModal);
  if (btnClose) btnClose.addEventListener('click', closeModal);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeModal();
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay.classList.contains('open')) closeModal();
    if (!overlay.classList.contains('open')) return;
    if (e.target && e.target.tagName === 'INPUT') return;
    if (isBlocked()) return;
    const m = KEY_MAP[e.key];
    if (!m || e.repeat) return;
    e.preventDefault();
    startCartHold(m.axis, m.sign);
  });
  window.addEventListener('keyup', (e) => {
    if (KEY_MAP[e.key] && overlay.classList.contains('open')) stopCartHold();
  });

  return { open: openModal, close: closeModal, stop: stopCartHold, refreshTcp: refreshTcp };
};
