/* MEMEBOT – dashboard web (DEMO ONLY). Solo lettura dei dati del bot simulato; nessuna transazione reale. */
(function () {
  "use strict";
  var KEY = "memebot.token";
  var tab = "dashboard";
  var lastEventId = 0;
  var events = [];
  var timer = null;
  var cache = { positions: [], history: [] };

  function $(id) { return document.getElementById(id); }
  function token() { return localStorage.getItem(KEY) || ""; }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function sol(v, d) { return v == null || isNaN(v) ? "–" : Number(v).toFixed(d == null ? 4 : d) + " SOL"; }
  function signed(v, d) { if (v == null || isNaN(v)) return "–"; var n = Number(v); return (n >= 0 ? "+" : "") + n.toFixed(d == null ? 4 : d); }
  function pct(v) { return v == null || isNaN(v) ? "–" : signed(v, 1) + "%"; }
  function cls(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }
  function time(ts) {
    if (!ts) return "";
    var d = new Date(ts);
    return d.toLocaleString("it-IT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Rome" });
  }
  function short(a) { return a ? a.slice(0, 4) + "…" + a.slice(-4) : ""; }

  var ORIGIN = { sniper: "Sniper", copy: "Copy", manual: "Manuale" };
  var REASONS = {
    stop_loss: "Stop loss", take_profit: "Take profit", trailing_stop: "Trailing stop", max_hold_time: "Tempo massimo",
    copy_sell: "Vendita del wallet copiato", partial_take_profit: "Presa di profitto a scalino",
    runner_trailing: "Calo dal massimo (resto)", mcap_drop: "Calo market cap", volume_drop: "Calo del volume",
    liquidity_drop: "Calo della liquidità", illiquid: "Pool senza liquidità", rug_illiquido: "Rug / illiquido (chiusa a 0)",
    uscita_forzata: "Uscita forzata", loss_timeout: "In perdita troppo a lungo", stagnant: "Ferma troppo a lungo",
    rotation: "Rotazione capitale", manual: "Vendita manuale"
  };

  function api(path, opts) {
    opts = opts || {};
    var headers = { "X-API-Key": token() };
    if (opts.body) headers["Content-Type"] = "application/json";
    return fetch(path, { method: opts.method || "GET", headers: headers, body: opts.body ? JSON.stringify(opts.body) : undefined, cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { var e = new Error("auth"); e.auth = true; throw e; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  // ------------------------------------------------------------- login
  function showLogin(msg) {
    stop();
    $("login").classList.remove("hidden");
    $("main").classList.add("hidden");
    $("tabbar").classList.add("hidden");
    $("login-error").textContent = msg || "";
  }
  function showMain() {
    $("login").classList.add("hidden");
    $("main").classList.remove("hidden");
    $("tabbar").classList.remove("hidden");
    start();
  }
  $("token-save").addEventListener("click", function () {
    var t = $("token-input").value.trim();
    localStorage.setItem(KEY, t);
    $("login-error").textContent = "Verifica in corso…";
    api("/api/portfolio").then(function () { $("token-input").value = ""; showMain(); })
      .catch(function (e) { showLogin(e.auth ? "Token non valido." : "Bot non raggiungibile, riprova tra poco (il server gratuito può impiegare 1 minuto a svegliarsi)."); });
  });
  $("logout").addEventListener("click", function () { localStorage.removeItem(KEY); events = []; lastEventId = 0; showLogin(""); });

  // ------------------------------------------------------------- tabs
  Array.prototype.forEach.call(document.querySelectorAll(".tabbar button"), function (b) {
    b.addEventListener("click", function () {
      tab = b.getAttribute("data-tab");
      Array.prototype.forEach.call(document.querySelectorAll(".tabbar button"), function (x) { x.classList.toggle("active", x === b); });
      Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (s) { s.classList.toggle("hidden", s.id !== "tab-" + tab); });
      window.scrollTo(0, 0);
      refresh();
    });
  });

  // ------------------------------------------------------------- rendering
  function renderPortfolio(p) {
    $("d-equity").textContent = sol(p.equity_sol, 3);
    var r = $("d-return"); r.textContent = pct(p.return_pct) + " su " + Number(p.initial_capital_sol).toFixed(2) + " SOL"; r.className = "pill " + cls(p.return_pct);
    $("d-cash").textContent = sol(p.cash_sol, 3);
    $("d-open").textContent = sol(p.open_value_sol, 3);
    setVal("d-real", p.realized_pnl_sol); setVal("d-unreal", p.unrealized_pnl_sol); setVal("d-daily", p.daily_pnl_sol);
    $("d-fees").textContent = sol(p.fees_paid_sol, 3);
    $("d-nopen").textContent = p.open_positions;
    $("d-nclosed").textContent = p.closed_positions + (p.voided_positions ? " (+" + p.voided_positions + " annullate)" : "");
  }
  function setVal(id, v) { var e = $(id); e.textContent = signed(v, 4) + " SOL"; e.className = cls(v); }

  function renderChart(curve) {
    var svg = $("chart");
    if (!curve || curve.length < 2) { svg.innerHTML = ""; return; }
    var ys = curve.map(function (c) { return c.equity_sol; });
    var min = Math.min.apply(null, ys), max = Math.max.apply(null, ys);
    if (max - min < 1e-9) { max += 0.01; min -= 0.01; }
    var pts = ys.map(function (y, i) { return (i / (ys.length - 1) * 300).toFixed(1) + "," + (76 - (y - min) / (max - min) * 72).toFixed(1); });
    var up = ys[ys.length - 1] >= ys[0];
    var col = up ? "#39ff88" : "#ff4d6d";
    svg.innerHTML = '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + col + '" stop-opacity=".35"/>' +
      '<stop offset="1" stop-color="' + col + '" stop-opacity="0"/></linearGradient></defs>' +
      '<polygon fill="url(#g)" points="0,80 ' + pts.join(" ") + ' 300,80"/>' +
      '<polyline fill="none" stroke="' + col + '" stroke-width="2" vector-effect="non-scaling-stroke" points="' + pts.join(" ") + '"/>';
  }

  function tags(p) {
    var t = '<span class="tag ' + esc(p.origin) + '">' + esc(ORIGIN[p.origin] || p.origin) + "</span>";
    if (p.copy_wallet) t += '<span class="tag">' + esc(short(p.copy_wallet)) + "</span>";
    if (p.tp_steps_done) t += '<span class="tag pos">scalini ' + p.tp_steps_done + "</span>";
    if (p.voided) t += '<span class="tag void">annullata</span>';
    return t;
  }
  function name(p) { return esc(p.symbol || (p.mint || "").slice(0, 6) + "…"); }

  function renderPositions(list) {
    cache.positions = list;
    $("positions-list").innerHTML = list.length ? list.map(function (p, i) {
      return '<div class="item" data-kind="positions" data-i="' + i + '"><div class="l"><div class="sym">' + name(p) + "</div>" + tags(p) +
        '</div><div class="r"><div>' + sol(p.value_sol, 4) + '</div><div class="' + cls(p.pnl_pct) + '">' + pct(p.pnl_pct) + "</div></div></div>";
    }).join("") : '<div class="empty">Nessuna posizione aperta</div>';
  }
  function renderHistory(list) {
    cache.history = list;
    $("history-list").innerHTML = list.length ? list.map(function (p, i) {
      return '<div class="item" data-kind="history" data-i="' + i + '"><div class="l"><div class="sym">' + name(p) + "</div>" + tags(p) +
        '<div class="muted small">' + esc(REASONS[p.close_reason] || p.close_reason || "") + " · " + time(p.closed_at) + "</div>" +
        '</div><div class="r"><div class="' + (p.voided ? "muted" : cls(p.realized_pnl_sol)) + '">' + signed(p.realized_pnl_sol, 4) +
        '</div><div class="muted small">' + pct(p.pnl_pct) + "</div></div></div>";
    }).join("") : '<div class="empty">Nessuna operazione chiusa</div>';
  }
  function renderEvents() {
    $("events-list").innerHTML = events.length ? events.slice().reverse().map(function (e) {
      return '<div class="item ev"><span class="ts">' + time(e.ts) + '</span><div class="t">' + esc(e.title) + '</div><div class="b">' + esc(e.body || "") + "</div></div>";
    }).join("") : '<div class="empty">In attesa di eventi…</div>';
  }
  function renderWallets(list) {
    $("wallets-list").innerHTML = list.length ? list.map(function (w) {
      return '<div class="item"><div class="l"><div class="sym">' + esc(w.label || short(w.address)) + "</div>" +
        '<span class="tag ' + (w.enabled ? "sniper" : "") + '">' + (w.enabled ? "attivo" : "disattivato") + "</span>" +
        '<span class="tag">' + esc(short(w.address)) + "</span>" +
        (w.unsellable_count ? '<span class="tag void">invendibili ' + w.unsellable_count + "</span>" : "") +
        (w.last_error ? '<div class="muted small">' + esc(w.last_error) + "</div>" : "") +
        '</div><div class="r"><div class="' + cls(w.total_pnl_sol) + '">' + signed(w.total_pnl_sol, 4) + '</div><div class="muted small">' +
        (w.closed_positions || 0) + " chiuse · " + (w.win_rate == null ? "–" : Math.round(w.win_rate) + "% vinte") + "</div></div></div>";
    }).join("") : '<div class="empty">Nessun wallet monitorato</div>';
  }

  function kv(k, v) { return '<div class="kv"><span class="muted">' + esc(k) + "</span><span>" + v + "</span></div>"; }
  function openDetail(p) {
    var open = p.status === "open";
    var h = "<h2>" + name(p) + "</h2>" + tags(p) + '<div style="height:8px"></div>';
    h += kv("Origine", esc(ORIGIN[p.origin] || p.origin));
    if (p.copy_wallet) h += kv("Wallet copiato", esc(short(p.copy_wallet)));
    h += kv("Investito", sol(p.total_cost_sol, 4));
    if (open) { h += kv("Valore attuale", sol(p.value_sol, 4)); h += kv("P&L non realizzato", '<span class="' + cls(p.unrealized_pnl_sol) + '">' + signed(p.unrealized_pnl_sol, 4) + " SOL (" + pct(p.pnl_pct) + ")</span>"); }
    h += kv("P&L realizzato", '<span class="' + cls(p.realized_pnl_sol) + '">' + signed(p.realized_pnl_sol, 4) + " SOL</span>");
    h += kv("Incassato", sol(p.proceeds_sol, 4));
    h += kv("Scalini di profitto", p.tp_steps_done || 0);
    h += kv("Prezzo medio d'ingresso", p.avg_entry_price_sol ? Number(p.avg_entry_price_sol).toExponential(3) + " SOL" : "–");
    h += kv("Prezzo massimo", p.peak_price_sol ? Number(p.peak_price_sol).toExponential(3) + " SOL" : "–");
    if (p.stop_loss_pct != null) h += kv("Stop loss", "-" + p.stop_loss_pct + "%");
    h += kv("Aperta", time(p.opened_at));
    if (!open) { h += kv("Chiusa", time(p.closed_at)); h += kv("Motivo", esc(REASONS[p.close_reason] || p.close_reason || "")); }
    if (p.voided) h += kv("Annullata", esc(p.void_reason || "dato di prezzo non realistico"));
    h += kv("Token", '<span class="small">' + esc(p.mint) + "</span>");
    h += '<p><a class="link" target="_blank" rel="noopener" href="https://dexscreener.com/solana/' + encodeURIComponent(p.mint) + '">Apri su Dexscreener ↗</a></p>';
    h += '<button class="btn ghost" id="sheet-close">Chiudi</button>';
    $("sheet-body").innerHTML = h;
    $("sheet").classList.remove("hidden");
    $("sheet-close").addEventListener("click", closeDetail);
  }
  function closeDetail() { $("sheet").classList.add("hidden"); }
  $("sheet").addEventListener("click", function (e) { if (e.target === $("sheet")) closeDetail(); });
  ["positions-list", "history-list"].forEach(function (id) {
    $(id).addEventListener("click", function (e) {
      var it = e.target.closest(".item"); if (!it) return;
      var p = cache[it.getAttribute("data-kind")][Number(it.getAttribute("data-i"))]; if (p) openDetail(p);
    });
  });

  // ------------------------------------------------------------- interruttori (solo simulazione)
  function bindSwitch(id, section) {
    $(id).addEventListener("change", function () {
      var el = $(id); el.disabled = true;
      api("/api/settings/" + section, { method: "PUT", body: { enabled: el.checked } })
        .catch(function () { el.checked = !el.checked; }).then(function () { el.disabled = false; });
    });
  }
  bindSwitch("sw-sniper", "sniper");
  bindSwitch("sw-copy", "copy");

  // ------------------------------------------------------------- aggiornamento (polling ogni 5 s)
  var tick = 0;
  function onErr(e) { if (e && e.auth) showLogin("Token non valido o cambiato. Inseriscilo di nuovo."); else $("d-status").textContent = "Bot non raggiungibile, nuovo tentativo tra 5 secondi…"; }
  function pollEvents() {
    var q = lastEventId ? "/api/events?limit=100&after_id=" + lastEventId : "/api/events?limit=60";
    return api(q).then(function (list) {
      list.forEach(function (e) { if (e.id > lastEventId) { lastEventId = e.id; events.push(e); } });
      if (events.length > 200) events = events.slice(-200);
      if (tab === "events") renderEvents();
    });
  }
  function refresh() {
    if (!token() && $("main").classList.contains("hidden")) return;
    var jobs = [api("/api/portfolio").then(renderPortfolio), pollEvents()];
    if (tab === "dashboard") {
      if (tick % 6 === 0) jobs.push(api("/api/analytics").then(function (a) { renderChart(a.equity_curve); }));
      jobs.push(api("/api/settings").then(function (s) {
        if (!$("sw-sniper").disabled) $("sw-sniper").checked = !!(s.sniper && s.sniper.enabled);
        if (!$("sw-copy").disabled) $("sw-copy").checked = !!(s.copy && s.copy.enabled);
      }));
      jobs.push(api("/api/health").then(function (h) {
        $("d-status").textContent = "Bot: " + (h.status === "ok" ? "online" : h.status) + " · motore " + h.engine + " · scanner " + h.scanner +
          (h.sol_usd && h.sol_usd.price ? " · SOL $" + Number(h.sol_usd.price).toFixed(2) : "") + " · aggiornato " + new Date().toLocaleTimeString("it-IT");
      }));
    }
    if (tab === "positions") jobs.push(api("/api/positions?status=open").then(renderPositions));
    if (tab === "history") jobs.push(api("/api/history?limit=200").then(renderHistory));
    if (tab === "events") renderEvents();
    if (tab === "wallets") jobs.push(api("/api/copy/wallets").then(renderWallets));
    tick++;
    return Promise.all(jobs).catch(onErr);
  }
  function start() { stop(); refresh(); timer = setInterval(function () { if (!document.hidden) refresh(); }, 5000); }
  function stop() { if (timer) clearInterval(timer); timer = null; }
  document.addEventListener("visibilitychange", function () { if (!document.hidden && timer) refresh(); });

  // ------------------------------------------------------------- avvio: il token resta solo su questo telefono
  if (token()) {
    api("/api/portfolio").then(showMain).catch(function (e) { if (e.auth) showLogin("Token non valido."); else showMain(); });
  } else {
    // server senza token configurato: entra direttamente
    fetch("/api/portfolio", { cache: "no-store" }).then(function (r) { if (r.ok) showMain(); else showLogin(""); }).catch(function () { showLogin(""); });
  }
})();
