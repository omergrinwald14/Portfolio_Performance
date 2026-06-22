from flask import Flask, request, jsonify, render_template_string, render_template
from database import init_db, migrate_db
from twr import calculate_twr
from parsers import (
    parse_ibkr_positions,
    parse_ibkr_transactions,
    parse_ibi_transactions,
    parse_tase_holdings,
    parse_tase_eod
)


app = Flask(__name__)
init_db()
migrate_db()

# ---------------------------------------------------------------------------
# Auto-classify a file by sniffing its content
# Returns one of: 'ibkr-positions' | 'ibkr-transactions' | 'ibi-transactions'
#                 'tase-eod' | None
# ---------------------------------------------------------------------------
def classify_file(text: str, filename: str) -> str | None:
    sample = text[:800]
    fname  = filename.lower()

    if "שער נעילה" in sample:
        return "tase-eod"
    if "תאריך" in sample and "סוג" in sample:
        return "ibi-transactions"
    if "ReportDate" in sample and "PositionValue" in sample:
        return "ibkr-positions"
    if "Transaction History" in sample:
        return "ibkr-transactions"
    if "ticker" in sample and "from_date" in sample:
        return "tase-holdings"

    # Filename fallbacks
    if "position" in fname or "portfolio" in fname:
        return "ibkr-positions"
    if "eod" in fname or "history" in fname:
        return "tase-eod"
    if any(x in fname for x in ["ibi", "broker2", "hebrew"]):
        return "ibi-transactions"
    if any(x in fname for x in ["transaction", "activity"]):
        return "ibkr-transactions"
    if "holdings" in fname:
        return "tase-holdings"

    return None


LABEL = {
    "ibkr-positions":    "IBKR Positions",
    "ibkr-transactions": "IBKR Transactions",
    "ibi-transactions":  "IBI Transactions",
    "tase-eod":          "TASE EOD Prices",
}

# ---------------------------------------------------------------------------
# Upload page — single drop zone, auto-sorts files
# ---------------------------------------------------------------------------
UPLOAD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>TWR App</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0f1117; color: #e2e8f0;
           font-family: 'Segoe UI', sans-serif; font-size: 14px; padding: 40px; }
    h1  { font-size: 22px; color: #4ade80; margin-bottom: 4px; }
    .sub { color: #64748b; margin-bottom: 32px; font-size: 13px; }

    /* Drop zone */
    .dropzone {
      border: 2px dashed #2e3450; border-radius: 12px;
      padding: 40px 24px; text-align: center; cursor: pointer;
      transition: all .2s; background: #1e2230; max-width: 860px;
      margin-bottom: 28px;
    }
    .dropzone.drag { border-color: #4ade80; background: rgba(74,222,128,.06); }
    .dropzone .ico { font-size: 36px; margin-bottom: 12px; }
    .dropzone .lbl { font-size: 15px; font-weight: 600; color: #e2e8f0; margin-bottom: 6px; }
    .dropzone .hint { font-size: 12px; color: #64748b; }
    #file-input { display: none; }
    .browse-btn {
      display: inline-block; margin-top: 14px;
      background: #4ade80; color: #000; border: none;
      border-radius: 6px; padding: 8px 20px; font-weight: 700;
      font-size: 13px; cursor: pointer;
    }
    .browse-btn:hover { background: #86efac; }

    /* File results list */
    .file-list { max-width: 860px; display: flex; flex-direction: column; gap: 8px;
                  margin-bottom: 28px; }
    .file-row {
      background: #1e2230; border: 1px solid #2e3450; border-radius: 8px;
      padding: 10px 16px; display: grid;
      grid-template-columns: 220px 160px 1fr auto;
      align-items: center; gap: 12px; font-size: 13px;
    }
    .file-row .fname { color: #e2e8f0; font-weight: 500;
                        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .type-badge {
      font-size: 10px; font-weight: 700; letter-spacing: .5px;
      text-transform: uppercase; padding: 3px 9px; border-radius: 10px;
    }
    .badge-ibkr-pos  { background: rgba(96,165,250,.15); color: #60a5fa; }
    .badge-ibkr-tx   { background: rgba(96,165,250,.15); color: #60a5fa; }
    .badge-ibi-tx    { background: rgba(74,222,128,.15);  color: #4ade80; }
    .badge-tase      { background: rgba(251,191,36,.15);  color: #fbbf24; }
    .badge-unknown   { background: rgba(248,113,113,.15); color: #f87171; }
    .file-row .msg   { font-family: monospace; font-size: 12px; }
    .ok  { color: #4ade80; }
    .err { color: #f87171; }
    .pending { color: #64748b; }
    .remove-btn {
      background: none; border: none; color: #64748b;
      font-size: 16px; cursor: pointer; padding: 0 4px;
    }
    .remove-btn:hover { color: #f87171; }

    /* Status */
    hr { border: none; border-top: 1px solid #2e3450; margin: 28px 0; max-width: 860px; }
    .status-row { font-family: monospace; font-size: 12px; color: #64748b; max-width: 860px; }
    .status-row b { color: #e2e8f0; }
    .refresh-btn {
      margin-top: 10px; background: #1e2230; color: #94a3b8;
      border: 1px solid #2e3450; border-radius: 6px;
      padding: 6px 14px; font-size: 12px; cursor: pointer;
    }
    .refresh-btn:hover { border-color: #94a3b8; }

    .upload-all-btn {
      background: #4ade80; color: #000; border: none; border-radius: 6px;
      padding: 9px 24px; font-weight: 700; font-size: 13px; cursor: pointer;
      margin-bottom: 16px;
    }
    .upload-all-btn:hover { background: #86efac; }
    .upload-all-btn:disabled { background: #2e3450; color: #64748b; cursor: default; }
  </style>
</head>
<body>
  <h1>TWR Portfolio App</h1>
  <p class="sub">Drop any files — auto-sorted by type and uploaded to the database</p>

  <!-- Drop zone -->
  <div class="dropzone" id="dropzone"
       ondragover="onDragOver(event)" ondragleave="onDragLeave()"
       ondrop="onDrop(event)" onclick="document.getElementById('file-input').click()">
    <div class="ico">📂</div>
    <div class="lbl">Drop files here</div>
    <div class="hint">
      IBKR positions · IBKR transactions · IBI transactions · TASE EOD prices<br>
      Multiple files at once · auto-detected by content
    </div>
    <button class="browse-btn" onclick="event.stopPropagation();
            document.getElementById('file-input').click()">Browse files</button>
  </div>
  <input type="file" id="file-input" multiple accept=".csv"
         onchange="addFiles(this.files)">

  <!-- Queue -->
  <div id="file-list" class="file-list"></div>
  <button class="upload-all-btn" id="upload-btn"
          onclick="uploadAll()" disabled>⚡ Upload All</button>

  <hr>
  <div class="status-row" id="db-status">Loading…</div>
  <button class="refresh-btn" onclick="loadStatus()">↻ Refresh status</button>
  <button class="refresh-btn" id="reset-btn"
          style="color:#f87171;border-color:#f87171;margin-left:8px"
          onclick="resetDB()">🗑 Reset DB</button>
  <button class="refresh-btn" id="build-btn"
        style="color:#4ade80;border-color:#4ade80;margin-left:8px"
        onclick="buildPortfolio()">⚡ Build Portfolio Table</button>        

  <script>
    const BADGE_CLASS = {
      'ibkr-positions':    'badge-ibkr-pos',
      'ibkr-transactions': 'badge-ibkr-tx',
      'ibi-transactions':  'badge-ibi-tx',
      'tase-eod':          'badge-tase',
      'tase-holdings':    'badge-tase',
    };
    const BADGE_LABEL = {
      'ibkr-positions':    'IBKR Positions',
      'ibkr-transactions': 'IBKR Transactions',
      'ibi-transactions':  'IBI Transactions',
      'tase-eod':          'TASE EOD Prices',
      'tase-holdings':    'TASE Holdings',
    };

    // queue: [{file, type, rowId}]
    let queue = [];
    let rowCounter = 0;

    function onDragOver(e) {
      e.preventDefault();
      document.getElementById('dropzone').classList.add('drag');
    }
    function onDragLeave() {
      document.getElementById('dropzone').classList.remove('drag');
    }
    function onDrop(e) {
      e.preventDefault();
      document.getElementById('dropzone').classList.remove('drag');
      addFiles(e.dataTransfer.files);
    }

    function addFiles(fileList) {
      Array.from(fileList).forEach(file => {
        const reader = new FileReader();
        reader.onload = ev => {
          const text = ev.target.result;
          const type = classifyClient(text, file.name);
          const id   = 'row-' + (rowCounter++);
          queue.push({ file, type, id });
          renderRow(file.name, type, id, 'pending', '');
          document.getElementById('upload-btn').disabled = false;
        };
        reader.readAsText(file);
      });
    }

    // Client-side classifier (mirrors server logic)
    function classifyClient(text, filename) {
      const s = text.slice(0, 800);
      const f = filename.toLowerCase();
      if (s.includes('שער נעילה'))                                 return 'tase-eod';
      if (s.includes('תאריך') && s.includes('סוג'))               return 'ibi-transactions';
      if (s.includes('ReportDate') && s.includes('PositionValue')) return 'ibkr-positions';
      if (s.includes('Transaction History'))                        return 'ibkr-transactions';
      if (s.includes('ticker') && s.includes('from_date'))            return 'tase-holdings';
      if (f.includes('position') || f.includes('portfolio'))       return 'ibkr-positions';
      if (f.includes('eod') || f.includes('history'))              return 'tase-eod';
      if (f.includes('ibi') || f.includes('broker2'))              return 'ibi-transactions';
      if (f.includes('transaction') || f.includes('activity'))     return 'ibkr-transactions';
      if (f.includes('holdings'))                                      return 'tase-holdings';
      return null;
    }

    function renderRow(fname, type, id, status, msg) {
      const list  = document.getElementById('file-list');
      const badge = type
        ? `<span class="type-badge ${BADGE_CLASS[type]||'badge-unknown'}">${BADGE_LABEL[type]||'Unknown'}</span>`
        : `<span class="type-badge badge-unknown">Unknown — skipped</span>`;
      const msgCls = status === 'ok' ? 'ok' : status === 'err' ? 'err' : 'pending';
      const msgTxt = msg || (status === 'pending' ? 'Queued' : '');

      const existing = document.getElementById(id);
      const html = `<div class="file-row" id="${id}">
        <div class="fname" title="${fname}">${fname}</div>
        <div>${badge}</div>
        <div class="msg ${msgCls}">${msgTxt}</div>
        <button class="remove-btn" onclick="removeRow('${id}')">×</button>
      </div>`;

      if (existing) {
        existing.outerHTML = html;
      } else {
        list.insertAdjacentHTML('beforeend', html);
      }
    }

    function removeRow(id) {
      queue = queue.filter(q => q.id !== id);
      document.getElementById(id)?.remove();
      if (!queue.length) document.getElementById('upload-btn').disabled = true;
    }

    async function uploadAll() {
      const btn = document.getElementById('upload-btn');
      btn.disabled = true;
      btn.textContent = 'Uploading…';

      for (const item of queue) {
        if (!item.type) {
          renderRow(item.file.name, null, item.id, 'err', '✗ Could not classify — skipped');
          continue;
        }
        renderRow(item.file.name, item.type, item.id, 'pending', 'Uploading…');
        const fd = new FormData();
        fd.append('file', item.file);
        try {
          const res  = await fetch('/upload/' + item.type, { method: 'POST', body: fd });
          const data = await res.json();
          if (data.error)
            renderRow(item.file.name, item.type, item.id, 'err', '✗ ' + data.error);
          else
            renderRow(item.file.name, item.type, item.id, 'ok', '✓ ' + data.message);
        } catch(e) {
          renderRow(item.file.name, item.type, item.id, 'err', '✗ ' + e.message);
        }
      }

      queue = [];
      btn.textContent = '⚡ Upload All';
      loadStatus();
    }

    async function loadStatus() {
      try {
        const res = await fetch('/status');
        const d   = await res.json();
        document.getElementById('db-status').innerHTML =
          'Positions: <b>' + d.positions + '</b> rows &nbsp;·&nbsp; ' +
          'Transactions: <b>' + d.transactions + '</b> rows &nbsp;·&nbsp; ' +
          'TASE prices: <b>' + d.tase_prices + '</b> rows';
      } catch(e) {
        document.getElementById('db-status').textContent = 'Could not load status';
      }
    }
    async function resetDB() {
      if (!confirm('Delete ALL data from the database? This cannot be undone.')) return;
      const btn = document.getElementById('reset-btn');
      btn.disabled = true;
      btn.textContent = 'Resetting…';
      try {
        const res  = await fetch('/reset', { method: 'POST' });
        const data = await res.json();
        loadStatus();
      } catch(e) {
        alert('Reset failed: ' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '🗑 Reset DB';
      }
    }
    async function buildPortfolio() {
      const btn = document.getElementById('build-btn');
      btn.disabled = true;
      btn.textContent = 'Building…';
      try {
        const res  = await fetch('/build', { method: 'POST' });
        const data = await res.json();
        alert(data.message);
      } catch(e) {
        alert('Build failed: ' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '⚡ Build Portfolio Table';
      }
    }

    loadStatus();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(UPLOAD_HTML)


def _read_file(req):
    if "file" not in req.files:
        return None, "No file provided"
    f = req.files["file"]
    if not f.filename:
        return None, "Empty filename"
    f.seek(0)    
    try:
        return f.read().decode("utf-8-sig"), None
    except UnicodeDecodeError:
        f.seek(0)
        try:
            return f.read().decode("windows-1255"), None
        except Exception as e:
            return None, str(e)


@app.route("/upload/ibkr-positions", methods=["POST"])
def upload_ibkr_positions():
    text, err = _read_file(request)
    if err:
        return jsonify({"error": err}), 400
    r = parse_ibkr_positions(text)
    return jsonify({"message": f"{r['inserted']} rows inserted, {r['skipped']} skipped", **r})


@app.route("/upload/ibkr-transactions", methods=["POST"])
def upload_ibkr_transactions():
    text, err = _read_file(request)
    if err:
        return jsonify({"error": err}), 400
    r = parse_ibkr_transactions(text)
    return jsonify({"message": f"{r['inserted']} transactions inserted, {r['skipped']} skipped", **r})


@app.route("/upload/ibi-transactions", methods=["POST"])
def upload_ibi_transactions():
    text, err = _read_file(request)
    if err:
        return jsonify({"error": err}), 400
    r = parse_ibi_transactions(text)
    return jsonify({"message": f"{r['inserted']} transactions inserted, {r['skipped']} skipped", **r})


@app.route("/upload/tase-eod", methods=["POST"])
def upload_tase_eod():
    text, err = _read_file(request)
    if err:
        return jsonify({"error": err}), 400
    r = parse_tase_eod(text)
    return jsonify({
        "message": (
            f"{r['inserted']} prices for {r.get('ticker','?')} "
            f"({r.get('name','')}) · {r['skipped']} skipped"
        ),
        **r,
    })


@app.route("/upload/tase-holdings", methods=["POST"])
def upload_tase_holdings():
    text, err = _read_file(request)
    if err:
        return jsonify({"error": err}), 400
    r = parse_tase_holdings(text)
    return jsonify({"message": f"{r['inserted']} holdings inserted, {r['skipped']} skipped", **r})


@app.route("/reset", methods=["POST"])
def reset_db():
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    for table in ["positions", "transactions", "tase_prices", "tase_holdings"]:
        c.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    return jsonify({"message": "All tables cleared."})


@app.route("/debug/positions")
def debug_positions():
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    rows = c.execute(
        "SELECT date, symbol, position_value_ils, fx_rate FROM positions ORDER BY date DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/debug/transactions")
def debug_transactions():
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    rows = c.execute(
        """SELECT date, broker, tx_type, security, amount_ils
           FROM transactions
           ORDER BY date DESC, broker, security"""
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/debug/value")
def debug_value():
    from twr import portfolio_value
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "Pass ?date=YYYY-MM-DD"}), 400
    result = portfolio_value(date)
    return jsonify(result)


@app.route("/status")
def status():
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    counts = {
        "positions":    c.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
        "transactions": c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "tase_prices":  c.execute("SELECT COUNT(*) FROM tase_prices").fetchone()[0],
    }
    conn.close()
    return jsonify(counts)

@app.route('/debug/twr')
def debug_twr():
    start_date = request.args.get('start_date', '2022-07-13')
    end_date = request.args.get('end_date', '2026-06-11')
    result = calculate_twr(start_date, end_date)
    return jsonify(result)


@app.route("/build", methods=["POST"])
def build_portfolio():
    from twr import build_daily_portfolio
    from database import get_db
    build_daily_portfolio()
    conn = get_db()
    c = conn.cursor()
    count = c.execute("SELECT COUNT(*) FROM daily_portfolio").fetchone()[0]
    conn.close()
    return jsonify({"message": f"Built {count} rows in daily_portfolio"})


@app.route("/debug/ibi-value")
def debug_ibi_value():
    from database import get_db
    date = request.args.get("date", "2023-09-11")
    conn = get_db()
    holdings = conn.execute("""
        SELECT ticker, hebrew_name, from_date, to_date, qty
        FROM tase_holdings
        WHERE from_date <= ? AND to_date >= ?
    """, (date, date)).fetchall()
    
    result = []
    for h in holdings:
        price_row = conn.execute("""
            SELECT date, price_ils FROM tase_prices
            WHERE ticker = ?
            AND date = (SELECT MAX(date) FROM tase_prices WHERE ticker = ? AND date <= ?)
        """, (h["ticker"], h["ticker"], date)).fetchone()
        result.append({
            "ticker": h["ticker"],
            "qty": h["qty"],
            "from_date": h["from_date"],
            "to_date": h["to_date"],
            "price_date": price_row["date"] if price_row else None,
            "price_ils": price_row["price_ils"] if price_row else None,
            "value": round(h["qty"] * price_row["price_ils"], 2) if price_row else 0
        })
    conn.close()
    return jsonify({"date": date, "holdings": result})


TWR_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>TWR Chart</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0f1117; color: #e2e8f0;
           font-family: 'Segoe UI', sans-serif; font-size: 14px; padding: 40px; }
    h1   { font-size: 22px; color: #4ade80; margin-bottom: 4px; }
    .sub { color: #64748b; margin-bottom: 28px; font-size: 13px; }
    .controls { display: flex; gap: 16px; align-items: flex-end; margin-bottom: 24px; flex-wrap: wrap; }
    .controls label { font-size: 12px; color: #94a3b8; display: flex; flex-direction: column; gap: 4px; }
    .controls input[type=date] {
      background: #1e2230; border: 1px solid #2e3450; color: #e2e8f0;
      border-radius: 6px; padding: 7px 10px; font-size: 13px;
    }
    .go-btn {
      background: #4ade80; color: #000; border: none; border-radius: 6px;
      padding: 8px 20px; font-weight: 700; font-size: 13px; cursor: pointer;
    }
    .go-btn:hover { background: #86efac; }
    .kpi-row { display: flex; gap: 20px; margin-bottom: 28px; flex-wrap: wrap; }
    .kpi {
      background: #1e2230; border: 1px solid #2e3450; border-radius: 10px;
      padding: 16px 24px; min-width: 160px;
    }
    .kpi .label { font-size: 11px; color: #64748b; text-transform: uppercase;
                   letter-spacing: .5px; margin-bottom: 6px; }
    .kpi .value { font-size: 26px; font-weight: 700; color: #4ade80; }
    .chart-wrap { background: #1e2230; border: 1px solid #2e3450;
                   border-radius: 12px; padding: 24px; max-width: 960px; }
    .err { color: #f87171; font-size: 13px; margin-top: 12px; }
  </style>
</head>
<body>
  <h1>Portfolio TWR</h1>
  <p class="sub">Time-Weighted Return · net of tax · ILS</p>

  <div class="controls">
    <label>Start date <input type="date" id="start" value="2022-07-13"></label>
    <label>End date   <input type="date" id="end"   value="2026-06-11"></label>
    <button class="go-btn" onclick="load()">Update</button>
  </div>

  <div class="kpi-row">
    <div class="kpi"><div class="label">Total TWR</div><div class="value" id="kpi-twr">—</div></div>
    <div class="kpi"><div class="label">CAGR</div>    <div class="value" id="kpi-cagr">—</div></div>
    <div class="kpi"><div class="label">Period</div>  <div class="value" id="kpi-days" style="font-size:18px">—</div></div>
  </div>

  <div class="chart-wrap">
    <canvas id="chart" height="320"></canvas>
    <div class="err" id="err"></div>
  </div>

  <script>
    let chartInstance = null;

    function pct(v) { return (v * 100).toFixed(2) + '%'; }

    async function load() {
      const start = document.getElementById('start').value;
      const end   = document.getElementById('end').value;
      document.getElementById('err').textContent = '';

      try {
        const res  = await fetch(`/debug/twr?start_date=${start}&end_date=${end}`);
        const data = await res.json();

        const series = data.series || [];
        if (!series.length) {
          document.getElementById('err').textContent = 'No data for selected range.';
          return;
        }

        // KPIs
        const twr   = data.twr;
        const days  = (new Date(end) - new Date(start)) / 86400000;
        const years = days / 365.25;
        const cagr  = Math.pow(1 + twr, 1 / years) - 1;

        document.getElementById('kpi-twr').textContent  = pct(twr);
        document.getElementById('kpi-cagr').textContent = pct(cagr);
        document.getElementById('kpi-days').textContent = `${Math.round(days)}d / ${years.toFixed(1)}y`;

        // Color KPIs red if negative
        ['kpi-twr','kpi-cagr'].forEach(id => {
          document.getElementById(id).style.color =
            parseFloat(document.getElementById(id).textContent) < 0 ? '#f87171' : '#4ade80';
        });

        // Chart
        const labels = series.map(r => r.date);
        const values = series.map(r => +(r.cumulative_twr * 100).toFixed(4));

        if (chartInstance) chartInstance.destroy();
        chartInstance = new Chart(document.getElementById('chart'), {
          type: 'line',
          data: {
            labels,
            datasets: [{
              label: 'Cumulative TWR (%)',
              data: values,
              borderColor: '#4ade80',
              borderWidth: 2,
              pointRadius: 0,
              fill: true,
              backgroundColor: 'rgba(74,222,128,0.07)',
              tension: 0.3,
            }]
          },
          options: {
            responsive: true,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { labels: { color: '#94a3b8' } },
              tooltip: {
                callbacks: {
                  label: ctx => ` TWR: ${ctx.parsed.y.toFixed(2)}%`
                }
              }
            },
            scales: {
              x: {
                ticks: { color: '#64748b', maxTicksLimit: 12,
                          callback: (_, i) => labels[i]?.slice(0,7) },
                grid:  { color: '#1a1f2e' }
              },
              y: {
                ticks: { color: '#64748b', callback: v => v + '%' },
                grid:  { color: '#1a1f2e' }
              }
            }
          }
        });

      } catch(e) {
        document.getElementById('err').textContent = 'Error: ' + e.message;
      }
    }

    load(); // auto-load on page open
  </script>
</body>
</html>
"""

@app.route("/twr")
def twr_page():
    return render_template_string(TWR_HTML)


@app.route("/debug/earliest")
def debug_earliest():
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    return jsonify({
        "earliest_position": c.execute("SELECT MIN(date) FROM positions").fetchone()[0],
        "earliest_transaction": c.execute("SELECT MIN(date) FROM transactions").fetchone()[0],
        "earliest_tase_price": c.execute("SELECT MIN(date) FROM tase_prices").fetchone()[0],
        "tx_dates": [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM transactions ORDER BY date LIMIT 20"
        ).fetchall()],
    })


@app.route("/debug/first-tx-value")
def debug_first_tx_value():
    from database import get_db
    conn = get_db()
    c = conn.cursor()
    date = "2022-07-13"
    nearest = c.execute(
        "SELECT MAX(date) FROM positions WHERE date <= ?", (date,)
    ).fetchone()[0]
    total = c.execute(
        "SELECT SUM(position_value_ils) FROM positions WHERE date = ?", (nearest,)
    ).fetchone()[0]
    rows = c.execute(
        "SELECT * FROM positions WHERE date = ?", (nearest,)
    ).fetchall()
    return jsonify({
        "queried_date": date,
        "nearest_position_date": nearest,
        "total_value": total,
        "rows": [dict(r) for r in rows]
    })

if __name__ == "__main__":
    app.run(debug=True, port=5000)
