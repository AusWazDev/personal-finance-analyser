"""
Report generator.

Produces:
  reports/monthly_summary.html   ← dashboard (charts rendered client-side via Plotly.js)
  reports/net_worth.html
  reports/transactions.html
  reports/transfers.html
  reports/review.html
  reports/fy_summary.html
"""

import html
import logging
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from src.utils import md_to_html as _md_to_html, CATEGORY_COLORS, EXCLUDE_FROM_SPEND as _EXCLUDE_FROM_SPEND

logger = logging.getLogger(__name__)
_INCOME_CATEGORIES = {"Income", "Board & Lodging", "Interest Income", "Business Reimbursement", "Family Loan Received"}
_TAXABLE_INCOME_CATS = {"Income", "Interest Income"}
_INCOME_EXEMPT_TAGS: dict[str, tuple[str, str, str]] = {
    "Board & Lodging":       ("exempt",     "#d1fae5", "#065f46"),
    "Business Reimbursement":("not income", "#dbeafe", "#1e40af"),
    "Family Loan Received":  ("borrowed",   "#fef3c7", "#92400e"),
}

_CAT_INCOME_GROUP = ["Board & Lodging", "Income", "Interest Income", "Business Reimbursement", "Family Loan Received"]


def _cat_optgroups(categories: list[str], selected: str = "", include_all: bool = False) -> str:
    """Return <optgroup>-grouped <option> HTML for a category <select>."""
    income = [c for c in _CAT_INCOME_GROUP if c in categories]
    expenditure = sorted([c for c in categories if c not in set(_CAT_INCOME_GROUP)])
    parts = []
    if include_all:
        parts.append('<option value="">All categories</option>')
    if income:
        parts.append('<optgroup label="Income">')
        for c in income:
            sel = ' selected' if c == selected else ''
            parts.append(f'<option value="{c}"{sel}>{c}</option>')
        parts.append('</optgroup>')
    if expenditure:
        parts.append('<optgroup label="Expenditure">')
        for c in expenditure:
            sel = ' selected' if c == selected else ''
            parts.append(f'<option value="{c}"{sel}>{c}</option>')
        parts.append('</optgroup>')
    return '\n'.join(parts)


def _help_box(title: str, intro: str, items: list[str], legend: list[tuple] | None = None) -> str:
    """Collapsible info panel injected into each page below the header."""
    items_html = "".join(f"<li>{item}</li>" for item in items)
    leg_html = ""
    if legend:
        leg_items = "".join(
            f'<span class="help-legend-item">'
            f'<span class="cnt-badge" style="background:{color};padding:2px 8px">{label}</span>'
            f' {desc}</span>'
            for label, color, desc in legend
        )
        leg_html = f'<div class="help-legend">{leg_items}</div>'
    return (
        f'<details class="help-box">'
        f'<summary>{html.escape(title)}</summary>'
        f'<div class="help-body">'
        f'<p>{intro}</p>'
        f'<ul>{items_html}</ul>'
        f'{leg_html}'
        f'</div>'
        f'</details>'
    )


# ── Import button UI (shared across all pages) ───────────────────────────────
# Defined as plain strings (not f-strings) so JS/CSS curly braces are literal.

_IMPORT_CSS = """
  .nav-actions{margin-left:auto;display:flex;gap:8px;align-items:center}
  .nav-import-btn{padding:7px 18px;background:#2A9D8F;color:white;
                  border:none;border-radius:8px;cursor:pointer;font-size:.85rem;
                  font-weight:600;white-space:nowrap}
  .nav-import-btn:hover{background:#21867a}
  .nav-import-btn:disabled{background:#555;cursor:not-allowed}
  .nav-recat-btn{padding:7px 18px;background:#E76F51;color:white;
                 border:none;border-radius:8px;cursor:pointer;font-size:.85rem;
                 font-weight:600;white-space:nowrap}
  .nav-recat-btn:hover{background:#c95f43}
  .nav-recat-btn:disabled{background:#555;cursor:not-allowed}
  .nav-refresh-btn{padding:7px 18px;background:#457B9D;color:white;
                   border:none;border-radius:8px;cursor:pointer;font-size:.85rem;
                   font-weight:600;white-space:nowrap}
  .nav-refresh-btn:hover{background:#366688}
  .nav-upload-btn{padding:7px 18px;background:#6c5ce7;color:white;
                  border:none;border-radius:8px;cursor:pointer;font-size:.85rem;
                  font-weight:600;white-space:nowrap}
  .nav-upload-btn:hover{background:#5a4dd1}
  .import-modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);
                   z-index:9999;align-items:center;justify-content:center}
  .import-modal-bg.open{display:flex}
  .import-modal-box{background:white;border-radius:14px;padding:28px 32px;width:720px;
                    max-width:95vw;display:flex;flex-direction:column;max-height:85vh;gap:0}
  .import-log{background:#1a1a2e;color:#e2e8f0;padding:16px;border-radius:8px;
              overflow-y:auto;font-size:.8rem;min-height:280px;flex:1;
              white-space:pre-wrap;font-family:monospace;margin:0}
  .btn-import-reload{padding:9px 20px;background:#2A9D8F;color:white;border:none;
                     border-radius:8px;cursor:pointer;font-weight:600}
  .btn-import-close{padding:9px 20px;background:#eee;color:#444;border:none;
                    border-radius:8px;cursor:pointer}
  .help-box{background:#e8f4f8;border-left:4px solid #457B9D;border-radius:0 8px 8px 0;margin-bottom:16px}
  .help-box summary{padding:10px 16px;cursor:pointer;font-weight:600;color:#264653;list-style:none;
                    display:flex;align-items:center;gap:8px;font-size:.88rem}
  .help-box summary::-webkit-details-marker{display:none}
  .help-box summary::before{content:'i';background:#457B9D;color:white;border-radius:50%;
                             width:18px;height:18px;display:inline-flex;align-items:center;
                             justify-content:center;font-size:.75rem;font-style:italic;
                             font-weight:700;flex-shrink:0}
  .help-box[open] summary{border-bottom:1px solid #b8d8e8}
  .help-body{padding:10px 16px 14px 36px;color:#334;font-size:.84rem}
  .help-body p{margin:0 0 6px}
  .help-body ul{margin:4px 0 0;padding-left:16px}
  .help-body li{margin-bottom:4px;line-height:1.5}
  .help-legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;align-items:center}
  .help-legend-item{display:flex;align-items:center;gap:6px;font-size:.82rem;color:#444}"""

_IMPORT_MODAL = """<div id="import-modal" class="import-modal-bg">
  <div class="import-modal-box">
    <h3 id="import-modal-title" style="margin:0 0 16px;color:#264653;font-size:1.1rem">Import Progress</h3>
    <pre id="import-log" class="import-log"></pre>
    <div style="margin-top:16px;display:flex;gap:10px;justify-content:flex-end">
      <button id="reload-btn" class="btn-import-reload" style="display:none"
              onclick="location.reload()">&#8635; Reload Page</button>
      <button id="modal-close-btn" class="btn-import-close"
              onclick="document.getElementById('import-modal').classList.remove('open')">Close</button>
    </div>
  </div>
</div>"""

_IMPORT_JS = """<script>
function _startJob(endpoint, trigBtnId, title) {
  if (window.location.protocol === 'file:') {
    alert('This feature requires the local server.\\nRun: python server.py\\nThen open: http://localhost:5100');
    return;
  }
  var titleEl = document.getElementById('import-modal-title');
  if (titleEl) titleEl.textContent = title;
  var modal = document.getElementById('import-modal');
  var log   = document.getElementById('import-log');
  var btn   = document.getElementById(trigBtnId);
  var rbtn  = document.getElementById('reload-btn');
  modal.classList.add('open');
  log.textContent = '⏳ Starting — please wait…';
  if (btn) { btn.disabled = true; btn.textContent = 'Running…'; }
  rbtn.style.display = 'none';
  var es = new EventSource(endpoint);
  es.onopen = function() { log.textContent = ''; };
  es.onmessage = function(e) {
    log.textContent += e.data + '\\n';
    log.scrollTop = log.scrollHeight;
  };
  es.addEventListener('done', function() {
    es.close();
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label; }
    rbtn.style.display = 'inline-block';
    var closebtn = document.getElementById('modal-close-btn');
    if (closebtn) closebtn.style.display = 'none';
  });
  es.onerror = function() {
    es.close();
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label; }
    if (log.textContent.indexOf('complete') === -1 && log.textContent.indexOf('failed') === -1) {
      log.textContent += '\\nConnection lost.\\n';
    }
  };
}
function startImport() { _startJob('/api/start-import', 'import-btn', 'Import Progress'); }
function startRecategorise() { _startJob('/api/recategorise', 'recat-btn', 'Re-categorise Progress'); }
function startRefresh() { _startJob('/api/refresh-reports', 'refresh-btn', 'Refreshing Charts'); }

// Upload modal (Item 1)
var _uploadFiles = [];
function openUpload() {
  document.getElementById('upload-modal').style.display = 'flex';
  document.getElementById('upload-submit-btn').style.display = 'inline-block';
  document.getElementById('upload-import-btn').style.display = 'none';
  document.getElementById('upload-list').innerHTML = '';
  document.getElementById('upload-status').textContent = '';
  _uploadFiles = [];
}
function closeUpload() { document.getElementById('upload-modal').style.display = 'none'; }
function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').style.background = '';
  handleFileSelect(e.dataTransfer.files);
}
function handleFileSelect(files) {
  var existing = new Set(_uploadFiles.map(f => f.name));
  Array.from(files).forEach(f => { if (!existing.has(f.name)) _uploadFiles.push(f); });
  var list = document.getElementById('upload-list');
  list.innerHTML = _uploadFiles.map(f =>
    '<div style="padding:3px 0;border-bottom:1px solid #f0f0f0">&#128196; ' + f.name + ' <span style="color:#999">(' + (f.size/1024).toFixed(0) + ' KB)</span></div>'
  ).join('');
}
function submitUpload() {
  if (!_uploadFiles.length) { alert('No files selected.'); return; }
  var btn = document.getElementById('upload-submit-btn');
  var status = document.getElementById('upload-status');
  btn.disabled = true; btn.textContent = 'Uploading…';
  status.textContent = ''; status.style.color = '#444';
  var fd = new FormData();
  _uploadFiles.forEach(f => fd.append('files', f));
  fetch('/api/upload', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        status.style.color = '#22a86e';
        status.textContent = d.count + ' file(s) uploaded.';
        btn.style.display = 'none';
        document.getElementById('upload-import-btn').style.display = 'inline-block';
      } else {
        status.style.color = '#E63946';
        status.textContent = 'Error: ' + (d.error || 'unknown');
        btn.disabled = false; btn.textContent = 'Upload & Queue Import';
      }
    })
    .catch(e => {
      status.style.color = '#E63946';
      status.textContent = 'Network error: ' + e;
      btn.disabled = false; btn.textContent = 'Upload & Queue Import';
    });
}
</script>"""


_UPLOAD_MODAL = """<div id="upload-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9998;align-items:center;justify-content:center"
     onclick="if(event.target===this)closeUpload()">
  <div style="background:#fff;border-radius:14px;padding:32px;width:520px;max-width:96vw">
    <h3 style="margin:0 0 8px;color:#264653;font-size:1.15rem">Upload Statement Files</h3>
    <p style="margin:0 0 20px;font-size:.85rem;color:#666">Drop statement files here. Import, categorisation and report refresh run automatically in the background.</p>
    <div id="drop-zone" style="border:2px dashed #2a9d8f;border-radius:10px;padding:36px 24px;text-align:center;cursor:pointer;transition:background .2s"
         onclick="document.getElementById('file-input').click()"
         ondragover="event.preventDefault();this.style.background='#e8f8f6'"
         ondragleave="this.style.background=''"
         ondrop="handleDrop(event)">
      <div style="font-size:2rem;margin-bottom:8px">&#128196;</div>
      <div style="color:#264653;font-weight:600">Drop files here or click to browse</div>
      <div style="font-size:.8rem;color:#888;margin-top:4px">.csv &nbsp; .pdf &nbsp; .html</div>
    </div>
    <input type="file" id="file-input" multiple accept=".csv,.pdf,.html,.CSV,.PDF,.HTML" style="display:none" onchange="handleFileSelect(this.files)">
    <div id="upload-list" style="margin-top:14px;font-size:.85rem;color:#444;max-height:140px;overflow-y:auto"></div>
    <div id="upload-status" style="margin-top:10px;font-size:.85rem;font-weight:600"></div>
    <div style="margin-top:20px;display:flex;gap:10px;justify-content:flex-end">
      <button onclick="closeUpload()" style="padding:8px 20px;border:1px solid #ccd;border-radius:8px;background:#fff;cursor:pointer;font-size:.9rem">Close</button>
      <button id="upload-submit-btn" onclick="submitUpload()" style="padding:8px 22px;background:#2a9d8f;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:600">Upload Files</button>
    </div>
  </div>
</div>"""

# ── Standard nav builder ───────────────────────────────────────────────────────

from src.utils import NAV_TABS as _NAV_TABS  # noqa: E402


def _build_nav_html(active_tab: str) -> str:
    """Return standardised site-nav + action buttons + modals + data tab bar.

    Uses absolute paths for cross-page compatibility.
    active_tab: key from _NAV_TABS, e.g. 'transactions', 'fy_summary'.
    """
    tabs = "".join(
        f'  <a href="{href}" class="tab-btn{" active" if key == active_tab else ""}">{label}</a>\n'
        for label, href, key, _mod in _NAV_TABS
    )
    data_active  = " active" if active_tab != "help" else ""
    help_active  = " active" if active_tab == "help" else ""
    return f"""<nav class="site-nav">
  <a href="/reports/monthly_summary.html" class="nav-link">Dashboard</a>
  <a href="/reports/monthly_summary.html#reports" class="nav-link">Reports</a>
  <a href="/reports/transactions.html" class="nav-link{data_active}">Data</a>
  <a href="/settings/accounts" class="nav-link">Settings</a>
  <a href="/help" class="nav-link{help_active}">Help</a>
  <div class="nav-actions">
    <button id="upload-btn" class="nav-upload-btn" onclick="openUpload()">&#8593; Upload Files</button>
  </div>
</nav>
{_IMPORT_MODAL}
{_UPLOAD_MODAL}
<div class="tab-bar">
{tabs}</div>"""


def _fy_gst_section(fy: int, gst_df: "pd.DataFrame") -> str:
    """Return the GST Claimable HTML section for a single FY."""
    header = '<h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">GST Claimable — ATO Input Tax Credits</h3>'
    if gst_df.empty:
        return (
            f'{header}'
            f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;'
            f'padding:14px 20px;font-size:.88rem;color:#166534;margin-bottom:20px">'
            f'No GST-claimable expenses flagged for FY{fy}. '
            f'Flag transactions using the <strong>GST</strong> button on the Transactions page.</div>'
        )
    total_gross = gst_df["amount"].abs().sum()
    total_gst = total_gross / 11
    rows = []
    for _, row in gst_df.iterrows():
        d = row["date"].strftime("%d %b %Y") if hasattr(row["date"], "strftime") else str(row["date"])[:10]
        gross = abs(row["amount"])
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 14px;white-space:nowrap'>{d}</td>"
            f"<td style='padding:6px 14px'>{html.escape(str(row.get('description', ''))[:60])}</td>"
            f"<td style='padding:6px 14px'>{html.escape(str(row.get('category', '')))}</td>"
            f"<td style='padding:6px 14px;color:#555;font-size:.82rem'>{html.escape(str(row.get('account', '')))}</td>"
            f"<td style='padding:6px 14px;text-align:right'>${gross:,.2f}</td>"
            f"<td style='padding:6px 14px;text-align:right;color:#059669;font-weight:600'>${gross/11:,.2f}</td>"
            f"</tr>"
        )
    body = "\n".join(rows)
    return f"""{header}
<table style="width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:20px">
  <thead><tr style="background:#059669;color:white">
    <th style="padding:8px 14px;text-align:left">Date</th>
    <th style="padding:8px 14px;text-align:left">Description</th>
    <th style="padding:8px 14px;text-align:left">Category</th>
    <th style="padding:8px 14px;text-align:left">Account</th>
    <th style="padding:8px 14px;text-align:right">Gross Amount</th>
    <th style="padding:8px 14px;text-align:right">GST (1/11th)</th>
  </tr></thead>
  <tbody>
    {body}
    <tr style="background:#f0fdf4;font-weight:700;border-top:2px solid #bbf7d0">
      <td colspan="4" style="padding:8px 14px">Total GST Claimable</td>
      <td style="padding:8px 14px;text-align:right">${total_gross:,.2f}</td>
      <td style="padding:8px 14px;text-align:right;color:#059669">${total_gst:,.2f}</td>
    </tr>
  </tbody>
</table>"""


def _fy_cg_section(fy: int, cg_data: dict) -> str:
    """Return the Capital Gains HTML section for a single FY, or a prompt if no data."""
    entry = cg_data.get(str(fy))
    header = '<h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">Capital Gains</h3>'
    if not entry:
        return (
            f'{header}'
            f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;'
            f'padding:14px 20px;font-size:.88rem;color:#166534;margin-bottom:20px">'
            f'No capital gains data recorded for FY{fy}. '
            f'<a href="/capital-gains" style="color:#166534;font-weight:600">Enter via the Capital Gains page</a> '
            f'if you had investment sales this year.</div>'
        )
    gross      = float(entry.get("gross_gains", 0) or 0)
    discount   = float(entry.get("cgt_discount", 0) or 0)
    net        = float(entry.get("net_gains", 0) or 0)
    losses     = float(entry.get("capital_losses_applied", 0) or 0)
    carried    = float(entry.get("carried_forward_losses", 0) or 0)
    notes      = str(entry.get("notes", "") or "").strip()
    net_color  = "#166534" if net >= 0 else "#991b1b"
    notes_row  = (
        f"<tr><td colspan='2' style='padding:6px 14px;color:#555;font-style:italic'>"
        f"{html.escape(notes)}</td></tr>"
        if notes else ""
    )
    return f"""{header}
<table style="width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:20px">
  <thead><tr style="background:#059669;color:white">
    <th style="padding:8px 14px;text-align:left">Item</th>
    <th style="padding:8px 14px;text-align:right">Amount</th>
  </tr></thead>
  <tbody>
    <tr><td style="padding:6px 14px">Gross Capital Gains</td><td style="padding:6px 14px;text-align:right">${gross:,.2f}</td></tr>
    <tr><td style="padding:6px 14px">CGT Discount Applied</td><td style="padding:6px 14px;text-align:right;color:#555">(${discount:,.2f})</td></tr>
    <tr><td style="padding:6px 14px">Capital Losses Applied</td><td style="padding:6px 14px;text-align:right;color:#555">(${losses:,.2f})</td></tr>
    {notes_row}
    <tr style="background:#f0fdf4;font-weight:700;border-top:2px solid #bbf7d0">
      <td style="padding:8px 14px">Net Taxable Capital Gains</td>
      <td style="padding:8px 14px;text-align:right;color:{net_color}">${net:,.2f}</td>
    </tr>
    {"" if carried == 0 else f"<tr style='background:#fff7ed'><td style='padding:6px 14px;color:#92400e'>Losses Carried Forward to Next FY</td><td style='padding:6px 14px;text-align:right;color:#92400e'>${carried:,.2f}</td></tr>"}
  </tbody>
</table>"""


def _fy_franking_section(fy: int, franking_data: dict) -> str:
    """Return the Dividends & Franking Credits HTML section for a single FY."""
    header = '<h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">Dividends &amp; Franking Credits</h3>'
    entry = (franking_data or {}).get(str(fy))
    if not entry:
        return (
            f'{header}'
            f'<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;'
            f'padding:14px 20px;font-size:.88rem;color:#0c4a6e;margin-bottom:20px">'
            f'No franking credit data for FY{fy}. '
            f'<a href="/franking-credits" style="color:#0369a1;font-weight:600">Enter via Franking Credits page</a> '
            f'if you received dividends this year.</div>'
        )
    cash      = float(entry.get("cash_dividends", 0) or 0)
    credits   = float(entry.get("franking_credits", 0) or 0)
    grossed   = round(cash + credits, 2)
    notes     = str(entry.get("notes", "") or "").strip()
    notes_row = (
        f"<tr><td colspan='2' style='padding:6px 14px;color:#555;font-style:italic'>"
        f"{html.escape(notes)}</td></tr>"
        if notes else ""
    )
    return f"""{header}
<table style="width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:20px">
  <thead><tr style="background:#0369a1;color:white">
    <th style="padding:8px 14px;text-align:left">Item</th>
    <th style="padding:8px 14px;text-align:right">Amount</th>
  </tr></thead>
  <tbody>
    <tr><td style="padding:6px 14px">Cash Dividends Received</td><td style="padding:6px 14px;text-align:right">${cash:,.2f}</td></tr>
    <tr><td style="padding:6px 14px">Franking Credits (imputed)</td><td style="padding:6px 14px;text-align:right">${credits:,.2f}</td></tr>
    {notes_row}
    <tr style="background:#eff6ff;font-weight:700;border-top:2px solid #bfdbfe">
      <td style="padding:8px 14px">Grossed-Up Dividend Income</td>
      <td style="padding:8px 14px;text-align:right;color:#1e40af">${grossed:,.2f}</td>
    </tr>
  </tbody>
</table>
<p style="font-size:.75rem;color:#64748b;margin:-14px 0 20px">
  Franking credits offset tax payable — include the grossed-up total as income and claim credits against your tax assessment.
</p>"""


def _fy_income_tax_section(
    fy: int,
    taxable_income: float,
    gst_claimable_total: float,
    tax_deductible_total: float,
) -> str:
    """Return the estimated income tax HTML section for a single FY."""
    from src.tax_estimator import estimate_income_tax
    header = '<h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">Estimated Income Tax</h3>'
    if taxable_income <= 0:
        return (
            f'{header}<div style="background:#f8fafc;border-radius:8px;padding:14px 20px;'
            f'font-size:.88rem;color:#64748b;margin-bottom:20px">No taxable income recorded for FY{fy}.</div>'
        )
    est = estimate_income_tax(taxable_income, fy)
    deductions = round(gst_claimable_total + tax_deductible_total, 2)
    adj_income = max(0.0, taxable_income - deductions)
    adj_est = estimate_income_tax(adj_income, fy) if deductions > 0 else None
    adj_row = ""
    if adj_est and deductions > 0:
        adj_row = (
            f"<tr style='background:#f0fdf4'>"
            f"<td style='padding:6px 14px;color:#166534'>If all recorded deductions applied (−${deductions:,.0f})</td>"
            f"<td style='padding:6px 14px;text-align:right;color:#166534;font-weight:600'>"
            f"${adj_est['total_tax']:,.2f} at {adj_est['effective_rate_pct']}% effective</td></tr>"
        )
    lito_row = (
        f"<tr><td style='padding:6px 14px'>Low Income Tax Offset (LITO)</td>"
        f"<td style='padding:6px 14px;text-align:right;color:#555'>(${est['lito']:,.2f})</td></tr>"
        if est["lito"] > 0 else ""
    )
    return f"""{header}
<table style="width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:8px">
  <thead><tr style="background:#92400e;color:white">
    <th style="padding:8px 14px;text-align:left">Component</th>
    <th style="padding:8px 14px;text-align:right">Amount</th>
  </tr></thead>
  <tbody>
    <tr><td style="padding:6px 14px">Taxable Income</td><td style="padding:6px 14px;text-align:right">${taxable_income:,.2f}</td></tr>
    <tr><td style="padding:6px 14px">Gross Income Tax</td><td style="padding:6px 14px;text-align:right">${est['gross_tax']:,.2f}</td></tr>
    {lito_row}
    <tr><td style="padding:6px 14px">Medicare Levy (2%)</td><td style="padding:6px 14px;text-align:right">${est['medicare_levy']:,.2f}</td></tr>
    {adj_row}
    <tr style="background:#fff7ed;font-weight:700;border-top:2px solid #fed7aa">
      <td style="padding:8px 14px">Estimated Total Tax Payable</td>
      <td style="padding:8px 14px;text-align:right;color:#92400e">${est['total_tax']:,.2f} ({est['effective_rate_pct']}% effective)</td>
    </tr>
    <tr style="background:#f0fdf4;font-weight:700">
      <td style="padding:8px 14px">Estimated Net Income After Tax</td>
      <td style="padding:8px 14px;text-align:right;color:#166534">${est['net_income']:,.2f}</td>
    </tr>
  </tbody>
</table>
<p style="font-size:.75rem;color:#64748b;margin:0 0 20px">
  ATO FY{fy} brackets + Medicare levy. LITO applied where eligible.
  Estimate only &mdash; does not account for offsets, rebates, PAYG withholding, or
  superannuation contributions tax. Confirm with ATO and your tax professional.
  Brackets: Stage 3 cuts apply from FY2025.
</p>"""


def _fy_hecs_section(fy: int, taxable_income: float) -> str:
    """Return HECS/HELP repayment alert HTML if income exceeds threshold, else empty string."""
    if taxable_income <= 0:
        return ""
    from src.tax_estimator import estimate_hecs_repayment
    result = estimate_hecs_repayment(taxable_income, fy)
    if result is None:
        return ""
    threshold = result["threshold"]
    rate = result["rate_pct"]
    repayment = result["repayment"]
    return f"""<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;
padding:14px 20px;margin-bottom:20px;font-size:.88rem">
  <strong style="color:#92400e">&#9432; HECS/HELP Repayment Alert</strong><br>
  <span style="color:#78350f">
    Your FY{fy} taxable income of <strong>${taxable_income:,.0f}</strong> exceeds the HECS/HELP
    minimum repayment threshold of <strong>${threshold:,}</strong>.
    At the {rate}% repayment rate, estimated compulsory repayment:
    <strong>${repayment:,.2f}</strong>.
    This is paid via your tax return — check your HELP debt balance at
    <a href="https://my.gov.au" style="color:#92400e">my.gov.au</a>.
  </span>
  <div style="font-size:.72rem;color:#b45309;margin-top:6px">
    Based on ATO 2024-25 thresholds — confirm current year rates at ato.gov.au.
  </div>
</div>"""


def prepare_fy_summary_data(
    df: pd.DataFrame,
    config: dict,
    cg_data: dict,
    franking_data: dict | None = None,
) -> list[str]:
    """Build per-FY section HTML blocks for the FY Summary live route.

    cg_data: pre-loaded dict from capital_gains.json (may be {}).
    franking_data: pre-loaded dict from franking_credits.json (may be None/{}).
    Returns list of HTML section strings, newest FY first.
    """
    if df.empty:
        return []

    company_name = (
        config.get("business", {}).get("full_name")
        or config.get("business", {}).get("company_name", "your employer")
    )

    df = df.copy()
    df["_fy"] = df["date"].apply(lambda d: d.year + 1 if d.month >= 7 else d.year)
    all_fys = sorted(df["_fy"].dropna().unique().astype(int), reverse=True)

    from datetime import date as _date
    today = _date.today()

    sections = []
    for fy in all_fys:
        fy_df = df[df["_fy"] == fy]
        fy_start = f"1 Jul {fy - 1}"
        fy_end = f"30 Jun {fy}"
        is_current = (_date(fy - 1, 7, 1) <= today <= _date(fy, 6, 30))
        badge_style = "background:#E9C46A;color:#333" if is_current else "background:#4CC9F0;color:#1a1a2e"
        badge_label = "current" if is_current else "complete"
        fy_badge = f'<span style="{badge_style};padding:2px 10px;border-radius:4px;font-size:.75rem;font-weight:600">{badge_label}</span>'

        # ── Income ──────────────────────────────────────────────────────────
        inc_df = fy_df[fy_df["category"].isin(_INCOME_CATEGORIES) & (fy_df["amount"] > 0)]
        income_rows = []
        total_income = 0.0
        taxable_income = 0.0
        for cat in ["Income", "Board & Lodging", "Interest Income", "Business Reimbursement", "Family Loan Received"]:
            subtotal = inc_df[inc_df["category"] == cat]["amount"].sum()
            if subtotal > 0:
                total_income += subtotal
                if cat in _TAXABLE_INCOME_CATS:
                    taxable_income += subtotal
                tag = _INCOME_EXEMPT_TAGS.get(cat)
                cat_badge = (
                    f' <span style="background:{tag[1]};color:{tag[2]};border-radius:3px;'
                    f'padding:1px 6px;font-size:.72rem;font-weight:600">{tag[0]}</span>'
                    if tag else ""
                )
                income_rows.append(
                    f"<tr><td style='padding:7px 14px'>{html.escape(cat)}{cat_badge}</td>"
                    f"<td style='padding:7px 14px;text-align:right'>${subtotal:,.2f}</td></tr>"
                )
        income_body = "\n".join(income_rows) if income_rows else (
            "<tr><td colspan='2' style='padding:8px 14px;color:#888'>No income recorded</td></tr>"
        )

        # ── Business expenses ───────────────────────────────────────────────
        biz_df = fy_df[fy_df["is_business"] & (fy_df["amount"] < 0)].sort_values("date")
        total_biz = biz_df["amount"].abs().sum()
        biz_rows = []
        for _, row in biz_df.iterrows():
            d = row["date"].strftime("%d %b %Y") if hasattr(row["date"], "strftime") else str(row["date"])[:10]
            biz_rows.append(
                f"<tr>"
                f"<td style='padding:6px 14px;white-space:nowrap'>{d}</td>"
                f"<td style='padding:6px 14px'>{html.escape(str(row.get('description', ''))[:60])}</td>"
                f"<td style='padding:6px 14px'>{html.escape(str(row.get('category', '')))}</td>"
                f"<td style='padding:6px 14px;color:#555;font-size:.82rem'>{html.escape(str(row.get('account', '')))}</td>"
                f"<td style='padding:6px 14px;text-align:right;font-weight:600'>${abs(row['amount']):,.2f}</td>"
                f"</tr>"
            )
        biz_body = "\n".join(biz_rows) if biz_rows else (
            "<tr><td colspan='5' style='padding:8px 14px;color:#888'>No business expenses recorded</td></tr>"
        )

        # ── Tax-deductible expenses ─────────────────────────────────────────
        has_tax_col = "is_tax_deductible" in fy_df.columns
        tax_df = fy_df[fy_df["is_tax_deductible"] & (fy_df["amount"] < 0)].sort_values("date") if has_tax_col else fy_df.iloc[0:0]
        # ── GST claimable ───────────────────────────────────────────────────
        has_gst_col = "is_gst_claimable" in fy_df.columns
        gst_df = fy_df[fy_df["is_gst_claimable"] & (fy_df["amount"] < 0)].sort_values("date") if has_gst_col else fy_df.iloc[0:0]
        total_tax = tax_df["amount"].abs().sum()
        tax_rows = []
        for _, row in tax_df.iterrows():
            d = row["date"].strftime("%d %b %Y") if hasattr(row["date"], "strftime") else str(row["date"])[:10]
            tax_rows.append(
                f"<tr>"
                f"<td style='padding:6px 14px;white-space:nowrap'>{d}</td>"
                f"<td style='padding:6px 14px'>{html.escape(str(row.get('description', ''))[:60])}</td>"
                f"<td style='padding:6px 14px'>{html.escape(str(row.get('category', '')))}</td>"
                f"<td style='padding:6px 14px;color:#555;font-size:.82rem'>{html.escape(str(row.get('account', '')))}</td>"
                f"<td style='padding:6px 14px;text-align:right;font-weight:600'>${abs(row['amount']):,.2f}</td>"
                f"</tr>"
            )
        tax_body = "\n".join(tax_rows) if tax_rows else (
            "<tr><td colspan='5' style='padding:8px 14px;color:#888'>No tax-deductible expenses recorded. "
            "Flag transactions using the <strong>TAX</strong> button on the Transactions page.</td></tr>"
        )

        # ── Expenditure breakdown ────────────────────────────────────────────
        exp_df = fy_df[~fy_df["category"].isin(_EXCLUDE_FROM_SPEND) & (fy_df["amount"] < 0)]
        total_exp = exp_df["amount"].abs().sum()
        net_savings = total_income - total_exp
        savings_rate = round((net_savings / total_income) * 100) if total_income > 0 else 0
        sr_color = "#2a9d8f" if savings_rate >= 20 else "#E9C46A" if savings_rate >= 0 else "#E63946"
        net_color = "#22a86e" if net_savings >= 0 else "#E63946"
        net_prefix = "+" if net_savings >= 0 else ""

        cat_totals = (
            exp_df.groupby("category")["amount"]
            .apply(lambda x: x.abs().sum())
            .sort_values(ascending=False)
        )
        exp_rows = []
        for _cat, _total in cat_totals.items():
            _pct = f"{round(_total / total_exp * 100)}%" if total_exp > 0 else "–"
            _bar_w = round(_total / total_exp * 100) if total_exp > 0 else 0
            exp_rows.append(
                f"<tr>"
                f"<td style='padding:6px 14px'>{html.escape(str(_cat))}</td>"
                f"<td style='padding:6px 14px;text-align:right'>${_total:,.2f}</td>"
                f"<td style='padding:6px 14px;min-width:100px'>"
                f"<div style='font-size:.78rem;color:#666;margin-bottom:2px'>{_pct}</div>"
                f"<div style='height:4px;background:#e2e8f0;border-radius:2px'>"
                f"<div style='height:4px;background:#E63946;border-radius:2px;width:{_bar_w}%'></div>"
                f"</div></td></tr>"
            )
        exp_body = "\n".join(exp_rows) if exp_rows else (
            "<tr><td colspan='3' style='padding:8px 14px;color:#888'>No expenditure recorded</td></tr>"
        )

        gst_claimable_total = float(gst_df["amount"].abs().sum()) if not gst_df.empty else 0.0

        sections.append(f"""
<div style="background:white;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.07);overflow:hidden;margin-bottom:28px">
  <div style="background:#1d3540;color:white;padding:16px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <h2 style="margin:0;font-size:1.15rem;flex:1">FY{fy} &nbsp; {fy_start} – {fy_end} &nbsp; {fy_badge}</h2>
    <a href="/api/tax-export/{fy}" style="font-size:.78rem;padding:6px 14px;background:#059669;color:white;border-radius:6px;text-decoration:none;font-weight:600;white-space:nowrap">&#x2B07; Download Tax Package</a>
    <a href="/api/business-export/{fy}?format=ofx" style="font-size:.78rem;padding:6px 14px;background:#0369a1;color:white;border-radius:6px;text-decoration:none;font-weight:600;white-space:nowrap">&#x2B07; Xero (OFX)</a>
    <a href="/api/business-export/{fy}?format=qif" style="font-size:.78rem;padding:6px 14px;background:#7c3aed;color:white;border-radius:6px;text-decoration:none;font-weight:600;white-space:nowrap">&#x2B07; MYOB (QIF)</a>
  </div>
  <div style="padding:20px 24px">
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:14px 16px;text-align:center">
        <div style="font-size:.7rem;color:#166534;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Taxable Income</div>
        <div style="font-size:1.25rem;font-weight:700;color:#166534">${taxable_income:,.0f}</div>
      </div>
      <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:14px 16px;text-align:center">
        <div style="font-size:.7rem;color:#991b1b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Total Expenditure</div>
        <div style="font-size:1.25rem;font-weight:700;color:#991b1b">${total_exp:,.0f}</div>
      </div>
      <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:14px 16px;text-align:center">
        <div style="font-size:.7rem;color:#0c4a6e;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Net Savings</div>
        <div style="font-size:1.25rem;font-weight:700;color:{net_color}">{net_prefix}${abs(net_savings):,.0f}</div>
      </div>
      <div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px 16px;text-align:center">
        <div style="font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Savings Rate</div>
        <div style="font-size:1.25rem;font-weight:700;color:{sr_color}">{savings_rate}%</div>
      </div>
    </div>
    <p style="font-size:.75rem;color:#94a3b8;margin:-8px 0 18px">* Net Savings and Savings Rate use total receipts (${total_income:,.0f}) including Board &amp; Lodging (exempt) and Business Reimbursements. Taxable Income above excludes these.</p>
    <h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">Income</h3>
    <table style="width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:20px">
      <thead><tr style="background:#264653;color:white">
        <th style="padding:8px 14px;text-align:left">Source</th>
        <th style="padding:8px 14px;text-align:right">Amount</th>
      </tr></thead>
      <tbody>
        {income_body}
        <tr style="border-top:1px solid #e2e8f0">
          <td style="padding:5px 14px;color:#94a3b8;font-size:.82rem">Total Receipts (incl. exempt &amp; non-taxable)</td>
          <td style="padding:5px 14px;text-align:right;color:#94a3b8;font-size:.82rem">${total_income:,.2f}</td>
        </tr>
        <tr style="background:#e8f5e9;font-weight:700;border-top:1px solid #c8e6c9">
          <td style="padding:8px 14px">Taxable Income</td>
          <td style="padding:8px 14px;text-align:right">${taxable_income:,.2f}</td>
        </tr>
      </tbody>
    </table>
    <h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">Business Expenses — Reimbursable from {html.escape(company_name)}</h3>
    <table style="width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:20px">
      <thead><tr style="background:#E63946;color:white">
        <th style="padding:8px 14px;text-align:left">Date</th>
        <th style="padding:8px 14px;text-align:left">Description</th>
        <th style="padding:8px 14px;text-align:left">Category</th>
        <th style="padding:8px 14px;text-align:left">Account</th>
        <th style="padding:8px 14px;text-align:right">Amount</th>
      </tr></thead>
      <tbody>
        {biz_body}
        <tr style="background:#fff3ee;font-weight:700;border-top:2px solid #f4c7b2">
          <td colspan="4" style="padding:8px 14px">Total Reimbursable</td>
          <td style="padding:8px 14px;text-align:right">${total_biz:,.2f}</td>
        </tr>
      </tbody>
    </table>
    <h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">Tax-Deductible Expenses — Personal Deductions</h3>
    <table style="width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:20px">
      <thead><tr style="background:#7C3AED;color:white">
        <th style="padding:8px 14px;text-align:left">Date</th>
        <th style="padding:8px 14px;text-align:left">Description</th>
        <th style="padding:8px 14px;text-align:left">Category</th>
        <th style="padding:8px 14px;text-align:left">Account</th>
        <th style="padding:8px 14px;text-align:right">Amount</th>
      </tr></thead>
      <tbody>
        {tax_body}
        <tr style="background:#f5f3ff;font-weight:700;border-top:2px solid #ddd6fe">
          <td colspan="4" style="padding:8px 14px">Total Tax Deductible</td>
          <td style="padding:8px 14px;text-align:right">${total_tax:,.2f}</td>
        </tr>
      </tbody>
    </table>
    {_fy_gst_section(fy, gst_df)}
    {_fy_cg_section(fy, cg_data)}
    {_fy_franking_section(fy, franking_data)}
    {_fy_income_tax_section(fy, taxable_income, gst_claimable_total, total_tax)}
    {_fy_hecs_section(fy, taxable_income)}
    <h3 style="margin:0 0 10px;font-size:.95rem;color:#264653;text-transform:uppercase;letter-spacing:.05em">Expenditure by Category</h3>
    <table style="width:100%;border-collapse:collapse;font-size:.88rem">
      <thead><tr style="background:#475569;color:white">
        <th style="padding:8px 14px;text-align:left">Category</th>
        <th style="padding:8px 14px;text-align:right">Amount</th>
        <th style="padding:8px 14px;text-align:left">% of Spend</th>
      </tr></thead>
      <tbody>
        {exp_body}
        <tr style="background:#f1f5f9;font-weight:700;border-top:2px solid #cbd5e1">
          <td style="padding:8px 14px">Total Expenditure</td>
          <td style="padding:8px 14px;text-align:right">${total_exp:,.2f}</td>
          <td style="padding:8px 14px;color:#666">100%</td>
        </tr>
      </tbody>
    </table>
  </div>
</div>""")

    return sections


def generate_fy_summary(df: pd.DataFrame, config: dict, output_dir: Path) -> None:
    """Generate reports/fy_summary.html — income and business expenses by Australian financial year."""
    output_path = output_dir / "fy_summary.html"

    # Load capital gains and franking credits data (silent default if file absent)
    import json as _json
    _cg_path = output_dir.parent / "Data" / "capital_gains.json"
    try:
        _cg_data: dict = _json.loads(_cg_path.read_text(encoding="utf-8")) if _cg_path.exists() else {}
    except Exception:
        _cg_data = {}
    _fc_path = output_dir.parent / "Data" / "franking_credits.json"
    try:
        _franking_data: dict = _json.loads(_fc_path.read_text(encoding="utf-8")) if _fc_path.exists() else {}
    except Exception:
        _franking_data = {}

    sections = prepare_fy_summary_data(df, config, _cg_data, _franking_data)
    content = "\n".join(sections) if sections else "<p style='padding:20px;color:#888'>No data.</p>"

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Financial Year Summary</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f0f4f8;margin:0;padding:20px 24px;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#264653 0%,#2a7b8e 100%);color:white;
         padding:24px 32px;border-radius:12px;margin-bottom:20px}}
.header h1{{margin:0 0 4px;font-size:1.5rem}}
.header p{{margin:0;opacity:.75;font-size:.9rem}}
.site-nav{{background:#1d3540;padding:0 24px;display:flex;gap:4px;
           border-radius:10px;margin-bottom:20px}}
.nav-link{{color:rgba(255,255,255,.65);text-decoration:none;padding:12px 18px;
           font-size:.88rem;font-weight:600;border-bottom:3px solid transparent}}
.nav-link:hover{{color:white}}
.nav-link.active{{color:white;border-bottom-color:#2a9d8f}}
.tab-bar{{display:flex;gap:2px;background:#1d3540;border-radius:10px;
          padding:6px 12px;margin-bottom:20px;flex-wrap:wrap;align-items:center}}
.tab-btn{{padding:8px 16px;border:none;border-radius:7px;background:transparent;
          color:rgba(255,255,255,.55);font-size:.82rem;font-weight:600;cursor:pointer;
          border-bottom:3px solid transparent;text-decoration:none;display:inline-block}}
.tab-btn.active{{background:rgba(42,157,143,.15);color:#2a9d8f;border-bottom-color:#2a9d8f}}
.tab-btn:hover:not(.active){{color:white}}
{_IMPORT_CSS}
</style>
</head>
<body>
{_build_nav_html("fy_summary")}
<div class="header">
  <h1>Financial Year Summary</h1>
  <p>Australian financial year (1 Jul &ndash; 30 Jun). Income and reimbursable business expenses by year.</p>
</div>
{_help_box(
    "How to use this page",
    "A snapshot of each financial year's income, spending, and business expenses. Useful for tax preparation and year-on-year comparison.",
    [
        "Each tab is one financial year &mdash; click to switch between years.",
        "<strong>KPI banner</strong> &mdash; each year shows Taxable Income, Total Expenditure, Net Savings, and Savings Rate at a glance.",
        "<strong>Taxable Income</strong> includes only Income and Interest Income. Board & Lodging (ATO exempt) and Business Reimbursements are shown in the table but excluded from the taxable figure.",
        "<strong>Income</strong> rows include all transactions categorised as Income, Board & Lodging, Interest Income, or Business Reimbursement.",
        "<strong>Expenditure by Category</strong> &mdash; breakdown of all non-income spending for the year, sorted largest first.",
        "<strong>Business expenses</strong> are transactions flagged as business-related &mdash; amounts your employer should reimburse.",
        "<strong>Capital Gains</strong> &mdash; investment sale summary (gross gains, CGT discount, net taxable gains). Enter figures via the Capital Gains page.",
        "Use this page to cross-check figures for your tax return.",
    ]
)}
{content}
{_IMPORT_JS}
</body>
</html>"""

    try:
        output_path.write_text(page_html, encoding="utf-8")
    except OSError as exc:
        logger.error(f"  ERROR: could not write FY summary -> {output_path}: {exc}")
        return
    logger.info(f"  FY summary -> {output_path} ({len(sections)} year(s))")



# ── Dashboard ─────────────────────────────────────────────────────────────────

def _period_select_options(fy_options_html: str) -> str:
    """Return the <option>/<optgroup> block used in both the dashboard and transactions page."""
    return f"""<optgroup label="Standard">
      <option value="this_month">This Month</option>
      <option value="last_month">Last Month</option>
      <option value="this_quarter">This Quarter (AUS FY)</option>
      <option value="last_quarter">Last Quarter</option>
    </optgroup>
    <optgroup label="Rolling">
      <option value="last_3m">Last 3 Months</option>
      <option value="last_6m">Last 6 Months</option>
      <option value="last_12m">Last 12 Months</option>
    </optgroup>
    <optgroup label="Financial Year">
{fy_options_html}
    </optgroup>
    <optgroup label="Calendar Year">
      <option value="this_cal">This Calendar Year</option>
      <option value="last_cal">Last Calendar Year</option>
    </optgroup>
    <option value="all">All Time</option>
    <option value="custom">Custom Range...</option>"""


def _period_dates_js() -> str:
    """Return the JS periodDates() function body (shared between pages)."""
    return r"""
function periodDates(p) {
  const todayD = new Date(TODAY);
  const y = todayD.getFullYear(), m = todayD.getMonth();
  const fyY = m >= 6 ? y : y - 1;

  // AUS FY quarters (0-indexed months)
  // Q1=Jul(6)-Sep(8), Q2=Oct(9)-Dec(11), Q3=Jan(0)-Mar(2), Q4=Apr(3)-Jun(5)
  let cqS, cqE, lqS, lqE;
  if (m >= 6 && m <= 8)      { cqS=[fyY,6,1];   cqE=[fyY,8,30];   lqS=[fyY-1,3,1]; lqE=[fyY-1,5,30]; }
  else if (m >= 9 && m <= 11){ cqS=[fyY,9,1];   cqE=[fyY,11,31];  lqS=[fyY,6,1];   lqE=[fyY,8,30]; }
  else if (m >= 0 && m <= 2) { cqS=[fyY+1,0,1]; cqE=[fyY+1,2,31]; lqS=[fyY,9,1];   lqE=[fyY,11,31]; }
  else                        { cqS=[fyY+1,3,1]; cqE=[fyY+1,5,30]; lqS=[fyY+1,0,1]; lqE=[fyY+1,2,31]; }

  function fmt(yy,mm,dd) {
    return yy+'-'+String(mm+1).padStart(2,'0')+'-'+String(dd).padStart(2,'0');
  }
  function addMonths(date, n) {
    const d = new Date(date);
    d.setMonth(d.getMonth() + n);
    return d;
  }
  function isoDate(d) {
    return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  }
  function firstOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
  function lastOfMonth(d)  {
    const n = new Date(d.getFullYear(), d.getMonth()+1, 0);
    return n;
  }

  if (p === 'this_month') {
    const f = firstOfMonth(todayD);
    return {from: isoDate(f), to: TODAY};
  }
  if (p === 'last_month') {
    const prev = new Date(todayD.getFullYear(), todayD.getMonth()-1, 1);
    return {from: isoDate(prev), to: isoDate(lastOfMonth(prev))};
  }
  if (p === 'this_quarter') {
    return {from: fmt(...cqS), to: fmt(...cqE)};
  }
  if (p === 'last_quarter') {
    return {from: fmt(...lqS), to: fmt(...lqE)};
  }
  if (p === 'last_3m') {
    const f = addMonths(firstOfMonth(todayD), -3);
    return {from: isoDate(f), to: TODAY};
  }
  if (p === 'last_6m') {
    const f = addMonths(firstOfMonth(todayD), -6);
    return {from: isoDate(f), to: TODAY};
  }
  if (p === 'last_12m') {
    const f = addMonths(firstOfMonth(todayD), -12);
    return {from: isoDate(f), to: TODAY};
  }
  if (p === 'this_fy') {
    return {from: fyY+'-07-01', to: (fyY+1)+'-06-30'};
  }
  if (p === 'this_cal') {
    return {from: y+'-01-01', to: y+'-12-31'};
  }
  if (p === 'last_cal') {
    return {from: (y-1)+'-01-01', to: (y-1)+'-12-31'};
  }
  if (p === 'all') {
    return {from: null, to: null};
  }
  if (p === 'custom') {
    return null;
  }
  // fy_YYYY
  const fyOpt = FY_OPTIONS.find(function(o) { return o.value === p; });
  if (fyOpt) return {from: fyOpt.from, to: fyOpt.to};
  return {from: null, to: null};
}
"""


# ── Chart tab summary tables ──────────────────────────────────────────────────

def _tbl_html(title: str, headers: list[str], rows: list) -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div style="background:white;border-radius:12px;padding:16px;'
        f'border:1px solid #e2e8f0;margin-top:16px;box-shadow:0 1px 4px rgba(0,0,0,.07)">'
        f'<h4 style="margin:0 0 12px;font-size:.82rem;color:#64748b;'
        f'text-transform:uppercase;letter-spacing:.06em;font-weight:700">{title}</h4>'
        f'<div class="tbl-wrap"><table class="dash-tbl"><thead><tr>{head}</tr></thead>'
        f'<tbody>{body}</tbody></table></div></div>'
    )


def prepare_dashboard_data(
    df: pd.DataFrame,
    config: dict,
    reports_dir: "Path | None" = None,
) -> dict:
    """Build all Python-side data for the live dashboard route.

    Does not write any files.
    Returns: {txn_json, budget_json, cat_colors_json, today_str, fy_opts_js,
              period_opts, period_dates_js, rec_timestamp, rec_preview_html}
    """
    import json as _json
    from datetime import date as _date
    from src.budgets import load_budgets as _load_budgets

    txn_records = []
    for _, row in df.iterrows():
        txn_records.append({
            "d":   row["date"].strftime("%Y-%m-%d"),
            "a":   float(row["amount"]),
            "c":   str(row.get("category", "")),
            "sub": str(row.get("sub_category", "") or ""),
            "desc": str(row.get("description", "")),
            "acc": str(row.get("account", "")),
            "b":   bool(row.get("is_business", False)),
        })
    txn_json       = _json.dumps(txn_records, ensure_ascii=False, separators=(",", ":"))
    budget_json    = _json.dumps(_load_budgets(config))
    cat_colors_json = _json.dumps(CATEGORY_COLORS)

    real_today = _date.today()
    today_str  = real_today.strftime("%Y-%m-%d")

    if df.empty:
        earliest_fy = real_today.year if real_today.month >= 7 else real_today.year - 1
    else:
        dmn = df["date"].min()
        earliest_fy = dmn.year if dmn.month >= 7 else dmn.year - 1

    current_fy_start = real_today.year if real_today.month >= 7 else real_today.year - 1
    fy_parts: list[str] = []
    this_fy_label = f"FY{current_fy_start + 1} (Jul {current_fy_start} – Jun {current_fy_start + 1})"
    fy_parts.append(f'      <option value="this_fy" selected>{this_fy_label}</option>')
    fy_opts_list: list[dict] = []
    for fy_start in range(current_fy_start - 1, earliest_fy - 1, -1):
        fy_val   = f"fy_{fy_start + 1}"
        fy_label = f"FY{fy_start + 1} (Jul {fy_start} – Jun {fy_start + 1})"
        fy_parts.append(f'      <option value="{fy_val}">{fy_label}</option>')
        fy_opts_list.append({
            "value": fy_val, "label": fy_label,
            "from": f"{fy_start}-07-01", "to": f"{fy_start + 1}-06-30",
        })
    fy_options_html = "\n".join(fy_parts)
    fy_opts_js      = _json.dumps(fy_opts_list)
    period_opts     = _period_select_options(fy_options_html)
    period_dates_js = _period_dates_js()

    rec_timestamp    = ""
    rec_preview_html = (
        "<p><em>No recommendations generated yet. "
        "Upload a statement file to trigger an import, or visit the Recommendations page.</em></p>"
    )
    if reports_dir is not None:
        rec_path = reports_dir / "recommendations.md"
        if rec_path.exists():
            rec_md = rec_path.read_text(encoding="utf-8")
            for _line in rec_md.splitlines()[:4]:
                if _line.startswith("*Generated"):
                    rec_timestamp = _line.strip("*").strip()
                    break
            _qw = re.search(r'##\s+Quick Wins\n+(.*?)(?=\n##\s|\Z)', rec_md, re.DOTALL)
            if not _qw:
                _qw = re.search(r'##\s+.+\n+(.*?)(?=\n##\s|\Z)', rec_md, re.DOTALL)
            if _qw:
                rec_preview_html = _md_to_html(_qw.group(1).strip())

    # Query latest balance snapshot for accounts flagged as savings
    savings_balance = 0.0
    savings_configured = False
    debt_total = 0.0
    balance_history_json = _json.dumps([])
    try:
        from src.db import get_db as _get_db
        _dconn = _get_db(config)
        try:
            # Savings balance
            _rows = _dconn.execute("""
                SELECT bs.account, bs.balance
                FROM balance_snapshots bs
                INNER JOIN (
                    SELECT account, MAX(date) AS max_date
                    FROM balance_snapshots GROUP BY account
                ) latest ON bs.account = latest.account AND bs.date = latest.max_date
                INNER JOIN accounts a ON a.account_name = bs.account
                WHERE a.is_savings = 1
            """).fetchall()
            if _rows:
                savings_configured = True
                savings_balance = sum(float(r[1]) for r in _rows)

            # Monthly net worth: sum of each account's latest balance per calendar month
            _bh = _dconn.execute("""
                SELECT substr(bs.date,1,7) AS ym, SUM(bs.balance) AS total
                FROM balance_snapshots bs
                INNER JOIN (
                    SELECT account, substr(date,1,7) AS ym2, MAX(date) AS max_date
                    FROM balance_snapshots GROUP BY account, substr(date,1,7)
                ) latest ON bs.account=latest.account AND bs.date=latest.max_date
                         AND substr(bs.date,1,7)=latest.ym2
                WHERE bs.date >= date('now','-14 months')
                GROUP BY ym ORDER BY ym
            """).fetchall()
            balance_history_json = _json.dumps(
                [{"m": r[0], "nw": round(float(r[1]), 2)} for r in _bh]
            )

            # Outstanding borrowed loans (for debt-to-income ratio)
            try:
                from src.loans import load_loans as _ll, calculate_loan_position as _clp
                for _loan in _ll(config):
                    if _loan.get("direction") == "borrowed":
                        _ost = float((_clp(_dconn, _loan) or {}).get("outstanding") or 0)
                        if _ost > 0:
                            debt_total += _ost
            except Exception:
                pass
        finally:
            _dconn.close()
    except Exception:
        pass

    # Upcoming bills within 14 days (for dashboard widget)
    upcoming_bills_json = _json.dumps([])
    try:
        from src.commitment_detector import load_commitments as _lc, get_upcoming as _gu
        from datetime import date as _ddate
        _comms = _lc(config)
        _today_d = _ddate.today()
        _bills = []
        for _b in _gu(_comms, days_ahead=14):
            _pd = _ddate.fromisoformat(_b["projected_date"])
            _bills.append({
                "name":     _b.get("name", ""),
                "amount":   float(_b.get("amount", 0)),
                "date":     _b["projected_date"],
                "days":     (_pd - _today_d).days,
                "category": _b.get("category", ""),
                "freq":     _b.get("frequency", "monthly"),
            })
        upcoming_bills_json = _json.dumps(_bills)
    except Exception:
        pass

    return {
        "txn_json":              txn_json,
        "budget_json":           budget_json,
        "cat_colors_json":       cat_colors_json,
        "today_str":             today_str,
        "fy_opts_js":            fy_opts_js,
        "period_opts":           period_opts,
        "period_dates_js":       period_dates_js,
        "rec_timestamp":         rec_timestamp,
        "rec_preview_html":      rec_preview_html,
        "savings_balance":       savings_balance,
        "savings_configured":    savings_configured,
        "debt_total":            debt_total,
        "balance_history_json":  balance_history_json,
        "upcoming_bills_json":   upcoming_bills_json,
    }


def generate_dashboard(
    df: pd.DataFrame,
    config: dict,
    output_dir: Path,
) -> None:
    import json as _json

    output_path = output_dir / "monthly_summary.html"

    # ── Build compact TXN JSON ────────────────────────────────────────────────
    txn_records = []
    for _, row in df.iterrows():
        txn_records.append({
            "d": row["date"].strftime("%Y-%m-%d"),
            "a": float(row["amount"]),
            "c": str(row.get("category", "")),
            "sub": str(row.get("sub_category", "") or ""),
            "desc": str(row.get("description", "")),
            "acc": str(row.get("account", "")),
            "b": bool(row.get("is_business", False)),
        })
    txn_json = _json.dumps(txn_records, ensure_ascii=False, separators=(",", ":"))
    from src.budgets import load_budgets as _load_budgets
    budget_json = _json.dumps(_load_budgets(config))
    cat_colors_json = _json.dumps(CATEGORY_COLORS)
    from datetime import date as _date
    real_today = _date.today()
    today_str = real_today.strftime("%Y-%m-%d")

    # ── Build FY options from data range ─────────────────────────────────────
    data_min_year = df["date"].min().year
    data_min_month = df["date"].min().month
    # earliest FY start year in data
    earliest_fy = data_min_year if data_min_month >= 7 else data_min_year - 1
    current_fy_start = real_today.year if real_today.month >= 7 else real_today.year - 1

    fy_options_html_parts = []
    # This FY first (selected by default)
    this_fy_label = f"FY{current_fy_start + 1} (Jul {current_fy_start} – Jun {current_fy_start + 1})"
    fy_options_html_parts.append(
        f'      <option value="this_fy" selected>{this_fy_label}</option>'
    )
    # Prior FYs dynamically from data
    fy_opts_list = []
    for fy_start in range(current_fy_start - 1, earliest_fy - 1, -1):
        fy_val = f"fy_{fy_start + 1}"
        fy_label = f"FY{fy_start + 1} (Jul {fy_start} – Jun {fy_start + 1})"
        fy_from = f"{fy_start}-07-01"
        fy_to = f"{fy_start + 1}-06-30"
        fy_options_html_parts.append(
            f'      <option value="{fy_val}">{fy_label}</option>'
        )
        fy_opts_list.append({"value": fy_val, "label": fy_label, "from": fy_from, "to": fy_to})
    fy_options_html = "\n".join(fy_options_html_parts)
    fy_opts_js = _json.dumps(fy_opts_list)

    period_opts = _period_select_options(fy_options_html)
    period_dates_js = _period_dates_js()

    # ── Recommendations card (Quick Wins preview only) ────────────────────────
    rec_path = output_dir / "recommendations.md"
    _rec_timestamp = ""
    _rec_preview_html = "<p><em>No recommendations generated yet. Upload a statement file to trigger an import, or visit the Recommendations page.</em></p>"
    if rec_path.exists():
        rec_md = rec_path.read_text(encoding="utf-8")
        for _line in rec_md.splitlines()[:4]:
            if _line.startswith("*Generated"):
                _rec_timestamp = _line.strip("*").strip()
                break
        _qw = re.search(r'##\s+Quick Wins\n+(.*?)(?=\n##\s|\Z)', rec_md, re.DOTALL)
        if not _qw:
            _qw = re.search(r'##\s+.+\n+(.*?)(?=\n##\s|\Z)', rec_md, re.DOTALL)
        if _qw:
            _rec_preview_html = _md_to_html(_qw.group(1).strip())

    # ── Number of months in data for avg/mo ──────────────────────────────────
    num_months_all = max(
        df["date"].dt.to_period("M").nunique(), 1
    )

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Personal Finance Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f0f4f8;margin:0;padding:20px 24px;color:#1a1a2e}}
.site-nav{{background:#1d3540;padding:0 24px;display:flex;gap:4px;
           border-radius:10px;margin-bottom:20px;align-items:center}}
.nav-link{{color:rgba(255,255,255,.65);text-decoration:none;padding:12px 18px;
           font-size:.88rem;font-weight:600;border-bottom:3px solid transparent}}
.nav-link:hover{{color:white}}
.nav-link.active{{color:white;border-bottom-color:#2a9d8f}}
/* Tab bars */
.tab-bar{{display:none;gap:2px;background:#1d3540;border-radius:10px;
          padding:6px 12px;margin-bottom:20px;flex-wrap:wrap;align-items:center}}
.tab-bar.visible{{display:flex}}
a.tab-btn{{text-decoration:none;display:inline-block}}
.tab-btn{{padding:8px 16px;border:none;border-radius:7px;background:transparent;
          color:rgba(255,255,255,.55);font-size:.82rem;font-weight:600;cursor:pointer;
          border-bottom:3px solid transparent}}
.tab-btn.active{{background:rgba(42,157,143,.15);color:#2a9d8f;border-bottom-color:#2a9d8f}}
.tab-btn:hover:not(.active){{color:white}}
.tab-panel{{display:none}}
.tab-panel.active{{display:block}}
.chart-wrap{{background:white;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
/* Controls bar */
.controls{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
           background:white;border-radius:10px;padding:12px 18px;margin-bottom:20px;border:1px solid #e2e8f0}}
.controls label{{font-size:.8rem;color:#64748b;margin-right:4px}}
.controls select,.controls input[type=date]{{
  padding:7px 12px;border-radius:8px;border:1px solid #dde;
  background:white;color:#1a1a2e;font-size:.85rem;cursor:pointer}}
.controls select:focus,.controls input[type=date]:focus{{outline:none;border-color:#2a9d8f}}
.period-label{{font-size:.82rem;color:#64748b;margin-left:auto;font-style:italic}}
/* View toggle */
.view-toggle{{display:flex;gap:0;border:1px solid #dde;border-radius:8px;overflow:hidden}}
.view-toggle button{{padding:7px 14px;border:none;background:#f0f4f8;color:#64748b;
                     font-size:.8rem;font-weight:600;cursor:pointer;white-space:nowrap}}
.view-toggle button.active{{background:#2a9d8f;color:white}}
.view-toggle button:hover:not(.active){{background:#e2e8f0;color:#1a1a2e}}
/* Custom date row */
.custom-dates{{display:none;gap:8px;align-items:center}}
.custom-dates.visible{{display:flex}}
/* Summary cards */
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:20px}}
@media(max-width:900px){{.cards{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:500px){{.cards{{grid-template-columns:1fr}}}}
.card{{background:white;border-radius:12px;padding:18px 20px;
       border:1px solid #e2e8f0;box-shadow:0 2px 8px rgba(0,0,0,.05)}}
.card .lbl{{font-size:.72rem;color:#64748b;text-transform:uppercase;
             letter-spacing:.07em;margin-bottom:6px}}
.card .val{{font-size:1.6rem;font-weight:700;line-height:1}}
.card .sub{{font-size:.75rem;color:#64748b;margin-top:4px}}
/* 2-col row */
.row-2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}}
@media(max-width:800px){{.row-2{{grid-template-columns:1fr}}}}
.panel{{background:white;border-radius:12px;padding:16px;border:1px solid #e2e8f0;box-shadow:0 2px 8px rgba(0,0,0,.05)}}
.panel h3{{margin:0 0 12px;font-size:.9rem;color:#64748b;text-transform:uppercase;
           letter-spacing:.06em;font-weight:700}}
/* Tables */
.tbl-wrap{{overflow-x:auto}}
table.dash-tbl{{width:100%;border-collapse:collapse;font-size:.82rem}}
table.dash-tbl thead th{{color:#64748b;font-weight:700;text-transform:uppercase;
                          font-size:.72rem;letter-spacing:.05em;padding:8px 10px;
                          border-bottom:1px solid #dde;text-align:left;white-space:nowrap}}
table.dash-tbl tbody td{{padding:7px 10px;border-bottom:1px solid #f0f0f8;
                           color:#1a1a2e;vertical-align:middle}}
table.dash-tbl tbody tr:last-child td{{border-bottom:none}}
table.dash-tbl tbody tr:hover td{{background:rgba(42,157,143,.06)}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;
       margin-right:6px;vertical-align:middle;flex-shrink:0}}
.mini-bar-wrap{{width:80px;height:6px;background:#e8ecf0;border-radius:3px;
                 display:inline-block;vertical-align:middle}}
.mini-bar{{height:6px;background:#2a9d8f;border-radius:3px}}
/* Largest txn table */
.panel-full{{background:white;border-radius:12px;padding:16px;
              border:1px solid #e2e8f0;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.05)}}
.panel-full h3{{margin:0 0 12px;font-size:.9rem;color:#64748b;text-transform:uppercase;
                letter-spacing:.06em;font-weight:700}}
/* Recommendations */
.rec-panel{{background:white;border-radius:12px;padding:24px 28px;
             border:1px solid #e2e8f0;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.05)}}
.rec-panel h2{{margin:0 0 14px;font-size:1rem;color:#1a1a2e;font-weight:700}}
.rec-controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}}
.rec-controls select{{padding:7px 12px;border-radius:8px;border:1px solid #dde;
                       background:white;color:#1a1a2e;font-size:.85rem}}
.rec-controls select:focus{{outline:none;border-color:#2a9d8f}}
.btn-refresh{{padding:7px 16px;background:#2a9d8f;color:white;border:none;
               border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:600}}
.btn-refresh:hover{{background:#21867a}}
.btn-refresh:disabled{{background:#555;cursor:not-allowed}}
.rec-content h1{{font-size:1.2rem;color:#1a1a2e;margin-top:0}}
.rec-content h2{{font-size:1rem;color:#2a9d8f;border-bottom:1px solid #dde;
                  padding-bottom:6px;margin-top:24px}}
.rec-content h3{{font-size:.9rem;color:#64748b}}
.rec-content ul,.rec-content ol{{padding-left:1.4em;line-height:1.8}}
.rec-content li{{margin-bottom:4px}}
.rec-content table{{border-collapse:collapse;width:100%;font-size:.82rem;margin:12px 0}}
.rec-content td,.rec-content th{{border:1px solid #dde;padding:7px 10px;text-align:left}}
.rec-content thead td,.rec-content th{{background:#264653;color:white;font-weight:600}}
.rec-content tr:nth-child(even){{background:#f8fafc}}
.rec-content p{{line-height:1.65;color:#334155}}
.rec-content em{{color:#64748b}}
.rec-content code{{background:#f1f5f9;color:#264653;padding:1px 5px;border-radius:3px;font-size:.85em}}
.rec-loading{{color:#64748b;font-style:italic;padding:8px 0}}
{_IMPORT_CSS}
</style>
</head>
<body>
<nav class="site-nav">
  <a href="monthly_summary.html" class="nav-link active" id="nav-dashboard" onclick="event.preventDefault();showSection('dashboard')">Dashboard</a>
  <a href="#reports" class="nav-link" id="nav-reports" onclick="event.preventDefault();showSection('reports')">Reports</a>
  <a href="/reports/transactions.html" class="nav-link">Data</a>
  <a href="/settings/accounts" class="nav-link">Settings</a>
  <a href="/help" class="nav-link">Help</a>
  <div class="nav-actions">
    <button id="upload-btn" class="nav-upload-btn" onclick="openUpload()">&#8593; Upload Files</button>
  </div>
</nav>
{_IMPORT_MODAL}
<div id="upload-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9998;align-items:center;justify-content:center"
     onclick="if(event.target===this)closeUpload()">
  <div style="background:#fff;border-radius:14px;padding:32px;width:520px;max-width:96vw">
    <h3 style="margin:0 0 8px;color:#264653;font-size:1.15rem">Upload Statement Files</h3>
    <p style="margin:0 0 20px;font-size:.85rem;color:#666">Drop statement files here. Import, categorisation and report refresh run automatically in the background.</p>
    <div id="drop-zone" style="border:2px dashed #2a9d8f;border-radius:10px;padding:36px 24px;text-align:center;cursor:pointer;transition:background .2s"
         onclick="document.getElementById('file-input').click()"
         ondragover="event.preventDefault();this.style.background='#e8f8f6'"
         ondragleave="this.style.background=''"
         ondrop="handleDrop(event)">
      <div style="font-size:2rem;margin-bottom:8px">&#128196;</div>
      <div style="color:#264653;font-weight:600">Drop files here or click to browse</div>
      <div style="font-size:.8rem;color:#888;margin-top:4px">.csv &nbsp; .pdf &nbsp; .html</div>
    </div>
    <input type="file" id="file-input" multiple accept=".csv,.pdf,.html,.CSV,.PDF,.HTML" style="display:none" onchange="handleFileSelect(this.files)">
    <div id="upload-list" style="margin-top:14px;font-size:.85rem;color:#444;max-height:140px;overflow-y:auto"></div>
    <div id="upload-status" style="margin-top:10px;font-size:.85rem;font-weight:600"></div>
    <div style="margin-top:20px;display:flex;gap:10px;justify-content:flex-end">
      <button onclick="closeUpload()" style="padding:8px 20px;border:1px solid #ccd;border-radius:8px;background:#fff;cursor:pointer;font-size:.9rem">Close</button>
      <button id="upload-submit-btn" onclick="submitUpload()" style="padding:8px 22px;background:#2a9d8f;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:600">Upload Files</button>
    </div>
  </div>
</div>

<!-- Reports tab bar (hidden until Reports nav is clicked) -->
<div class="tab-bar" id="reports-tab-bar">
  <button id="tab-btn-income"     class="tab-btn active" onclick="showTab('income')">Income vs Expenditure</button>
  <button id="tab-btn-spend"      class="tab-btn" onclick="showTab('spend')">Spend by Category</button>
  <button id="tab-btn-budget"     class="tab-btn" onclick="showTab('budget')">Budget vs Actual</button>
  <button id="tab-btn-trends"     class="tab-btn" onclick="showTab('trends')">Spend Trends</button>
  <button id="tab-btn-recurring"  class="tab-btn" onclick="showTab('recurring')">Recurring</button>
  <button id="tab-btn-merchants"  class="tab-btn" onclick="showTab('merchants')">Top Merchants</button>
  <button id="tab-btn-heatmap"    class="tab-btn" onclick="showTab('heatmap')">Day of Week</button>
  <button id="tab-btn-business"   class="tab-btn" onclick="showTab('business')">Business Expenses</button>
</div>
<!-- Reports help (hidden until Reports nav is clicked) -->
<div id="reports-help" style="display:none">
{_help_box(
    "How to use the Reports section",
    "Interactive charts built from all your transaction data. Every chart responds instantly to the period filter.",
    [
        "<strong>Filter period</strong> — use the dropdown above to narrow all charts to any date range: this month, a financial year, last 90 days, or a custom range. Charts update instantly with no server call.",
        "<strong>Click a category bar</strong> in <em>Spend by Category</em> or <em>Top Merchants</em> to jump directly to the Transactions page, pre-filtered to that category and the current date range.",
        "<strong>Edit a category</strong> on the Transactions page by clicking any row to expand it, then selecting a new category from the dropdown — changes save automatically.",
        "<strong>Income vs Expenditure</strong> — monthly grouped bar showing all income streams vs total spend.",
        "<strong>Spend by Category</strong> — total spend per category for the period, sorted largest first. Click any bar to drill down.",
        "<strong>Budget vs Actual</strong> — compares your configured monthly budgets against actual average spend. Red bars exceed budget.",
        "<strong>Spend Trends</strong> — monthly line chart for your top 8 spending categories.",
        "<strong>Recurring</strong> — merchants appearing in 2 or more months, sorted by total spend — useful for finding subscriptions.",
        "<strong>Top Merchants</strong> — top 20 individual merchants by total spend. Click any bar to see all transactions from that merchant.",
        "<strong>Day of Week</strong> — heatmap showing which days of the week you spend the most across each month.",
        "<strong>Business Expenses</strong> — all transactions flagged as business expenses, for reimbursement tracking.",
    ]
)}
</div>
<!-- Reports filter bar (hidden until Reports nav is clicked) -->
<div id="reports-filter-bar" style="display:none;flex-wrap:wrap;gap:10px;align-items:center;
     background:white;border-radius:10px;padding:12px 18px;margin-bottom:20px;border:1px solid #e2e8f0;
     box-shadow:0 1px 4px rgba(0,0,0,.05)">
  <label style="font-size:.82rem;font-weight:600;color:#475569;white-space:nowrap">Filter period</label>
  <select id="report-period-sel" onchange="onReportPeriodChange()"
          style="padding:7px 12px;border-radius:8px;border:1px solid #dde;font-size:.85rem;color:#334155;background:white">
    {period_opts}
  </select>
  <div id="report-custom-dates" style="display:none;gap:8px;align-items:center">
    <input type="date" id="report-from-date"
           style="padding:7px 10px;border:1px solid #ccd;border-radius:8px;font-size:.85rem">
    <span style="color:#94a3b8">to</span>
    <input type="date" id="report-to-date"
           style="padding:7px 10px;border:1px solid #ccd;border-radius:8px;font-size:.85rem">
    <button onclick="applyReportFilter()"
            style="padding:7px 14px;background:#2a9d8f;color:white;border:none;border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:600">Apply</button>
  </div>
  <span id="report-filter-status" style="font-size:.82rem;color:#94a3b8;font-style:italic"></span>
</div>

<div id="tab-panel-overview" class="tab-panel active">
<!-- Controls -->
<div class="controls">
  <label>Period</label>
  <select id="period-preset" onchange="onPeriodChange()">
    {period_opts}
  </select>
  <div class="custom-dates" id="custom-dates">
    <input type="date" id="custom-from">
    <span style="color:#94a3b8">to</span>
    <input type="date" id="custom-to">
    <button onclick="renderDashboard()" style="padding:7px 14px;background:#2a9d8f;color:white;border:none;border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:600">Apply</button>
  </div>
  <div class="view-toggle" style="margin-left:12px">
    <button id="view-both"    class="active" onclick="setView('both')">Income &amp; Expenses</button>
    <button id="view-expenses"               onclick="setView('expenses')">Expenses Only</button>
    <button id="view-income"                 onclick="setView('income')">Income Only</button>
  </div>
  <span class="period-label" id="period-label"></span>
</div>

<!-- Summary cards -->
<div class="cards" id="cards"></div>

<!-- Budget status chips -->
<div id="budget-status" style="margin-bottom:20px"></div>

<!-- Monthly Income vs Expenses - full width -->
<div class="panel" style="margin-bottom:20px">
  <h3>Monthly Income vs Expenses</h3>
  <div id="chart-bar" style="height:320px"></div>
</div>

<!-- Spend by Category - full width horizontal bar -->
<div class="panel" style="margin-bottom:20px">
  <h3 id="spend-bar-title">Spend by Category</h3>
  <div id="chart-spend-bar"></div>
</div>

<!-- Tables row -->
<div class="row-2">
  <div class="panel">
    <h3>Category Breakdown</h3>
    <div class="tbl-wrap"><table class="dash-tbl" id="cat-tbl">
      <thead><tr><th>Category</th><th>Total</th><th>% Spend</th><th>Avg/Month</th><th>Transactions</th></tr></thead>
      <tbody id="cat-tbl-body"></tbody>
    </table></div>
  </div>
  <div class="panel">
    <h3>Top 10 Merchants by Spend</h3>
    <div class="tbl-wrap"><table class="dash-tbl" id="merch-tbl">
      <thead><tr><th>Merchant</th><th>Category</th><th>Total</th></tr></thead>
      <tbody id="merch-tbl-body"></tbody>
    </table></div>
  </div>
</div>

<!-- Largest transactions -->
<div class="panel-full">
  <h3>Largest Transactions</h3>
  <div class="tbl-wrap"><table class="dash-tbl" id="large-tbl">
    <thead><tr><th>Date</th><th>Description</th><th>Account</th><th>Category</th><th style="text-align:right">Amount</th></tr></thead>
    <tbody id="large-tbl-body"></tbody>
  </table></div>
</div>

<!-- Recommendations card -->
<div class="rec-panel">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;flex-wrap:wrap;gap:8px">
    <h2 style="margin:0">AI Recommendations — Quick Wins</h2>
    <a href="/recommendations" style="padding:7px 18px;background:#2a9d8f;color:white;border-radius:8px;font-size:.85rem;font-weight:600;text-decoration:none;white-space:nowrap">View Full Report &#8594;</a>
  </div>
  <p style="font-size:.79rem;color:#94a3b8;margin:0 0 14px">{_rec_timestamp}</p>
  <div class="rec-content">{_rec_preview_html}</div>
</div>

</div><!-- end tab-panel-overview -->
<!-- __CHART_PANELS__ -->

<script>
const TXN = {txn_json};
const TODAY = "{today_str}";
const FY_OPTIONS = {fy_opts_js};
const BUDGET_DATA = {budget_json};
const CAT_COLORS = {cat_colors_json};
const INCOME_CATS = {{"Income": true, "Board & Lodging": true, "Interest Income": true, "Business Reimbursement": true, "Family Loan Received": true}};
const EXCLUDE_SPEND = {{"Income": true, "Board & Lodging": true, "Interest Income": true, "Business Reimbursement": true, "Family Loan Received": true, "Transfers": true, "Investment": true}};

let currentView = 'both';
let currentFrom = null;
let currentTo   = null;

// ── Helpers ──────────────────────────────────────────────────────────────────
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function fmtAmt(v) {{
  return '$' + Math.abs(v).toLocaleString('en-AU', {{minimumFractionDigits:0,maximumFractionDigits:0}});
}}
function fmtAmtDec(v) {{
  return '$' + Math.abs(v).toLocaleString('en-AU', {{minimumFractionDigits:2,maximumFractionDigits:2}});
}}
function cleanMerchant(desc) {{
  desc = String(desc);
  desc = desc.replace(/VISA DEBIT PURCHASE CARD \\d+\\s*/i,'');
  desc = desc.replace(/ANZ INTERNET BANKING BPAY\\s*/i,'');
  desc = desc.replace(/ANZ INTERNET BANKING FUNDS TFER\\s*(TRANSFER\\s*)?\\d*\\s*(FROM|TO)?\\s*/i,'');
  desc = desc.replace(/ANZ M-?BANKING (FUNDS TFER|PAYMENT)\\s*(TRANSFER\\s*)?\\d*\\s*(FROM|TO)?\\s*/i,'');
  desc = desc.replace(/PAYMENT (TO|FROM)\\s*/i,'');
  desc = desc.replace(/\\s*\\{{?\\d{{5,}}\\}}?\\s*$/,'');
  return desc.trim().substring(0,50).trim();
}}

{period_dates_js}

// ── Period / view controls ───────────────────────────────────────────────────
function onPeriodChange() {{
  const p = document.getElementById('period-preset').value;
  const cd = document.getElementById('custom-dates');
  if (p === 'custom') {{
    cd.classList.add('visible');
    return; // wait for Apply click
  }}
  cd.classList.remove('visible');
  const range = periodDates(p);
  if (range) {{ currentFrom = range.from; currentTo = range.to; }}
  renderDashboard();
}}

function setView(v) {{
  currentView = v;
  ['both','expenses','income'].forEach(function(x) {{
    document.getElementById('view-'+x).classList.toggle('active', x===v);
  }});
  renderDashboard();
}}

function getFilteredTxn() {{
  const from = currentFrom, to = currentTo;
  return TXN.filter(function(t) {{
    if (from && t.d < from) return false;
    if (to   && t.d > to  ) return false;
    return true;
  }});
}}

// ── Render ───────────────────────────────────────────────────────────────────
function renderDashboard() {{
  // Handle custom range apply
  const p = document.getElementById('period-preset').value;
  if (p === 'custom') {{
    currentFrom = document.getElementById('custom-from').value || null;
    currentTo   = document.getElementById('custom-to').value   || null;
  }}

  const txn = getFilteredTxn();

  // Period label
  const lbl = document.getElementById('period-label');
  if (currentFrom || currentTo) {{
    lbl.textContent = (currentFrom || '?') + ' to ' + (currentTo || '?');
  }} else {{
    lbl.textContent = 'All Time';
  }}

  renderCards(txn);
  renderBudgetStatus(txn);
  renderBarChart(txn);
  renderSpendBar(txn);
  renderCatTable(txn);
  renderMerchantTable(txn);
  renderLargeTable(txn);
}}

// ── Cards ────────────────────────────────────────────────────────────────────
function renderCards(txn) {{
  const months = new Set();
  txn.forEach(function(t) {{ months.add(t.d.substring(0,7)); }});
  const numMo = Math.max(months.size, 1);

  const income = txn.filter(function(t) {{ return INCOME_CATS[t.c] && t.a > 0; }})
                     .reduce(function(s,t){{ return s+t.a; }}, 0);
  const expenses = txn.filter(function(t) {{ return t.a < 0 && !EXCLUDE_SPEND[t.c]; }})
                       .reduce(function(s,t){{ return s+Math.abs(t.a); }}, 0);
  const net = income - expenses;
  const savRate = income > 0 ? Math.round((net/income)*100) : 0;
  const biz = txn.filter(function(t) {{ return t.b && t.a < 0; }})
                   .reduce(function(s,t){{ return s+Math.abs(t.a); }}, 0);

  const incAvg = fmtAmt(income/numMo) + '/mo';
  const expAvg = fmtAmt(expenses/numMo) + '/mo';

  let srColor = savRate >= 20 ? '#2a9d8f' : savRate >= 0 ? '#E9C46A' : '#E63946';
  let netColor = net >= 0 ? '#00BB77' : '#E63946';

  document.getElementById('cards').innerHTML =
    card('Income', fmtAmt(income), incAvg, '#00BB77') +
    card('Expenses', fmtAmt(expenses), expAvg, '#E63946') +
    card('Net Cashflow', (net>=0?'+':'-') + fmtAmt(Math.abs(net)), '', netColor) +
    card('Savings Rate', savRate+'%', '', srColor) +
    card('Business Expenses', fmtAmt(biz), 'reimbursable', '#FB5607');
}}

function card(lbl, val, sub, color) {{
  return '<div class="card">' +
    '<div class="lbl">'+esc(lbl)+'</div>' +
    '<div class="val" style="color:'+color+'">'+esc(val)+'</div>' +
    (sub ? '<div class="sub">'+esc(sub)+'</div>' : '') +
    '</div>';
}}

// ── Budget status ─────────────────────────────────────────────────────────────
function renderBudgetStatus(txn) {{
  var el = document.getElementById('budget-status');
  if (!el) return;
  var budget = BUDGET_DATA;
  if (!budget || !Object.keys(budget).length) {{ el.innerHTML = ''; return; }}

  // Current month only
  var now = new Date();
  var ym = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0');
  var monthTxn = txn.filter(function(t) {{ return t.d.substring(0,7) === ym && t.a < 0 && !EXCLUDE_SPEND[t.c]; }});

  var actual = {{}};
  monthTxn.forEach(function(t) {{ actual[t.c] = (actual[t.c]||0) + Math.abs(t.a); }});

  var over=0, warn=0, ok=0;
  var chips = Object.keys(budget).map(function(cat) {{
    var spent = actual[cat] || 0;
    var limit = budget[cat];
    var pct = limit > 0 ? spent/limit : 0;
    var bg, color, icon;
    if (pct >= 1)      {{ bg='#7f1d1d'; color='#fca5a5'; icon='&#9888; '; over++; }}
    else if (pct>=0.8) {{ bg='#451a03'; color='#fcd34d'; icon='&#9679; '; warn++; }}
    else               {{ bg='#14532d'; color='#86efac'; icon='&#10003; '; ok++;  }}
    return '<span style="display:inline-flex;align-items:center;gap:4px;background:'+bg+
           ';color:'+color+';border-radius:6px;padding:4px 10px;font-size:.78rem;font-weight:600;white-space:nowrap">'+
           icon+esc(cat)+' <span style="opacity:.75">$'+Math.round(spent)+' / $'+limit+'</span></span>';
  }}).join(' ');

  var total = Object.keys(budget).length;
  var summaryColor = over > 0 ? '#fca5a5' : warn > 0 ? '#fcd34d' : '#86efac';
  var summaryText = over > 0 ? over+' over budget' : warn > 0 ? warn+' approaching limit' : 'All categories on track';

  el.innerHTML = '<div style="background:#1d3540;border-radius:10px;padding:14px 18px;margin-bottom:4px">' +
    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">' +
    '<span style="font-size:.82rem;font-weight:700;color:#e2e8f0">Budget — '+esc(ym)+'</span>' +
    '<span style="font-size:.78rem;color:'+summaryColor+';font-weight:600">'+summaryText+' ('+ok+'/'+total+')</span>' +
    '</div>' +
    '<div style="display:flex;flex-wrap:wrap;gap:6px">'+chips+'</div></div>';
}}

// ── Bar chart ────────────────────────────────────────────────────────────────
function renderBarChart(txn) {{
  const monthIncome = {{}}, monthExpenses = {{}};
  txn.forEach(function(t) {{
    const mo = t.d.substring(0,7);
    if (INCOME_CATS[t.c] && t.a > 0) {{
      monthIncome[mo] = (monthIncome[mo]||0) + t.a;
    }}
    if (t.a < 0 && !EXCLUDE_SPEND[t.c]) {{
      monthExpenses[mo] = (monthExpenses[mo]||0) + Math.abs(t.a);
    }}
  }});

  const months = Array.from(new Set(
    Object.keys(monthIncome).concat(Object.keys(monthExpenses))
  )).sort();

  const inc = months.map(function(m){{ return monthIncome[m]||0; }});
  const exp = months.map(function(m){{ return monthExpenses[m]||0; }});
  const net = months.map(function(m,i){{ return inc[i]-exp[i]; }});

  const traces = [];
  if (currentView !== 'expenses') {{
    traces.push({{
      type:'bar', name:'Income', x:months, y:inc,
      marker:{{color:'#00BB77'}},
      hovertemplate:'%{{x}}<br>Income: $%{{y:,.0f}}<extra></extra>'
    }});
  }}
  if (currentView !== 'income') {{
    traces.push({{
      type:'bar', name:'Expenses', x:months, y:exp,
      marker:{{color:'#E63946'}},
      hovertemplate:'%{{x}}<br>Expenses: $%{{y:,.0f}}<extra></extra>'
    }});
  }}
  if (currentView === 'both') {{
    traces.push({{
      type:'scatter', name:'Net', x:months, y:net, mode:'lines+markers',
      line:{{color:'#2a9d8f',width:2,dash:'dot'}},
      marker:{{size:6}},
      hovertemplate:'%{{x}}<br>Net: $%{{y:,.0f}}<extra></extra>'
    }});
  }}

  const layout = {{
    barmode:'group', paper_bgcolor:'transparent', plot_bgcolor:'transparent',
    font:{{color:'#334155',size:11}},
    xaxis:{{showgrid:false,zeroline:false,showline:false}},
    yaxis:{{gridcolor:'#edf2f7',showgrid:true,zeroline:false,showline:false,
            tickprefix:'$',tickformat:',.0f'}},
    legend:{{orientation:'h',y:1.12,font:{{size:11}}}},
    margin:{{l:60,r:20,t:20,b:50}},
    height:280
  }};
  Plotly.newPlot('chart-bar', traces, layout, {{responsive:true,displayModeBar:false}});
}}

// ── Spend by Category horizontal bar ─────────────────────────────────────────
function renderSpendBar(txn) {{
  const isIncome = currentView === 'income';
  document.getElementById('spend-bar-title').textContent =
    isIncome ? 'Income by Source' : 'Spend by Category';

  const totals = {{}};
  txn.forEach(function(t) {{
    if (isIncome) {{
      if (INCOME_CATS[t.c] && t.a > 0) totals[t.c] = (totals[t.c]||0) + t.a;
    }} else {{
      if (t.a < 0 && !EXCLUDE_SPEND[t.c]) totals[t.c] = (totals[t.c]||0) + Math.abs(t.a);
    }}
  }});

  const grand = Object.values(totals).reduce(function(a,b){{ return a+b; }}, 0);
  // Sort ascending so largest appears at top of horizontal bar chart
  const labels = Object.keys(totals).sort(function(a,b){{ return totals[a]-totals[b]; }});
  const values = labels.map(function(l){{ return totals[l]; }});
  const colors = labels.map(function(l){{ return CAT_COLORS[l]||'#8D8D8D'; }});
  const textLabels = values.map(function(v) {{
    const pct = grand > 0 ? (v/grand*100).toFixed(1) : '0.0';
    return '$' + v.toLocaleString('en-AU', {{maximumFractionDigits:0}}) + '  (' + pct + '%)';
  }});

  const chartHeight = Math.max(320, labels.length * 34 + 60);
  const el = document.getElementById('chart-spend-bar');
  el.style.height = chartHeight + 'px';

  Plotly.newPlot('chart-spend-bar', [{{
    type:'bar', orientation:'h',
    x:values, y:labels,
    marker:{{color:colors}},
    text:textLabels, textposition:'outside',
    hovertemplate:'%{{y}}<br>$%{{x:,.2f}}<extra></extra>',
    cliponaxis: false
  }}], {{
    paper_bgcolor:'transparent', plot_bgcolor:'transparent',
    font:{{color:'#334155',size:12}},
    xaxis:{{tickprefix:'$',tickformat:',.0f',gridcolor:'#edf2f7',showgrid:true,
            zeroline:false,showline:false}},
    yaxis:{{automargin:true,showgrid:false,zeroline:false,showline:false}},
    margin:{{l:10,r:180,t:10,b:40}},
    height:chartHeight,
    showlegend:false
  }}, {{responsive:true,displayModeBar:false}});
}}

// ── Category table ────────────────────────────────────────────────────────────
function renderCatTable(txn) {{
  const months = new Set();
  txn.forEach(function(t) {{ months.add(t.d.substring(0,7)); }});
  const numMo = Math.max(months.size, 1);

  // tree: {{ cat: {{ total, count, subs: {{ subcat: total }} }} }}
  const tree = {{}};
  let grand = 0;
  txn.forEach(function(t) {{
    if (t.a < 0 && !EXCLUDE_SPEND[t.c]) {{
      const v = Math.abs(t.a);
      if (!tree[t.c]) tree[t.c] = {{ total: 0, count: 0, subs: {{}} }};
      tree[t.c].total += v;
      tree[t.c].count += 1;
      grand += v;
      if (t.sub) {{
        tree[t.c].subs[t.sub] = (tree[t.c].subs[t.sub]||0) + v;
      }}
    }}
  }});

  const sortedCats = Object.keys(tree).sort(function(a,b){{ return tree[b].total - tree[a].total; }});

  const rows = [];
  sortedCats.forEach(function(cat) {{
    const node = tree[cat];
    const v = node.total;
    const pct = grand > 0 ? (v/grand*100).toFixed(1) : '0.0';
    const avg = fmtAmtDec(v/numMo);
    const color = CAT_COLORS[cat]||'#8D8D8D';
    rows.push('<tr style="font-weight:600">' +
      '<td><span class="dot" style="background:'+color+'"></span>'+esc(cat)+'</td>' +
      '<td>'+fmtAmtDec(v)+'</td>' +
      '<td>'+pct+'%</td>' +
      '<td>'+avg+'</td>' +
      '<td style="text-align:right;color:#64748b">'+node.count+'</td>' +
      '</tr>');
    const subKeys = Object.keys(node.subs).sort(function(a,b){{ return node.subs[b]-node.subs[a]; }});
    subKeys.forEach(function(sub) {{
      const sv = node.subs[sub];
      const spct = grand > 0 ? (sv/grand*100).toFixed(1) : '0.0';
      const savg = fmtAmtDec(sv/numMo);
      rows.push('<tr style="opacity:0.75">' +
        '<td style="padding-left:24px;color:#64748b">&#8627; '+esc(sub)+'</td>' +
        '<td>'+fmtAmtDec(sv)+'</td>' +
        '<td>'+spct+'%</td>' +
        '<td>'+savg+'</td>' +
        '<td></td>' +
        '</tr>');
    }});
  }});

  document.getElementById('cat-tbl-body').innerHTML = rows.join('') ||
    '<tr><td colspan="5" style="color:#64748b;text-align:center;padding:16px">No expense data</td></tr>';
}}

// ── Merchant table ────────────────────────────────────────────────────────────
function renderMerchantTable(txn) {{
  const totals = {{}}, cats = {{}};
  txn.forEach(function(t) {{
    if (t.a < 0 && !EXCLUDE_SPEND[t.c]) {{
      const m = cleanMerchant(t.desc);
      totals[m] = (totals[m]||0) + Math.abs(t.a);
      if (!cats[m]) cats[m] = t.c;
    }}
  }});

  const sorted = Object.keys(totals).sort(function(a,b){{ return totals[b]-totals[a]; }}).slice(0,10);
  const rows = sorted.map(function(m) {{
    const color = CAT_COLORS[cats[m]]||'#8D8D8D';
    return '<tr>' +
      '<td>'+esc(m)+'</td>' +
      '<td><span class="dot" style="background:'+color+'"></span>'+esc(cats[m])+'</td>' +
      '<td>'+fmtAmtDec(totals[m])+'</td>' +
      '</tr>';
  }}).join('');

  document.getElementById('merch-tbl-body').innerHTML = rows ||
    '<tr><td colspan="3" style="color:#94a3b8;text-align:center;padding:16px">No data</td></tr>';
}}

// ── Largest transactions ──────────────────────────────────────────────────────
function renderLargeTable(txn) {{
  const expenses = txn.filter(function(t){{ return t.a < 0 && !EXCLUDE_SPEND[t.c]; }});
  expenses.sort(function(a,b){{ return Math.abs(b.a)-Math.abs(a.a); }});
  const top = expenses.slice(0,10);

  const rows = top.map(function(t) {{
    const color = CAT_COLORS[t.c]||'#8D8D8D';
    return '<tr>' +
      '<td style="white-space:nowrap">'+esc(t.d)+'</td>' +
      '<td title="'+esc(t.desc)+'">'+esc(t.desc.substring(0,60))+'</td>' +
      '<td>'+esc(t.acc)+'</td>' +
      '<td><span class="dot" style="background:'+color+'"></span>'+esc(t.c)+'</td>' +
      '<td style="text-align:right;color:#E63946;font-weight:600">'+fmtAmtDec(t.a)+'</td>' +
      '</tr>';
  }}).join('');

  document.getElementById('large-tbl-body').innerHTML = rows ||
    '<tr><td colspan="5" style="color:#94a3b8;text-align:center;padding:16px">No data</td></tr>';
}}

// ── Report chart rendering (client-side) ─────────────────────────────────────
var _RPT_LAYOUT = {{
  paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)',
  font:{{color:'#334155'}},
  hoverlabel:{{bgcolor:'#1e293b',font:{{color:'white',size:11}},bordercolor:'rgba(0,0,0,0)'}},
}};
function _rptL(extra) {{ return Object.assign({{}}, _RPT_LAYOUT, extra); }}
function _rptFmt(v) {{
  return '$' + Math.abs(v).toLocaleString('en-AU',{{minimumFractionDigits:0,maximumFractionDigits:0}});
}}
function _rptSpend(txn) {{
  return txn.filter(function(t){{ return t.a < 0 && !EXCLUDE_SPEND[t.c]; }});
}}
function _cleanDesc(d) {{
  return d.toUpperCase().trim()
    .replace(/^VISA DEBIT PURCHASE CARD \\d+\\s*/i,'')
    .replace(/^ANZ INTERNET BANKING BPAY\\s*/i,'')
    .replace(/^ANZ (INTERNET BANKING FUNDS TFER|M-?BANKING (FUNDS TFER|PAYMENT)).*?(FROM|TO)?\\s*/i,'')
    .replace(/^PAYMENT (TO|FROM)\\s*/i,'')
    .replace(/\\s*\\{{?\\d{{5,}}\\}}?\\s*$/,'')
    .substring(0,50).trim();
}}

function _rptIncome(txn, el) {{
  var mi={{}}, me={{}};
  txn.forEach(function(t){{
    var mo=t.d.substring(0,7);
    if(INCOME_CATS[t.c]&&t.a>0) mi[mo]=(mi[mo]||0)+t.a;
    if(t.a<0&&!EXCLUDE_SPEND[t.c]) me[mo]=(me[mo]||0)+Math.abs(t.a);
  }});
  var months=Object.keys(Object.assign({{}},mi,me)).sort();
  Plotly.newPlot(el,[
    {{name:'Income',      x:months,y:months.map(function(m){{return mi[m]||0;}}),type:'bar',marker:{{color:'#00BB77'}}}},
    {{name:'Expenditure', x:months,y:months.map(function(m){{return me[m]||0;}}),type:'bar',marker:{{color:'#E63946'}}}},
  ],_rptL({{title:'Monthly Income vs Expenditure',barmode:'group',
    xaxis:{{title:'Month',showgrid:false,zeroline:false}},
    yaxis:{{title:'AUD',tickformat:'$,.0f',gridcolor:'#edf2f7',zeroline:false}},
    legend:{{orientation:'h',y:1.12}},margin:{{t:60,r:20,b:60,l:80}}
  }}),{{responsive:true,displayModeBar:false}});
}}

function _rptSpendCat(txn, el, fromDate, toDate) {{
  var byCat={{}};
  _rptSpend(txn).forEach(function(t){{ byCat[t.c]=(byCat[t.c]||0)+Math.abs(t.a); }});
  var cats=Object.keys(byCat).sort(function(a,b){{return byCat[a]-byCat[b];}});
  if(!cats.length){{ el.innerHTML='<p style="padding:40px;text-align:center;color:#94a3b8">No spend data.</p>'; return; }}
  var plt=Plotly.newPlot(el,[{{
    type:'bar',orientation:'h',
    x:cats.map(function(c){{return byCat[c];}}),y:cats,
    marker:{{color:cats.map(function(c){{return CAT_COLORS[c]||'#8D8D8D';}})}},
    text:cats.map(function(c){{return _rptFmt(byCat[c]);}}),textposition:'outside',
    hovertemplate:'%{{y}}: %{{text}}<extra></extra>',
  }}],_rptL({{title:'Spend by Category',
    xaxis:{{tickformat:'$,.0f',gridcolor:'#edf2f7',zeroline:false}},
    yaxis:{{automargin:true}},
    height:Math.max(400,cats.length*32+80),margin:{{t:60,r:110,b:60,l:160}}
  }}),{{responsive:true,displayModeBar:false}});
  plt.then(function(){{
    el.on('plotly_click',function(d){{
      var cat=d.points[0].y;
      var url='transactions.html?cat='+encodeURIComponent(cat);
      if(fromDate) url+='&from='+fromDate;
      if(toDate)   url+='&to='+toDate;
      window.location.href=url;
    }});
  }});
}}

function _rptBudget(txn, el) {{
  var budget=BUDGET_DATA;
  if(!budget||!Object.keys(budget).length){{
    el.innerHTML='<p style="padding:40px;text-align:center;color:#94a3b8">No budget configured in config.yaml.</p>'; return;
  }}
  var months=new Set(); txn.forEach(function(t){{months.add(t.d.substring(0,7));}});
  var nMo=Math.max(months.size,1);
  var act={{}};
  _rptSpend(txn).forEach(function(t){{act[t.c]=(act[t.c]||0)+Math.abs(t.a);}});
  var cats=Object.keys(budget);
  var bv=cats.map(function(c){{return budget[c]||0;}});
  var av=cats.map(function(c){{return (act[c]||0)/nMo;}});
  Plotly.newPlot(el,[
    {{name:'Budget',x:cats,y:bv,type:'bar',marker:{{color:'rgba(100,120,140,0.25)'}},
      text:bv.map(_rptFmt),textposition:'outside'}},
    {{name:'Actual (avg/month)',x:cats,y:av,type:'bar',
      marker:{{color:av.map(function(v,i){{return v<=bv[i]?'#2A9D8F':'#E63946';}})}},
      text:av.map(_rptFmt),textposition:'outside'}},
  ],_rptL({{title:'Budget vs Actual (avg/month)',barmode:'group',
    yaxis:{{tickformat:'$,.0f',gridcolor:'#edf2f7',zeroline:false}},
    margin:{{t:60,r:20,b:80,l:80}}
  }}),{{responsive:true,displayModeBar:false}});
}}

function _rptTrends(txn, el) {{
  var byCatMo={{}};
  _rptSpend(txn).forEach(function(t){{
    if(!byCatMo[t.c]) byCatMo[t.c]={{}};
    var mo=t.d.substring(0,7);
    byCatMo[t.c][mo]=(byCatMo[t.c][mo]||0)+Math.abs(t.a);
  }});
  var allMo=[]; var seen={{}};
  Object.values(byCatMo).forEach(function(m){{Object.keys(m).forEach(function(mo){{if(!seen[mo]){{seen[mo]=1;allMo.push(mo);}}}})}});
  allMo.sort();
  var totals=Object.keys(byCatMo).map(function(c){{
    return {{c:c,total:Object.values(byCatMo[c]).reduce(function(s,v){{return s+v;}},0)}};
  }}).sort(function(a,b){{return b.total-a.total;}}).slice(0,8);
  var traces=totals.map(function(item){{
    return {{name:item.c,type:'scatter',mode:'lines+markers',
      x:allMo,y:allMo.map(function(m){{return byCatMo[item.c][m]||0;}}),
      line:{{color:CAT_COLORS[item.c]||'#8D8D8D',width:2}},marker:{{size:5}}}};
  }});
  if(!traces.length){{ el.innerHTML='<p style="padding:40px;text-align:center;color:#94a3b8">No data.</p>'; return; }}
  Plotly.newPlot(el,traces,_rptL({{
    title:'Monthly Spend Trends — Top 8 Categories',
    xaxis:{{title:'Month',showgrid:false,zeroline:false}},
    yaxis:{{title:'AUD',tickformat:'$,.0f',gridcolor:'#edf2f7',zeroline:false}},
    legend:{{orientation:'h',y:-0.25}},margin:{{t:60,r:20,b:120,l:80}}
  }}),{{responsive:true,displayModeBar:false}});
}}

function _rptRecurring(txn, el) {{
  var byM={{}};
  _rptSpend(txn).forEach(function(t){{
    var m=_cleanDesc(t.desc);
    if(!byM[m]) byM[m]={{months:new Set(),total:0,cat:t.c,count:0}};
    byM[m].months.add(t.d.substring(0,7)); byM[m].total+=Math.abs(t.a); byM[m].count++;
  }});
  var rows=Object.entries(byM).filter(function(e){{return e[1].months.size>=2;}})
    .sort(function(a,b){{return b[1].total-a[1].total;}}).slice(0,40);
  if(!rows.length){{ el.innerHTML='<p style="padding:40px;text-align:center;color:#94a3b8">No recurring transactions detected.</p>'; return; }}
  Plotly.newPlot(el,[{{type:'table',columnwidth:[3,1,1,1.2,1.2,1.5],
    header:{{values:['Merchant','Transactions','Months','Avg Amount','Total Spent','Category'],
      fill:{{color:'#1e293b'}},font:{{color:'white',size:12}},align:'left',height:36}},
    cells:{{values:[
      rows.map(function(e){{return e[0].substring(0,50);}}),
      rows.map(function(e){{return e[1].count;}}),
      rows.map(function(e){{return e[1].months.size;}}),
      rows.map(function(e){{return _rptFmt(e[1].total/e[1].count);}}),
      rows.map(function(e){{return _rptFmt(e[1].total);}}),
      rows.map(function(e){{return e[1].cat;}}),
    ],fill:{{color:[rows.map(function(_,i){{return i%2===0?'#f8fafc':'white';}})]}},
    font:{{color:'#334155',size:12}},align:'left',height:28}},
  }}],_rptL({{height:Math.max(400,36+rows.length*30),margin:{{t:10,r:0,b:0,l:0}}}}),
  {{responsive:true,displayModeBar:false}});
}}

function _rptMerchants(txn, el, fromDate, toDate) {{
  var byM={{}},mCat={{}};
  _rptSpend(txn).forEach(function(t){{
    var m=_cleanDesc(t.desc);
    byM[m]=(byM[m]||0)+Math.abs(t.a); mCat[m]=t.c;
  }});
  var top=Object.entries(byM).sort(function(a,b){{return b[1]-a[1];}}).slice(0,20).reverse();
  if(!top.length){{ el.innerHTML='<p style="padding:40px;text-align:center;color:#94a3b8">No data.</p>'; return; }}
  var plt=Plotly.newPlot(el,[{{
    type:'bar',orientation:'h',
    x:top.map(function(e){{return e[1];}}),y:top.map(function(e){{return e[0];}}),
    marker:{{color:top.map(function(e){{return CAT_COLORS[mCat[e[0]]]||'#8D8D8D';}})}},
    text:top.map(function(e){{return _rptFmt(e[1]);}}),textposition:'outside',
    hovertemplate:'%{{y}}: %{{text}}<extra></extra>',
  }}],_rptL({{title:'Top 20 Merchants by Spend',
    xaxis:{{tickformat:'$,.0f',gridcolor:'#edf2f7',zeroline:false}},
    yaxis:{{automargin:true}},height:Math.max(500,top.length*28+80),
    margin:{{t:60,r:110,b:60,l:200}}
  }}),{{responsive:true,displayModeBar:false}});
  plt.then(function(){{
    el.on('plotly_click',function(d){{
      var merchant=d.points[0].y;
      var url='transactions.html?search='+encodeURIComponent(merchant);
      if(fromDate) url+='&from='+fromDate;
      if(toDate)   url+='&to='+toDate;
      window.location.href=url;
    }});
  }});
}}

function _rptHeatmap(txn, el) {{
  var DOW=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  var DNAMES=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  var months=[];var mSeen={{}};
  txn.forEach(function(t){{var mo=t.d.substring(0,7);if(!mSeen[mo]){{mSeen[mo]=1;months.push(mo);}}}});
  months.sort();
  var grid={{}};DOW.forEach(function(d){{grid[d]={{}};}});
  _rptSpend(txn).forEach(function(t){{
    var dt=new Date(t.d+'T00:00:00');
    var dow=DNAMES[dt.getDay()];
    var mo=t.d.substring(0,7);
    grid[dow][mo]=(grid[dow][mo]||0)+Math.abs(t.a);
  }});
  var z=DOW.map(function(d){{return months.map(function(m){{return grid[d][m]||0;}});}});
  var text=z.map(function(row){{return row.map(function(v){{return v>0?'$'+Math.round(v).toLocaleString():'';}})}});
  Plotly.newPlot(el,[{{type:'heatmap',z:z,x:months,y:DOW,text:text,texttemplate:'%{{text}}',
    colorscale:'Blues',showscale:false,
    hovertemplate:'<b>%{{y}}</b><br>%{{x}}: $%{{z:,.0f}}<extra></extra>',
  }}],_rptL({{
    title:'Spend by Day of Week — which days cost you most each month?',
    height:380,
    xaxis:{{type:'category',tickangle:-45}},
    yaxis:{{type:'category'}},
    margin:{{t:70,r:20,b:100,l:110}}
  }}),{{responsive:true,displayModeBar:false}});
}}

function _rptBusiness(txn, el) {{
  var biz=txn.filter(function(t){{return t.b&&t.a<0;}}).sort(function(a,b){{return b.d.localeCompare(a.d);}});
  if(!biz.length){{ el.innerHTML='<p style="padding:40px;text-align:center;color:#94a3b8">No business expenses flagged.</p>'; return; }}
  var total=biz.reduce(function(s,t){{return s+Math.abs(t.a);}},0);
  Plotly.newPlot(el,[{{type:'table',columnwidth:[1.2,3,1.2,1.5,1.5],
    header:{{values:['Date','Description','Amount','Category','Account'],
      fill:{{color:'#7c3503'}},font:{{color:'white',size:12}},align:'left',height:36}},
    cells:{{values:[
      biz.map(function(t){{return t.d;}}),
      biz.map(function(t){{return t.desc;}}),
      biz.map(function(t){{return _rptFmt(t.a);}}),
      biz.map(function(t){{return t.c;}}),
      biz.map(function(t){{return t.acc;}}),
    ],fill:{{color:[biz.map(function(_,i){{return i%2===0?'#fff3ee':'white';}})]}},
    font:{{color:'#334155',size:12}},align:'left',height:28}},
  }}],_rptL({{title:'Business Expenses — Total Reimbursable: '+_rptFmt(total),
    height:Math.max(400,36+biz.length*30),margin:{{t:60,r:0,b:0,l:0}}
  }}),{{responsive:true,displayModeBar:false}});
}}

function renderReportCharts(fromDate, toDate) {{
  var txn=TXN.filter(function(t){{
    if(fromDate&&t.d<fromDate) return false;
    if(toDate  &&t.d>toDate)   return false;
    return true;
  }});
  var el=function(id){{return document.getElementById('rpt-'+id);}};
  _rptIncome(txn,    el('income'));
  _rptSpendCat(txn,  el('spend'),    fromDate, toDate);
  _rptBudget(txn,    el('budget'));
  _rptTrends(txn,    el('trends'));
  _rptRecurring(txn, el('recurring'));
  _rptMerchants(txn, el('merchants'), fromDate, toDate);
  _rptHeatmap(txn,   el('heatmap'));
  _rptBusiness(txn,  el('business'));
  var status=document.getElementById('report-filter-status');
  if(status) status.textContent='';
}}

// ── Section and tab navigation ────────────────────────────────────────────────
const ALL_CHART_TABS = ['income','spend','budget','trends','recurring','merchants','heatmap','business'];
var _currentChartTab = 'income';
var _rptInitialised = false;

function _resizePlotlyCharts() {{
  setTimeout(function() {{
    window.dispatchEvent(new Event('resize'));
  }}, 150);
}}

function showSection(section) {{
  var alreadyInit = _rptInitialised;
  if (section === 'reports' && !_rptInitialised) {{
    _rptInitialised = true;
    setTimeout(function() {{
      var p = document.getElementById('report-period-sel');
      var sel = p ? p.value : '';
      var rng = sel ? periodDates(sel) : null;
      renderReportCharts(rng ? (rng.from||'') : '', rng ? (rng.to||'') : '');
    }}, 50);
  }}
  ['dashboard','reports'].forEach(function(s) {{
    var el = document.getElementById('nav-' + s);
    if (el) el.classList.toggle('active', s === section);
  }});
  var rBar   = document.getElementById('reports-tab-bar');
  var rfBar  = document.getElementById('reports-filter-bar');
  var rHelp  = document.getElementById('reports-help');
  if (rBar)  rBar.classList.toggle('visible', section === 'reports');
  if (rfBar) rfBar.style.display = section === 'reports' ? 'flex' : 'none';
  if (rHelp) rHelp.style.display = section === 'reports' ? 'block' : 'none';
  var overview = document.getElementById('tab-panel-overview');
  if (overview) overview.classList.toggle('active', section === 'dashboard');
  ALL_CHART_TABS.forEach(function(id) {{
    var panel = document.getElementById('tab-panel-' + id);
    if (panel) panel.classList.toggle('active', section === 'reports' && id === _currentChartTab);
  }});
  if (section === 'reports' && alreadyInit) _resizePlotlyCharts();
  if (section === 'dashboard') {{
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }} else {{
    history.replaceState(null, '', '#' + section);
  }}
}}

function showTab(name) {{
  _currentChartTab = name;
  var rBar = document.getElementById('reports-tab-bar');
  if (!rBar || !rBar.classList.contains('visible')) showSection('reports');
  ALL_CHART_TABS.forEach(function(id) {{
    var btn   = document.getElementById('tab-btn-'  + id);
    var panel = document.getElementById('tab-panel-' + id);
    if (btn)   btn.classList.toggle('active',   id === name);
    if (panel) panel.classList.toggle('active', id === name);
  }});
  var chartEl = document.getElementById('rpt-' + name);
  if (chartEl) chartEl.style.opacity = '0';
  setTimeout(function() {{
    window.dispatchEvent(new Event('resize'));
    setTimeout(function() {{
      if (chartEl) {{ chartEl.style.transition = 'none'; chartEl.style.opacity = '1'; }}
    }}, 80);
  }}, 20);
  history.replaceState(null, '', '#' + name);
}}

// ── Report period filter ─────────────────────────────────────────────────────
function onReportPeriodChange() {{
  var p  = document.getElementById('report-period-sel').value;
  var cd = document.getElementById('report-custom-dates');
  if (cd) cd.style.display = p === 'custom' ? 'flex' : 'none';
  if (p !== 'custom') applyReportFilter();
}}

function applyReportFilter() {{
  var p = document.getElementById('report-period-sel').value;
  var rng = p === 'custom' ? {{
    from: document.getElementById('report-from-date').value,
    to:   document.getElementById('report-to-date').value,
  }} : periodDates(p);
  var fromDate = rng ? (rng.from || '') : '';
  var toDate   = rng ? (rng.to   || '') : '';
  renderReportCharts(fromDate, toDate);
}}

(function() {{
  var hash = window.location.hash.replace('#', '');
  if (ALL_CHART_TABS.indexOf(hash) !== -1) {{
    _currentChartTab = hash;
    showSection('reports');
    ALL_CHART_TABS.forEach(function(id) {{
      var btn   = document.getElementById('tab-btn-'  + id);
      var panel = document.getElementById('tab-panel-' + id);
      if (btn)   btn.classList.toggle('active',   id === hash);
      if (panel) panel.classList.toggle('active', id === hash);
    }});
  }} else if (hash === 'reports') {{
    showSection('reports');
  }}
}})();

// ── Init ─────────────────────────────────────────────────────────────────────
(function() {{
  const initRange = periodDates('this_fy');
  if (initRange) {{ currentFrom = initRange.from; currentTo = initRange.to; }}
  renderDashboard();
}})();
</script>
{_IMPORT_JS}
</body>
</html>"""

    # Build and inject chart tab panels (client-side rendering — no Python chart generation)
    _chart_tabs = ["income","spend","budget","trends","recurring","merchants","heatmap","business"]
    _chart_panels = "".join(
        f'<div id="tab-panel-{tid}" class="tab-panel">'
        f'<div class="chart-wrap" id="rpt-{tid}" style="min-height:420px"></div>'
        f'</div>\n'
        for tid in _chart_tabs
    )
    page_html = page_html.replace("<!-- __CHART_PANELS__ -->", _chart_panels)

    try:
        output_path.write_text(page_html, encoding="utf-8")
    except OSError as exc:
        logger.error(f"  ERROR: could not write dashboard -> {output_path}: {exc}")
        return
    logger.info(f"  Dashboard -> {output_path}")


# ── Review page ───────────────────────────────────────────────────────────────

def prepare_review_data(df: pd.DataFrame, config: dict) -> dict:
    """Group Miscellaneous transactions by description and build review row HTML.

    Returns: {has_data, rows_html, num_groups, num_txns, cat_options}
    """
    import json as _json
    from src.enricher import find_paypal_hints

    misc = df[df["category"] == "Miscellaneous"].copy()
    categories = config.get("categories", list(CATEGORY_COLORS.keys()))
    cat_options = '<option value="" disabled selected>-- category --</option>\n' + _cat_optgroups(categories)

    if misc.empty:
        return {"has_data": False, "cat_options": cat_options, "num_groups": 0, "num_txns": 0, "rows_html": ""}

    paypal_hints = find_paypal_hints(df)
    misc["desc_norm"] = misc["description"].str.upper().str.strip()

    group_data: list[dict] = []
    for desc_norm, grp in misc.groupby("desc_norm", sort=False):
        grp_s = grp.sort_values("date", ascending=False)
        first = grp_s.iloc[0]
        group_data.append({
            "count": len(grp_s),
            "txn_ids": grp_s["txn_id"].tolist(),
            "total_abs": float(grp["amount"].abs().sum()),
            "min_date": grp["date"].min(),
            "max_date": grp["date"].max(),
            "first_amount": float(first.get("amount", 0)),
            "first_desc": str(first.get("description", "")),
            "rows": grp_s,
        })
    group_data.sort(key=lambda g: -g["count"])

    def _date_range(mn: "pd.Timestamp", mx: "pd.Timestamp") -> str:
        if mn == mx:
            return mn.strftime("%d %b %Y")
        if mn.year == mx.year and mn.month == mx.month:
            return mn.strftime("%b %Y")
        if mn.year == mx.year:
            return f"{mn.strftime('%b')}–{mx.strftime('%b %Y')}"
        return f"{mn.strftime('%b %Y')}–{mx.strftime('%b %Y')}"

    rows_html: list[str] = []
    for g in group_data:
        desc_esc      = html.escape(g["first_desc"][:80])
        desc_title    = html.escape(g["first_desc"])
        txn_ids_json  = html.escape(_json.dumps(g["txn_ids"]))
        date_rng      = _date_range(g["min_date"], g["max_date"])
        min_iso       = g["min_date"].strftime("%Y-%m-%d")
        max_iso       = g["max_date"].strftime("%Y-%m-%d")
        total_fmt     = f"${g['total_abs']:,.2f}"
        badge_color   = "#E63946" if g["count"] > 5 else "#457B9D"

        detail_rows: list[str] = []
        for _, irow in g["rows"].iterrows():
            i_txn_id  = str(irow.get("txn_id", ""))
            i_date    = irow["date"].strftime("%d %b %Y") if hasattr(irow["date"], "strftime") else str(irow["date"])[:10]
            i_amount  = float(irow.get("amount", 0))
            i_amt_fmt = f"-${abs(i_amount):,.2f}" if i_amount < 0 else f"+${i_amount:,.2f}"
            i_color   = "#E63946" if i_amount < 0 else "#00BB77"
            i_acct    = html.escape(str(irow.get("account", "")))
            i_note    = html.escape(str(irow.get("user_note", "") or ""))

            hint = paypal_hints.get(i_txn_id)
            hint_html = ""
            if hint:
                days_label = f"{hint['days_off']}d off" if hint["days_off"] > 0 else "same day"
                hint_html = (
                    f'<br><span class="paypal-hint">'
                    f'&#128279; PayPal: <strong>{html.escape(hint["merchant"])}</strong>'
                    f' ${hint["amount"]:,.2f} ({days_label})</span>'
                )

            detail_rows.append(
                f'<tr class="det-row" data-txnid="{i_txn_id}">'
                f'<td style="color:#888;white-space:nowrap;padding:5px 10px">{i_date}</td>'
                f'<td style="color:#888;font-size:.8rem;padding:5px 10px">{i_acct}</td>'
                f'<td style="color:{i_color};font-weight:600;text-align:right;white-space:nowrap;padding:5px 10px">{i_amt_fmt}{hint_html}</td>'
                f'<td style="padding:5px 10px">'
                f'<input type="text" class="note-inp" value="{i_note}" placeholder="Note…" oninput="scheduleNoteSave(this)"'
                f' style="width:180px;padding:3px 6px;border:1px solid #dde;border-radius:5px;font-size:.8rem">'
                f'</td>'
                f'<td style="width:30px;text-align:center;padding:5px 8px">'
                f'<span class="save-status" style="font-size:.75rem"></span></td>'
                f'</tr>'
            )

        rows_html.append(
            f'<tr class="grp-row" data-desc="{desc_esc}" data-amount="{g["first_amount"]}"'
            f' data-txnids="{txn_ids_json}" data-min="{min_iso}" data-max="{max_iso}">'
            f'<td class="cnt-cell"><span class="cnt-badge" style="background:{badge_color}">{g["count"]}</span></td>'
            f'<td class="g-desc" title="{desc_title}">{desc_esc}</td>'
            f'<td class="g-dates">{date_rng}</td>'
            f'<td class="g-total">{total_fmt}</td>'
            f'<td><select class="cat-sel" onchange="onCatChange(this)">{cat_options}</select></td>'
            f'<td><select class="sub-sel" onchange="autoSaveGroup(this)"><option value="">&#8212; None &#8212;</option></select></td>'
            f'<td style="width:36px;text-align:center"><span class="save-status" style="font-size:.8rem"></span></td>'
            f'<td><button class="expand-btn" onclick="toggleGroup(this)" title="Show individual transactions">&#9658;</button></td>'
            f'</tr>'
            f'<tr class="grp-detail" style="display:none">'
            f'<td colspan="8" style="padding:0 8px 8px 40px;background:#f8fafc">'
            f'<table style="width:100%;border-collapse:collapse">'
            f'<thead><tr style="background:#e9eef4">'
            f'<th style="padding:4px 10px;text-align:left;font-size:.78rem;font-weight:600;color:#555">Date</th>'
            f'<th style="padding:4px 10px;text-align:left;font-size:.78rem;font-weight:600;color:#555">Account</th>'
            f'<th style="padding:4px 10px;text-align:right;font-size:.78rem;font-weight:600;color:#555">Amount</th>'
            f'<th style="padding:4px 10px;text-align:left;font-size:.78rem;font-weight:600;color:#555">Note</th>'
            f'<th></th>'
            f'</tr></thead>'
            f'<tbody>{"".join(detail_rows)}</tbody>'
            f'</table></td></tr>'
        )

    return {
        "has_data":   True,
        "rows_html":  "\n".join(rows_html),
        "num_groups": len(group_data),
        "num_txns":   len(misc),
        "cat_options": cat_options,
    }


def generate_review_page(df: pd.DataFrame, config: dict, output_dir: Path) -> None:
    """Generate reports/review.html — grouped-by-description view for Miscellaneous."""
    import json as _json
    output_path = output_dir / "review.html"

    misc = df[df["category"] == "Miscellaneous"].copy()

    categories = config.get("categories", list(CATEGORY_COLORS.keys()))
    cat_options = '<option value="" disabled selected>-- category --</option>\n' + _cat_optgroups(categories)

    if misc.empty:
        try:
            output_path.write_text(
                "<!DOCTYPE html><html><body>"
                "<h2 style='font-family:sans-serif;padding:40px'>"
                "No Miscellaneous transactions to review.</h2>"
                "</body></html>",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error(f"  ERROR: could not write review page -> {output_path}: {exc}")
            return
        logger.info(f"  Review page -> {output_path} (0 items)")
        return

    from src.enricher import find_paypal_hints
    paypal_hints = find_paypal_hints(df)

    # ── Group by normalised description ──────────────────────────────────────
    misc["desc_norm"] = misc["description"].str.upper().str.strip()

    group_data: list[dict] = []
    for desc_norm, grp in misc.groupby("desc_norm", sort=False):
        grp_s = grp.sort_values("date", ascending=False)
        first = grp_s.iloc[0]
        group_data.append({
            "desc_norm": desc_norm,
            "count": len(grp_s),
            "txn_ids": grp_s["txn_id"].tolist(),
            "total_abs": float(grp["amount"].abs().sum()),
            "min_date": grp["date"].min(),
            "max_date": grp["date"].max(),
            "first_txn_id": str(first["txn_id"]),
            "first_amount": float(first.get("amount", 0)),
            "first_desc": str(first.get("description", "")),
            "rows": grp_s,
        })
    group_data.sort(key=lambda g: -g["count"])

    num_groups = len(group_data)
    num_txns = len(misc)

    def _date_range(mn: pd.Timestamp, mx: pd.Timestamp) -> str:
        if mn == mx:
            return mn.strftime("%d %b %Y")
        if mn.year == mx.year and mn.month == mx.month:
            return mn.strftime("%b %Y")
        if mn.year == mx.year:
            return f"{mn.strftime('%b')}–{mx.strftime('%b %Y')}"
        return f"{mn.strftime('%b %Y')}–{mx.strftime('%b %Y')}"

    # ── Build HTML rows ───────────────────────────────────────────────────────
    rows_html: list[str] = []
    for g in group_data:
        desc_esc = html.escape(g["first_desc"][:80])
        desc_title = html.escape(g["first_desc"])
        txn_ids_json = html.escape(_json.dumps(g["txn_ids"]))
        date_rng = _date_range(g["min_date"], g["max_date"])
        min_iso = g["min_date"].strftime("%Y-%m-%d")
        max_iso = g["max_date"].strftime("%Y-%m-%d")
        total_fmt = f"${g['total_abs']:,.2f}"
        badge_color = "#E63946" if g["count"] > 5 else "#457B9D"

        # Individual detail rows (collapsed by default)
        detail_rows: list[str] = []
        for _, irow in g["rows"].iterrows():
            i_txn_id = str(irow.get("txn_id", ""))
            i_date = irow["date"].strftime("%d %b %Y") if hasattr(irow["date"], "strftime") else str(irow["date"])[:10]
            i_amount = float(irow.get("amount", 0))
            i_amt_fmt = f"-${abs(i_amount):,.2f}" if i_amount < 0 else f"+${i_amount:,.2f}"
            i_color = "#E63946" if i_amount < 0 else "#00BB77"
            i_acct = html.escape(str(irow.get("account", "")))
            i_note = html.escape(str(irow.get("user_note", "") or ""))

            hint = paypal_hints.get(i_txn_id)
            hint_html = ""
            if hint:
                days_label = f"{hint['days_off']}d off" if hint["days_off"] > 0 else "same day"
                hint_html = (
                    f'<br><span class="paypal-hint">'
                    f'&#128279; PayPal: <strong>{html.escape(hint["merchant"])}</strong>'
                    f' ${hint["amount"]:,.2f} ({days_label})</span>'
                )

            detail_rows.append(
                f'<tr class="det-row" data-txnid="{i_txn_id}">'
                f'<td style="color:#888;white-space:nowrap;padding:5px 10px">{i_date}</td>'
                f'<td style="color:#888;font-size:.8rem;padding:5px 10px">{i_acct}</td>'
                f'<td style="color:{i_color};font-weight:600;text-align:right;white-space:nowrap;padding:5px 10px">{i_amt_fmt}{hint_html}</td>'
                f'<td style="padding:5px 10px">'
                f'<input type="text" class="note-inp" value="{i_note}" placeholder="Note…" oninput="scheduleNoteSave(this)"'
                f' style="width:180px;padding:3px 6px;border:1px solid #dde;border-radius:5px;font-size:.8rem">'
                f'</td>'
                f'<td style="width:30px;text-align:center;padding:5px 8px"><span class="save-status" style="font-size:.75rem"></span></td>'
                f'</tr>'
            )

        detail_html = "".join(detail_rows)

        rows_html.append(f"""<tr class="grp-row" data-desc="{desc_esc}" data-amount="{g['first_amount']}"
    data-txnids="{txn_ids_json}" data-min="{min_iso}" data-max="{max_iso}">
  <td class="cnt-cell"><span class="cnt-badge" style="background:{badge_color}">{g['count']}</span></td>
  <td class="g-desc" title="{desc_title}">{desc_esc}</td>
  <td class="g-dates">{date_rng}</td>
  <td class="g-total">{total_fmt}</td>
  <td><select class="cat-sel" onchange="onCatChange(this)">{cat_options}</select></td>
  <td><select class="sub-sel" onchange="autoSaveGroup(this)"><option value="">— None —</option></select></td>
  <td style="width:36px;text-align:center"><span class="save-status" style="font-size:.8rem"></span></td>
  <td><button class="expand-btn" onclick="toggleGroup(this)" title="Show individual transactions">&#9658;</button></td>
</tr>
<tr class="grp-detail" style="display:none">
  <td colspan="8" style="padding:0 8px 8px 40px;background:#f8fafc">
    <table style="width:100%;border-collapse:collapse">
      <thead><tr style="background:#e9eef4">
        <th style="padding:4px 10px;text-align:left;font-size:.78rem;font-weight:600;color:#555">Date</th>
        <th style="padding:4px 10px;text-align:left;font-size:.78rem;font-weight:600;color:#555">Account</th>
        <th style="padding:4px 10px;text-align:right;font-size:.78rem;font-weight:600;color:#555">Amount</th>
        <th style="padding:4px 10px;text-align:left;font-size:.78rem;font-weight:600;color:#555">Note</th>
        <th></th>
      </tr></thead>
      <tbody>{detail_html}</tbody>
    </table>
  </td>
</tr>""")

    rows_joined = "\n".join(rows_html)

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review: {num_groups} merchants ({num_txns} transactions)</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f0f4f8;margin:0;padding:20px 24px;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#264653 0%,#2a7b8e 100%);color:white;
         padding:24px 32px;border-radius:12px;margin-bottom:20px}}
.header h1{{margin:0 0 4px;font-size:1.5rem}}
.header p{{margin:0;opacity:.75;font-size:.9rem}}
.toolbar{{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap}}
.toolbar input[type=text]{{padding:8px 12px;border:1px solid #ccd;border-radius:8px;
                           font-size:.9rem;width:280px}}
.btn{{padding:8px 16px;border:none;border-radius:8px;cursor:pointer;
      font-size:.85rem;font-weight:600}}
.count{{font-size:.85rem;color:#666;margin-left:auto}}
.paypal-hint{{display:block;font-size:.75rem;color:#7c4d00;background:#fff3cd;
              border:1px solid #ffc107;border-radius:4px;padding:2px 5px;margin-top:2px;line-height:1.4}}
.card{{background:white;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.07);overflow:hidden}}
table{{width:100%;border-collapse:collapse;font-size:.88rem}}
thead tr{{background:#264653;color:white}}
th{{padding:10px 12px;text-align:left;font-weight:600;white-space:nowrap}}
td{{padding:9px 12px;border-bottom:1px solid #eef;vertical-align:middle}}
.grp-row:hover td{{background:#f0f8f6}}
.grp-detail td{{border-bottom:1px solid #eef}}
.cnt-cell{{width:52px;text-align:center}}
.cnt-badge{{display:inline-block;padding:3px 9px;border-radius:12px;color:white;
            font-size:.78rem;font-weight:700;white-space:nowrap}}
.g-desc{{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}}
.g-dates{{white-space:nowrap;color:#777;font-size:.82rem;width:130px}}
.g-total{{white-space:nowrap;font-weight:600;color:#555;width:100px;text-align:right}}
select.cat-sel{{padding:5px 8px;border:1px solid #ccd;border-radius:6px;font-size:.85rem;width:160px}}
select.sub-sel{{padding:5px 8px;border:1px solid #ccd;border-radius:6px;font-size:.85rem;width:140px;color:#475569}}
.expand-btn{{padding:4px 8px;background:none;border:1px solid #ccd;border-radius:5px;
             cursor:pointer;color:#555;font-size:.8rem}}
.expand-btn:hover{{background:#f0f4f8}}
.grp-row{{cursor:default}}
.grp-row:hover td{{background:#f0f8f6}}
.grp-detail td{{padding:0;border-bottom:none}}
.det-row:hover td{{background:#f8fafc}}
.note-inp{{width:180px;padding:3px 6px;border:1px solid #dde;border-radius:5px;font-size:.8rem;
           color:#334;background:white}}
.note-inp:focus{{outline:none;border-color:#2a9d8f;box-shadow:0 0 0 2px rgba(42,157,143,.15)}}
.save-status{{font-size:.75rem;min-width:28px;display:inline-block;text-align:center}}
.saved-banner{{display:none;background:#00BB77;color:white;padding:12px 20px;
              border-radius:8px;margin-bottom:16px;font-weight:600}}
.site-nav{{background:#1d3540;padding:0 24px;display:flex;gap:4px;
           border-radius:10px;margin-bottom:20px}}
.nav-link{{color:rgba(255,255,255,.65);text-decoration:none;padding:12px 18px;
           font-size:.88rem;font-weight:600;border-bottom:3px solid transparent}}
.nav-link:hover{{color:white}}
.nav-link.active{{color:white;border-bottom-color:#2a9d8f}}
.tab-bar{{display:flex;gap:2px;background:#1d3540;border-radius:10px;
          padding:6px 12px;margin-bottom:20px;flex-wrap:wrap;align-items:center}}
.tab-btn{{padding:8px 16px;border:none;border-radius:7px;background:transparent;
          color:rgba(255,255,255,.55);font-size:.82rem;font-weight:600;cursor:pointer;
          border-bottom:3px solid transparent;text-decoration:none;display:inline-block}}
.tab-btn.active{{background:rgba(42,157,143,.15);color:#2a9d8f;border-bottom-color:#2a9d8f}}
.tab-btn:hover:not(.active){{color:white}}
{_IMPORT_CSS}
</style>
</head>
<body>
{_build_nav_html("review")}
<div class="header">
  <h1>Review: {num_groups} merchants &nbsp;<span style="font-size:1rem;opacity:.75">({num_txns} transactions)</span></h1>
  <p>Transactions auto-categorised as <em>Miscellaneous</em>, grouped by merchant. Assign a category to update the entire group at once.</p>
</div>
{_help_box(
    "How to use this page",
    "Each row is a unique merchant. Fixing one row updates every transaction from that merchant.",
    [
        "The <strong>#</strong> column shows how many uncategorised transactions share this merchant name.",
        "Select a category from the dropdown — saves instantly and applies to <em>all</em> transactions in the group.",
        "Click &#9658; to expand a group and see individual transactions, add notes, or check PayPal hints.",
        "&#9889; <strong>Seed Rules from History</strong> scans previously categorised transactions and auto-creates merchant rules so future imports categorise them automatically.",
        "Changes are saved instantly. Reports refresh automatically the next time a file is uploaded.",
    ],
    legend=[
        (">5", "#E63946", "more than 5 occurrences &mdash; high-frequency merchant, worth fixing first"),
        ("&le;5", "#457B9D", "5 or fewer occurrences"),
    ]
)}
<div class="saved-banner" id="banner"></div>
<div class="toolbar">
  <input type="text" id="filter" placeholder="Filter by merchant description...">
  <button class="btn" onclick="seedRules(this)"
          style="background:#f39c12;color:white" title="Auto-add merchant rules from all categorised history">&#9889; Seed Rules from History</button>
  <span class="count" id="count">{num_groups} merchants</span>
  <button class="btn" onclick="openHistory()" style="background:#457B9D;color:white">&#128337; History</button>
</div>
<!-- History / Undo modal -->
<div id="history-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;align-items:center;justify-content:center"
     onclick="if(event.target===this)this.style.display='none'">
  <div style="background:white;border-radius:14px;padding:28px 32px;width:680px;max-width:95vw;max-height:80vh;overflow-y:auto">
    <h3 style="margin:0 0 16px;color:#264653">Override History</h3>
    <p style="margin:0 0 16px;font-size:.85rem;color:#555">Recent changes — click Undo to restore previous values.</p>
    <div id="history-list"><p style="color:#888">Loading…</p></div>
    <div style="margin-top:16px;text-align:right">
      <button onclick="document.getElementById('history-modal').style.display='none'"
              style="padding:8px 20px;background:#264653;color:white;border:none;border-radius:8px;cursor:pointer;font-size:.9rem">Close</button>
    </div>
  </div>
</div>
<div class="card">
<table>
<thead><tr>
  <th style="width:52px;text-align:center">#</th>
  <th>Merchant / Description</th>
  <th style="width:130px">Date range</th>
  <th style="width:100px;text-align:right">Total</th>
  <th style="width:160px">Assign category</th>
  <th style="width:140px">Sub-category</th>
  <th style="width:36px"></th>
  <th style="width:52px"></th>
</tr></thead>
<tbody id="tbody">
{rows_joined}
</tbody>
</table>
<div id="review-pagination" style="display:flex;align-items:center;gap:12px;padding:12px 16px;font-size:.88rem;color:#64748b"></div>
</div>
<script>
const filterEl = document.getElementById('filter');
const countEl  = document.getElementById('count');

// ── SUBCATS (fetched from API — same source as Merchant Rules page) ───────────
var SUBCATS = {{}};
fetch('/api/subcats').then(r => r.json()).then(d => {{
  SUBCATS = d;
  // Populate sub-sel dropdowns for any pre-selected categories
  document.querySelectorAll('#tbody .grp-row').forEach(tr => {{
    const cat = tr.querySelector('.cat-sel').value;
    if (cat) _refreshSubSel(tr.querySelector('.sub-sel'), cat, '');
  }});
}});

function _refreshSubSel(sel, cat, current) {{
  const list = SUBCATS[cat] || [];
  const known = current && !list.includes(current) ? [current, ...list] : list;
  sel.innerHTML = '<option value="">— None —</option>' +
    known.map(s => `<option value="${{s}}"${{s === current ? ' selected' : ''}}>${{s}}</option>`).join('');
}}

function onCatChange(catSel) {{
  const tr = catSel.closest('.grp-row');
  const subSel = tr.querySelector('.sub-sel');
  _refreshSubSel(subSel, catSel.value, '');
  autoSaveGroup(catSel);
}}

// ── Pagination ────────────────────────────────────────────────────────────────
var _REVIEW_PAGE = 50;
var _reviewPage  = 1;
var _allGrpRows  = [];

function _buildRowList() {{
  _allGrpRows = [];
  document.querySelectorAll('#tbody .grp-row').forEach(tr => _allGrpRows.push(tr));
}}

function _renderReviewPage() {{
  const q = filterEl.value.toLowerCase();
  const matched = _allGrpRows.filter(tr => !q || tr.dataset.desc.toLowerCase().includes(q));
  const total = matched.length;
  const totalPages = Math.max(1, Math.ceil(total / _REVIEW_PAGE));
  _reviewPage = Math.min(_reviewPage, totalPages);
  const start = (_reviewPage - 1) * _REVIEW_PAGE;

  _allGrpRows.forEach(tr => {{
    const det = tr.nextElementSibling;
    tr.style.display = 'none';
    if (det && det.classList.contains('grp-detail')) det.style.display = 'none';
  }});
  matched.slice(start, start + _REVIEW_PAGE).forEach(tr => {{ tr.style.display = ''; }});

  countEl.textContent = total + ' merchants' +
    (totalPages > 1 ? ' — page ' + _reviewPage + ' of ' + totalPages : '');

  const pag = document.getElementById('review-pagination');
  if (totalPages > 1) {{
    pag.innerHTML =
      '<button onclick="_goReviewPage(' + (_reviewPage-1) + ')" ' + (_reviewPage<=1?'disabled':'') +
        ' style="padding:4px 12px;border:1px solid #cbd5e1;border-radius:6px;cursor:pointer">&#8592; Prev</button>' +
      '<span style="color:#64748b;font-size:.88rem">Page ' + _reviewPage + ' of ' + totalPages + ' (' + total + ' merchants)</span>' +
      '<button onclick="_goReviewPage(' + (_reviewPage+1) + ')" ' + (_reviewPage>=totalPages?'disabled':'') +
        ' style="padding:4px 12px;border:1px solid #cbd5e1;border-radius:6px;cursor:pointer">Next &#8594;</button>';
  }} else {{ pag.innerHTML = ''; }}
}}

function _goReviewPage(p) {{ _reviewPage = p; _renderReviewPage(); }}

function filterGroups() {{
  _reviewPage = 1;
  _renderReviewPage();
}}
filterEl.addEventListener('input', filterGroups);

document.addEventListener('DOMContentLoaded', function() {{
  _buildRowList();
  _renderReviewPage();
}});

function toggleGroup(btn) {{
  const grpRow = btn.closest('.grp-row');
  const detRow = grpRow.nextElementSibling;
  if (!detRow || !detRow.classList.contains('grp-detail')) return;
  const opening = detRow.style.display === 'none';
  detRow.style.display = opening ? '' : 'none';
  btn.innerHTML = opening ? '&#9660;' : '&#9658;';
}}

const _noteTimers = {{}};

function _setStatus(tr, msg, ok) {{
  const sp = tr.querySelector('.save-status');
  if (!sp) return;
  sp.textContent = msg;
  sp.style.color = ok ? '#22a86e' : '#E63946';
}}

function autoSaveGroup(sel) {{
  const tr     = sel.closest('.grp-row');
  const cat    = tr.querySelector('.cat-sel').value;
  const sub    = tr.querySelector('.sub-sel').value;
  if (!cat) return;
  const desc   = tr.dataset.desc;
  const amount = parseFloat(tr.dataset.amount);
  const ids    = JSON.parse(tr.dataset.txnids.replace(/&quot;/g,'"'));
  _setStatus(tr, '…', true);

  fetch('/api/apply-override', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify([{{
      txn_id: ids[0], category: cat, sub_category: sub,
      apply_to_all: true, description: desc, amount: amount
    }}])
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      _setStatus(tr, '✓', true);
      tr.style.opacity = '0.4';
      const det = tr.nextElementSibling;
      if (det) det.style.display = 'none';
      document.getElementById('banner').textContent =
        'Saved ' + (d.master_updated || ids.length) + ' transactions. Reports refresh automatically on next import.';
      document.getElementById('banner').style.display = 'block';
    }} else {{ _setStatus(tr, '✗', false); }}
  }})
  .catch(() => _setStatus(tr, '✗', false));
}}

function scheduleNoteSave(inp) {{
  const tr    = inp.closest('tr');
  const txnId = tr.dataset.txnid;
  clearTimeout(_noteTimers[txnId]);
  _setStatus(tr, '…', true);
  _noteTimers[txnId] = setTimeout(() => {{
    fetch('/api/save-note', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ txn_id: txnId, note: inp.value.trim() }})
    }}).then(r => r.json())
      .then(d => _setStatus(tr, d.ok ? '✓' : '✗', !!d.ok))
      .catch(() => _setStatus(tr, '✗', false));
  }}, 600);
}}

function seedRules(btn) {{
  btn.disabled = true;
  btn.textContent = 'Seeding…';
  fetch('/api/seed-merchant-rules', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ threshold: 0.8, min_count: 2 }})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      const msg = d.added + ' merchant rule(s) added, ' + d.backfilled + ' transaction(s) updated.';
      document.getElementById('banner').textContent = msg + (d.backfilled ? ' Reports refresh automatically on next import.' : '');
      document.getElementById('banner').style.display = 'block';
      btn.textContent = d.added ? '✓ ' + d.added + ' rules added' : '✓ No new rules found';
    }} else {{
      btn.textContent = '✗ Error';
      alert('Seed failed: ' + (d.error || 'unknown'));
    }}
    btn.disabled = false;
  }})
  .catch(e => {{ btn.textContent = '✗ Error'; btn.disabled = false; alert(e); }});
}}

function escHtml(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function _merchantPrefix(desc) {{
  // Extract a clean merchant name by stripping trailing reference numbers (4+ digits)
  // and keeping up to 4 words, max 40 chars.
  const words = String(desc).trim().split(/\\s+/);
  const keep = [];
  for (const w of words) {{
    if (/^\\d{{4,}}$/.test(w)) break;
    keep.push(w);
    if (keep.length >= 4) break;
  }}
  return keep.join(' ').toUpperCase().substring(0, 40);
}}

function onModalAllChange() {{
  const show = document.getElementById('modal-all').checked;
  document.getElementById('modal-merchant-row').style.display = show ? 'block' : 'none';
}}

function openHistory() {{
  const modal = document.getElementById('history-modal');
  const list  = document.getElementById('history-list');
  modal.style.display = 'flex';
  list.innerHTML = '<p style="color:#888">Loading…</p>';
  fetch('/api/override-history?limit=15')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok || !data.batches || !data.batches.length) {{
        list.innerHTML = '<p style="color:#888;padding:8px 0">No override history yet.</p>';
        return;
      }}
      list.innerHTML = data.batches.map(b => `
        <div style="border:1px solid #eef;border-radius:8px;padding:12px 16px;margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:4px">
            <strong style="font-size:.88rem">${{escHtml(b.summary)}}</strong>
            <span style="font-size:.75rem;color:#888;white-space:nowrap">${{(b.applied_at||b.timestamp||'').replace('T',' ').slice(0,16)}}</span>
          </div>
          <div style="font-size:.8rem;color:#777;margin-bottom:8px">${{b.changes.length}} change(s)</div>
          <button onclick="undoBatch('${{b.batch_id}}',this)"
                  style="padding:5px 14px;background:#E63946;color:white;border:none;border-radius:6px;cursor:pointer;font-size:.82rem;font-weight:600">
            &#x21A9; Undo
          </button>
        </div>`).join('');
    }})
    .catch(e => {{ list.innerHTML = '<p style="color:#E63946">Error: ' + e + '</p>'; }});
}}

function undoBatch(batchId, btn) {{
  if (!confirm('Undo this batch? This will restore the previous categories/notes.')) return;
  btn.disabled = true; btn.textContent = 'Undoing…';
  fetch('/api/undo-override', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{batch_id: batchId}})
  }})
  .then(r => r.json())
  .then(data => {{
    if (data.ok) {{
      btn.textContent = '✓ Undone'; btn.style.background = '#888';
      document.getElementById('banner').textContent =
        'Undone (' + data.undone + ' row(s) restored). Reports refresh automatically on next import.';
      document.getElementById('banner').style.display = 'block';
    }} else {{
      btn.disabled = false; btn.textContent = '↩ Undo';
      alert('Undo failed: ' + (data.error || 'Unknown'));
    }}
  }})
  .catch(e => {{ btn.disabled = false; btn.textContent = '↩ Undo'; alert('Network error: ' + e); }});
}}
</script>
{_IMPORT_JS}
</body>
</html>"""

    try:
        output_path.write_text(page_html, encoding="utf-8")
    except OSError as exc:
        logger.error(f"  ERROR: could not write review page -> {output_path}: {exc}")
        return
    logger.info(f"  Review page -> {output_path} ({num_groups} groups, {num_txns} items)")


# ── Transactions page ─────────────────────────────────────────────────────────

def prepare_transactions_data(df: pd.DataFrame, config: dict) -> dict:
    """Build all data needed to render the transactions Jinja2 template or static page.

    Returns a dict with: records_json, today_str, fy_options_json,
    cat_options_html, cat_options_sel, acct_options_html, period_opts_html,
    period_dates_js, cat_colors_json, txn_count.
    """
    import json as _json
    from datetime import date as _date

    categories = config.get("categories", list(CATEGORY_COLORS.keys()))
    cat_options_html = _cat_optgroups(categories, include_all=True)
    cat_options_sel = _cat_optgroups(categories)

    from src.payin4_detector import load_payin4_groups
    payin4_lookup = {g["purchase_txn_id"]: g for g in load_payin4_groups(config)}

    # Reload with split parents (excluded from summary load by default)
    try:
        from src.db import load_transactions as _lt
        df_all = _lt(config, include_split_parents=True)
        if not df_all.empty:
            df = df_all
    except Exception:
        pass

    if df.empty:
        return {
            "records_json": "[]",
            "today_str": _date.today().strftime("%Y-%m-%d"),
            "fy_options_json": "[]",
            "cat_options_html": cat_options_html,
            "cat_options_sel": cat_options_sel,
            "acct_options_html": "<option value=''>All accounts</option>",
            "period_opts_html": _period_select_options(""),
            "period_dates_js": _period_dates_js(),
            "cat_colors_json": _json.dumps(CATEGORY_COLORS),
            "txn_count": 0,
        }

    display = df.copy()
    display["date_str"]     = display["date"].dt.strftime("%Y-%m-%d")
    display["date_display"] = display["date"].dt.strftime("%x")
    display["amount_str"] = display["amount"].apply(
        lambda v: f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"
    )

    records = []
    for _, row in display.iterrows():
        txn_id = str(row.get("txn_id", ""))
        rec = {
            "txn_id": txn_id,
            "date": str(row.get("date_str", "")),
            "date_display": str(row.get("date_display", "")),
            "description": str(row.get("description", "")),
            "amount": float(row.get("amount", 0)),
            "amount_str": str(row.get("amount_str", "")),
            "category": str(row.get("category", "")),
            "account": str(row.get("account", "")),
            "is_business": bool(row.get("is_business", False)),
            "is_tax_deductible": bool(row.get("is_tax_deductible", False)),
            "is_gst_claimable": bool(row.get("is_gst_claimable", False)),
            "source_file": str(row.get("source_file", "")),
            "user_note": str(row.get("user_note", "")),
            "note": str(row.get("note", "")),
            "tags": str(row.get("tags", "") or ""),
            "payin4": payin4_lookup.get(txn_id),
            "parent_txn_id": str(row.get("parent_txn_id") or ""),
            "is_split_parent": int(row.get("is_split_parent") or 0),
            "split_children": [],
        }
        records.append(rec)

    _children_by_parent: dict = {}
    for _r in records:
        if _r["parent_txn_id"]:
            _children_by_parent.setdefault(_r["parent_txn_id"], []).append(_r)
    for _r in records:
        if _r["is_split_parent"]:
            _r["split_children"] = _children_by_parent.get(_r["txn_id"], [])

    records_json = _json.dumps(records, ensure_ascii=False)

    accounts = sorted(df["account"].dropna().unique().tolist())
    acct_options_html = "<option value=''>All accounts</option>" + "".join(
        f'<option value="{a}">{a}</option>' for a in accounts
    )

    real_today = _date.today()
    today_str = real_today.strftime("%Y-%m-%d")
    current_fy_start = real_today.year if real_today.month >= 7 else real_today.year - 1
    data_min = df["date"].min()
    earliest_fy = data_min.year if data_min.month >= 7 else data_min.year - 1
    fy_html_parts = []
    fy_opts_list = []
    this_fy_lbl = f"FY{current_fy_start + 1} (Jul {current_fy_start} – Jun {current_fy_start + 1})"
    fy_html_parts.append(f'      <option value="this_fy">{this_fy_lbl}</option>')
    for fy_start in range(current_fy_start - 1, earliest_fy - 1, -1):
        fy_val = f"fy_{fy_start + 1}"
        fy_lbl = f"FY{fy_start + 1} (Jul {fy_start} – Jun {fy_start + 1})"
        fy_html_parts.append(f'      <option value="{fy_val}">{fy_lbl}</option>')
        fy_opts_list.append({"value": fy_val, "label": fy_lbl,
                             "from": f"{fy_start}-07-01", "to": f"{fy_start + 1}-06-30"})

    return {
        "records_json": records_json,
        "today_str": today_str,
        "fy_options_json": _json.dumps(fy_opts_list),
        "cat_options_html": cat_options_html,
        "cat_options_sel": cat_options_sel,
        "acct_options_html": acct_options_html,
        "period_opts_html": _period_select_options("\n".join(fy_html_parts)),
        "period_dates_js": _period_dates_js(),
        "cat_colors_json": _json.dumps(CATEGORY_COLORS),
        "txn_count": len(df),
    }


def generate_transactions_page(df: pd.DataFrame, config: dict, output_dir: Path) -> None:
    """Generate reports/transactions.html — searchable master CSV view."""
    output_path = output_dir / "transactions.html"

    if df.empty:
        output_path.write_text(
            "<!DOCTYPE html><html><body>"
            "<h2 style='font-family:sans-serif;padding:40px'>No transactions.</h2>"
            "</body></html>",
            encoding="utf-8",
        )
        logger.info(f"  Transactions page -> {output_path} (0 rows)")
        return

    categories = config.get("categories", list(CATEGORY_COLORS.keys()))
    cat_options_html = _cat_optgroups(categories, include_all=True)
    cat_options_sel = _cat_optgroups(categories)

    # Load Pay-in-4 groups for inline linkage display
    from src.payin4_detector import load_payin4_groups
    payin4_lookup = {g["purchase_txn_id"]: g for g in load_payin4_groups(config)}

    # Reload full df including split parent rows (they're excluded from summary reports
    # via load_transactions default, but the browsing page needs to show them).
    try:
        from src.db import load_transactions as _lt
        df_all = _lt(config, include_split_parents=True)
        if not df_all.empty:
            df = df_all
    except Exception:
        pass  # fall back to the df passed in (no split parents)

    # Build JS data array
    display = df.copy()
    display["date_str"]     = display["date"].dt.strftime("%Y-%m-%d")  # ISO — used for filtering
    display["date_display"] = display["date"].dt.strftime("%x")        # locale — shown to user
    display["amount_str"] = display["amount"].apply(
        lambda v: f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"
    )

    records = []
    for _, row in display.iterrows():
        txn_id = str(row.get("txn_id", ""))
        rec = {
            "txn_id": txn_id,
            "date": str(row.get("date_str", "")),
            "date_display": str(row.get("date_display", "")),
            "description": str(row.get("description", "")),
            "amount": float(row.get("amount", 0)),
            "amount_str": str(row.get("amount_str", "")),
            "category": str(row.get("category", "")),
            "account": str(row.get("account", "")),
            "is_business": bool(row.get("is_business", False)),
            "is_tax_deductible": bool(row.get("is_tax_deductible", False)),
            "is_gst_claimable": bool(row.get("is_gst_claimable", False)),
            "source_file": str(row.get("source_file", "")),
            "user_note": str(row.get("user_note", "")),
            "note": str(row.get("note", "")),
            "tags": str(row.get("tags", "") or ""),
            "payin4": payin4_lookup.get(txn_id),
            "parent_txn_id": str(row.get("parent_txn_id") or ""),
            "is_split_parent": int(row.get("is_split_parent") or 0),
            "split_children": [],
        }
        records.append(rec)

    # Attach split children to their parent records
    _children_by_parent: dict = {}
    for _r in records:
        if _r["parent_txn_id"]:
            _children_by_parent.setdefault(_r["parent_txn_id"], []).append(_r)
    for _r in records:
        if _r["is_split_parent"]:
            _r["split_children"] = _children_by_parent.get(_r["txn_id"], [])

    import json as _json
    from datetime import date as _date

    js_data = _json.dumps(records, ensure_ascii=False)

    # Unique accounts for filter dropdown
    accounts = sorted(df["account"].dropna().unique().tolist())
    acct_options = "<option value=''>All accounts</option>" + "".join(
        f'<option value="{a}">{a}</option>' for a in accounts
    )

    # Period preset options (FY range from data)
    real_today = _date.today()
    txn_today_str = real_today.strftime("%Y-%m-%d")
    current_fy_start = real_today.year if real_today.month >= 7 else real_today.year - 1
    data_min = df["date"].min()
    earliest_fy = data_min.year if data_min.month >= 7 else data_min.year - 1
    txn_fy_html_parts = []
    txn_fy_opts_list = []
    this_fy_lbl = f"FY{current_fy_start + 1} (Jul {current_fy_start} – Jun {current_fy_start + 1})"
    txn_fy_html_parts.append(f'      <option value="this_fy">{this_fy_lbl}</option>')
    for fy_start in range(current_fy_start - 1, earliest_fy - 1, -1):
        fy_val = f"fy_{fy_start + 1}"
        fy_lbl = f"FY{fy_start + 1} (Jul {fy_start} – Jun {fy_start + 1})"
        txn_fy_html_parts.append(f'      <option value="{fy_val}">{fy_lbl}</option>')
        txn_fy_opts_list.append({
            "value": fy_val, "label": fy_lbl,
            "from": f"{fy_start}-07-01", "to": f"{fy_start + 1}-06-30",
        })
    txn_fy_opts_js = _json.dumps(txn_fy_opts_list)
    txn_period_opts = _period_select_options("\n".join(txn_fy_html_parts))

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transactions ({len(df)})</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f0f4f8;margin:0;padding:20px 24px;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#264653 0%,#2a7b8e 100%);color:white;
         padding:24px 32px;border-radius:12px;margin-bottom:20px}}
.header h1{{margin:0 0 4px;font-size:1.5rem}}
.header p{{margin:0;opacity:.75;font-size:.9rem}}
.toolbar{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}}
.toolbar input,.toolbar select{{padding:8px 12px;border:1px solid #ccd;border-radius:8px;
                                font-size:.88rem}}
.toolbar input{{width:280px}}
.count{{margin-left:auto;font-size:.85rem;color:#666}}
.btn{{padding:8px 18px;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:.85rem}}
.btn-dl{{background:#264653;color:white}}
.btn-dl:hover{{background:#2a7b8e}}
.card{{background:white;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.07);overflow:auto}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
thead tr{{background:#264653;color:white;position:sticky;top:0}}
th{{padding:10px 12px;text-align:left;cursor:pointer;white-space:nowrap;user-select:none}}
th:hover{{background:#2a7b8e}}
th .sort-icon{{opacity:.5;font-size:.7em;margin-left:4px}}
td{{padding:7px 12px;border-bottom:1px solid #eef;white-space:nowrap}}
tr:hover td{{background:#f8fafc}}
.amt-neg{{color:#E63946;font-weight:600}}
.amt-pos{{color:#00BB77;font-weight:600}}
.biz-badge{{background:#FB5607;color:white;border-radius:4px;padding:1px 6px;font-size:.75em}}
.tax-badge{{background:#7C3AED;color:white;border-radius:4px;padding:1px 6px;font-size:.75em}}
.gst-badge{{background:#059669;color:white;border-radius:4px;padding:1px 6px;font-size:.75em}}
.cat-pill{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.78rem;font-weight:600}}
.ovr-btn{{padding:3px 10px;border:1px solid #ccd;border-radius:6px;background:#f8f9fc;
          cursor:pointer;font-size:.78rem}}
.ovr-btn:hover{{background:#264653;color:white;border-color:#264653}}
.modal-bg{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;
           align-items:center;justify-content:center}}
.modal-bg.open{{display:flex}}
.modal{{background:white;border-radius:14px;padding:28px 32px;width:420px;max-width:95vw;
        box-shadow:0 8px 40px rgba(0,0,0,.2)}}
.modal h3{{margin:0 0 16px;font-size:1.1rem;color:#264653}}
.modal .info{{font-size:.85rem;color:#555;margin-bottom:16px;line-height:1.5}}
.modal select{{width:100%;padding:9px;border:1px solid #ccd;border-radius:8px;
               font-size:.9rem;margin-bottom:8px}}
.modal label{{font-size:.85rem;color:#444;display:flex;align-items:center;gap:8px;margin-bottom:16px}}
.modal .actions{{display:flex;gap:10px;justify-content:flex-end}}
.modal .btn-cancel{{background:#eee;color:#444}}
.modal .btn-apply{{background:#264653;color:white}}
.saved-banner{{display:none;background:#00BB77;color:white;padding:12px 20px;
              border-radius:8px;margin-bottom:16px;font-weight:600}}
.site-nav{{background:#1d3540;padding:0 24px;display:flex;gap:4px;
           border-radius:10px;margin-bottom:20px}}
.nav-link{{color:rgba(255,255,255,.65);text-decoration:none;padding:12px 18px;
           font-size:.88rem;font-weight:600;border-bottom:3px solid transparent}}
.nav-link:hover{{color:white}}
.nav-link.active{{color:white;border-bottom-color:#2a9d8f}}
.tab-bar{{display:flex;gap:2px;background:#1d3540;border-radius:10px;
          padding:6px 12px;margin-bottom:20px;flex-wrap:wrap;align-items:center}}
.tab-btn{{padding:8px 16px;border:none;border-radius:7px;background:transparent;
          color:rgba(255,255,255,.55);font-size:.82rem;font-weight:600;cursor:pointer;
          border-bottom:3px solid transparent;text-decoration:none;display:inline-block}}
.tab-btn.active{{background:rgba(42,157,143,.15);color:#2a9d8f;border-bottom-color:#2a9d8f}}
.tab-btn:hover:not(.active){{color:white}}
{_IMPORT_CSS}
</style>
</head>
<body>
{_build_nav_html("transactions")}
<div class="header">
  <h1>Transactions</h1>
  <p>Complete transaction history across all accounts.</p>
</div>
{_help_box(
    "How to use this page",
    "Search, filter and inspect every transaction from every account.",
    [
        "Use the search box to filter by description, payee, or note; use the period dropdown to filter by date range.",
        "Click any row to expand it &mdash; change the category, toggle the business expense flag, or add a note.",
        "All edits save automatically; there is no Save button.",
        "The auto-categoriser reruns across all transactions on every import.",
    ]
)}
<div class="saved-banner" id="banner"></div>
<div class="toolbar">
  <select id="period-preset" onchange="applyPreset()" style="padding:8px 10px;border:1px solid #ccd;border-radius:8px;font-size:.88rem">
    <option value="">&#8212; Quick select period &#8212;</option>
    {txn_period_opts}
  </select>
  <input type="text" id="search" placeholder="Search description or note...">
  <input type="date" id="from-date" title="From date" style="padding:8px 10px;border:1px solid #ccd;border-radius:8px;font-size:.88rem">
  <input type="date" id="to-date" title="To date" style="padding:8px 10px;border:1px solid #ccd;border-radius:8px;font-size:.88rem">
  <input type="number" id="amt-min" placeholder="Min $" min="0" step="0.01" title="Minimum amount (absolute)" style="padding:8px 10px;border:1px solid #ccd;border-radius:8px;font-size:.88rem;width:90px">
  <input type="number" id="amt-max" placeholder="Max $" min="0" step="0.01" title="Maximum amount (absolute)" style="padding:8px 10px;border:1px solid #ccd;border-radius:8px;font-size:.88rem;width:90px">
  <select id="cat-filter">{cat_options_html}</select>
  <select id="acct-filter">{acct_options}</select>
  <select id="biz-filter">
    <option value="">All</option>
    <option value="1">Business only</option>
  </select>
  <select id="tag-filter">
    <option value="">All tags</option>
  </select>
  <a href="/tags" style="font-size:.82rem;color:#2a9d8f;white-space:nowrap;text-decoration:none">&#127991; Tags</a>
  <span class="count" id="count">{len(df)} transactions</span>
</div>
<div class="card">
<table>
<thead><tr>
  <th onclick="sortBy('date')">Date<span class="sort-icon" id="sort-date">&#9650;</span></th>
  <th onclick="sortBy('description')">Description<span class="sort-icon" id="sort-description"></span></th>
  <th onclick="sortBy('account')">Account<span class="sort-icon" id="sort-account"></span></th>
  <th onclick="sortBy('amount')">Amount<span class="sort-icon" id="sort-amount"></span></th>
  <th onclick="sortBy('category')">Category<span class="sort-icon" id="sort-category"></span></th>
  <th>Note</th>
  <th>Override</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
<div id="pagination" style="display:flex;align-items:center;gap:12px;padding:12px 16px;font-size:.88rem;color:#64748b"></div>
</div>

<div class="modal-bg" id="modal-bg">
  <div class="modal">
    <h3>Override Category</h3>
    <div class="info" id="modal-info"></div>
    <select id="modal-cat">{cat_options_sel}</select>
    <select id="modal-sub" onchange="onSubChange()"
      style="width:100%;margin-top:8px;padding:8px 10px;border:1px solid #ccd;border-radius:8px;font-size:.9rem"></select>
    <input type="text" id="modal-sub-new" placeholder="Enter new sub-category name…"
      style="display:none;width:100%;margin-top:6px;padding:8px 10px;border:1px solid #2a9d8f;border-radius:8px;font-size:.9rem">
    <label style="margin-top:8px;display:block"><input type="checkbox" id="modal-all" onchange="onModalAllChange()"> Apply to all similar descriptions (updates default for future runs)</label>
    <div id="modal-merchant-row" style="display:none;margin-top:8px">
      <div style="font-size:.8rem;color:#666;margin-bottom:4px">Save merchant rule — all transactions matching this prefix will get this category:</div>
      <input type="text" id="modal-merchant" placeholder="e.g. COFFETTI DONCASTER"
        style="width:100%;padding:7px 10px;border:1px solid #2a9d8f;border-radius:8px;font-size:.88rem;box-sizing:border-box">
    </div>
    <div class="actions">
      <button class="btn btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn btn-apply" onclick="applyOverride()">Save</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="note-modal-bg">
  <div class="modal">
    <h3>Edit Note</h3>
    <div class="info" id="note-modal-info"></div>
    <textarea id="note-modal-text" rows="3" style="width:100%;padding:9px;border:1px solid #ccd;border-radius:8px;font-size:.9rem;resize:vertical;margin-bottom:12px" placeholder="Add a note for this transaction..."></textarea>
    <div class="actions">
      <button class="btn btn-cancel" onclick="closeNoteModal()">Cancel</button>
      <button class="btn btn-apply" onclick="saveNote()">Save</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="flags-modal-bg">
  <div class="modal">
    <h3>Edit Flags</h3>
    <div class="info" id="flags-modal-info"></div>
    <label style="display:flex;align-items:center;gap:8px;margin:14px 0 8px;cursor:pointer">
      <input type="checkbox" id="flags-modal-biz" style="width:16px;height:16px">
      <span><strong>Business Expense</strong> — reimbursable by Bedlin <span class="biz-badge" style="vertical-align:middle">BIZ</span></span>
    </label>
    <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;cursor:pointer">
      <input type="checkbox" id="flags-modal-tax" style="width:16px;height:16px">
      <span><strong>Tax Deductible</strong> — personal deduction, not reimbursed <span class="tax-badge" style="vertical-align:middle">TAX</span></span>
    </label>
    <label style="display:flex;align-items:center;gap:8px;margin-bottom:18px;cursor:pointer">
      <input type="checkbox" id="flags-modal-gst" style="width:16px;height:16px">
      <span><strong>GST Claimable</strong> — ATO input tax credit (1/11th) <span class="gst-badge" style="vertical-align:middle">GST</span></span>
    </label>
    <div class="actions">
      <button class="btn btn-cancel" onclick="closeFlagsModal()">Cancel</button>
      <button class="btn btn-apply" onclick="saveFlags()">Save</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="tag-modal-bg">
  <div class="modal" style="width:min(440px,95vw)">
    <h2>Edit Tags</h2>
    <div id="tag-modal-info" style="font-size:.82rem;color:#94a3b8;margin-bottom:16px"></div>
    <div id="tag-chips-wrap" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px;min-height:28px"></div>
    <div style="display:flex;gap:8px;align-items:center">
      <input id="tag-input" type="text" placeholder="Add tag (Enter to add)" list="known-tags-list"
             style="flex:1;padding:8px 12px;border-radius:8px;border:1px solid #2e4a5a;background:#0f1923;color:#e2e8f0;font-size:.9rem"
             onkeydown="if(event.key==='Enter'){{addTagFromInput();event.preventDefault();}}">
      <datalist id="known-tags-list"></datalist>
      <button class="btn-save" onclick="addTagFromInput()" style="white-space:nowrap;font-size:.85rem;padding:8px 14px">+ Add</button>
    </div>
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeTagModal()">Cancel</button>
      <button class="btn-save btn-apply" onclick="saveTagsModal()">Save</button>
    </div>
  </div>
</div>

<script>
const RAW = {js_data};
const TODAY = "{txn_today_str}";
const FY_OPTIONS = {txn_fy_opts_js};
let saved = {{}};  // txn_id -> saved category (for visual tracking)
let sortCol = 'date', sortDir = 1;
let currentTxnId = null;

function escHtml(s) {{
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}}

function fxLine(note) {{
  if (!note) return '';
  var m = (note || '').match(/\\[([A-Z]{{3}})\\s+([-\\d.]+)\\]/);
  if (!m) return '';
  var amt = parseFloat(m[2]);
  return '<span style="font-size:.74rem;color:#94a3b8;display:inline-block;margin-left:4px">(' + m[1] + ' ' + (amt < 0 ? '-' : '') + Math.abs(amt).toFixed(2) + ')</span>';
}}

function renderTagChips(tags) {{
  if (!tags) return '';
  var parts = tags.split(',').map(function(s){{return s.trim();}}).filter(Boolean);
  if (!parts.length) return '';
  return '<span style="display:inline-flex;flex-wrap:wrap;gap:3px;margin-left:5px;vertical-align:middle">' +
    parts.map(function(t){{
      return '<span style="font-size:.68rem;padding:1px 7px;border-radius:8px;background:rgba(42,157,143,.18);color:#2a9d8f;border:1px solid rgba(42,157,143,.28);font-weight:600;cursor:default">' + escHtml(t) + '</span>';
    }}).join('') + '</span>';
}}

const COLORS = {_json.dumps(CATEGORY_COLORS)};

function catColor(cat) {{
  return COLORS[cat] || '#8D8D8D';
}}

// Period preset helpers (shared logic with dashboard)
{_period_dates_js()}

function applyPreset() {{
  const p = document.getElementById('period-preset').value;
  if (!p) return;
  const range = periodDates(p);
  if (range && range !== null) {{
    document.getElementById('from-date').value = range.from || '';
    document.getElementById('to-date').value   = range.to   || '';
  }}
  render();
}}

var PAGE_SIZE = 100;
var _currentPage = 1;
var _filteredRows = [];

function render() {{
  const q      = document.getElementById('search').value.toLowerCase();
  const fd     = document.getElementById('from-date').value;
  const td     = document.getElementById('to-date').value;
  const amtMin = parseFloat(document.getElementById('amt-min').value);
  const amtMax = parseFloat(document.getElementById('amt-max').value);
  const cf     = document.getElementById('cat-filter').value;
  const af     = document.getElementById('acct-filter').value;
  const bf     = document.getElementById('biz-filter').value;
  const tagf   = document.getElementById('tag-filter').value;

  _filteredRows = RAW.filter(r => {{
    if (r.parent_txn_id) return false;  // skip split children — shown inline under parent
    if (q && !r.description.toLowerCase().includes(q) && !(r.user_note||'').toLowerCase().includes(q) && !(r.tags||'').toLowerCase().includes(q)) return false;
    if (fd && r.date < fd) return false;
    if (td && r.date > td) return false;
    const abs = Math.abs(r.amount);
    if (!isNaN(amtMin) && abs < amtMin - 0.005) return false;
    if (!isNaN(amtMax) && abs > amtMax + 0.005) return false;
    if (cf && r.category !== cf) return false;
    if (af && r.account !== af) return false;
    if (bf === '1' && !r.is_business) return false;
    if (tagf && !(r.tags||'').split(',').map(function(s){{return s.trim();}}).filter(Boolean).includes(tagf)) return false;
    return true;
  }});

  _filteredRows.sort((a,b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    return av < bv ? -sortDir : av > bv ? sortDir : 0;
  }});

  _currentPage = 1;
  renderPage();
}}

function renderPage() {{
  const total     = _filteredRows.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  _currentPage    = Math.min(_currentPage, totalPages);
  const start     = (_currentPage - 1) * PAGE_SIZE;
  const rows      = _filteredRows.slice(start, start + PAGE_SIZE);

  document.getElementById('count').textContent =
    total + ' of {len(df)} transactions' +
    (totalPages > 1 ? ' — page ' + _currentPage + ' of ' + totalPages : '');

  const pagDiv = document.getElementById('pagination');
  if (totalPages > 1) {{
    pagDiv.innerHTML =
      '<button onclick="goPage(' + (_currentPage-1) + ')" ' + (_currentPage<=1?'disabled':'') +
        ' style="padding:4px 12px;border:1px solid #cbd5e1;border-radius:6px;cursor:pointer">&#8592; Prev</button>' +
      '<span>Page ' + _currentPage + ' of ' + totalPages + ' (' + total + ' rows)</span>' +
      '<button onclick="goPage(' + (_currentPage+1) + ')" ' + (_currentPage>=totalPages?'disabled':'') +
        ' style="padding:4px 12px;border:1px solid #cbd5e1;border-radius:6px;cursor:pointer">Next &#8594;</button>';
  }} else {{
    pagDiv.innerHTML = '';
  }}

  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map(r => {{
    const cat = r.category;
    const amtCls = r.amount < 0 ? 'amt-neg' : 'amt-pos';
    const biz = r.is_business ? '<span class="biz-badge">BIZ</span>' : '';
    const tax = r.is_tax_deductible ? '<span class="tax-badge">TAX</span>' : '';
    const gst = r.is_gst_claimable ? '<span class="gst-badge">GST</span>' : '';
    const wasSaved = !!saved[r.txn_id];
    const ovr = wasSaved ? '<span style="color:#00BB77;font-size:.75em">&#10003; saved</span>' : '';
    const color = catColor(cat);
    const noteText = r.user_note || '';
    const noteDisplay = noteText
      ? `<span style="font-size:.78rem;color:#444" title="${{escHtml(noteText)}}">${{escHtml(noteText.substring(0,30))}}${{noteText.length>30?'…':''}}</span>`
      : '';
    const noteBtn = `<button class="ovr-btn" onclick="openNoteModal('${{r.txn_id}}')" title="Edit note">&#9998;</button>`;

    // Split badge and children detail rows
    const splitBadge = r.is_split_parent
      ? `<span style="display:inline-block;padding:1px 6px;border-radius:6px;font-size:.7rem;font-weight:700;background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;margin-left:4px">SPLIT</span>`
        + `<button class="ovr-btn" onclick="toggleSplit('${{r.txn_id}}')" style="margin-left:4px;font-size:.75rem" title="Show/hide split detail">&#8942;</button>`
      : '';
    const splitDetail = r.is_split_parent && r.split_children && r.split_children.length
      ? '<tr id="split-${{r.txn_id}}" style="display:none"><td colspan="7" style="padding:0 0 4px 32px;background:var(--surface-2)">'
        + '<table style="width:100%;border-collapse:collapse;font-size:.83rem">'
        + '<thead><tr><th style="padding:4px 10px;text-align:left;color:var(--text-2)">#</th>'
        + '<th style="padding:4px 10px;text-align:left;color:var(--text-2)">Description</th>'
        + '<th style="padding:4px 10px;text-align:left;color:var(--text-2)">Amount</th>'
        + '<th style="padding:4px 10px;text-align:left;color:var(--text-2)">Category</th></tr></thead><tbody>'
        + r.split_children.map(function(c, idx) {{
            const cc = catColor(c.category);
            return '<tr><td style="padding:4px 10px;color:var(--text-2)">' + (idx+1) + '</td>'
              + '<td style="padding:4px 10px">' + escHtml(c.description.substring(0,50)) + '</td>'
              + '<td style="padding:4px 10px;font-weight:700;color:' + (c.amount<0?'#e63946':'#2a9d8f') + '">' + escHtml(c.amount_str) + '</td>'
              + '<td style="padding:4px 10px"><span class="cat-pill" style="background:' + cc + '22;color:' + cc + '">' + c.category + '</span></td></tr>';
          }}).join('')
        + '</tbody></table>'
        + '<div style="padding:4px 10px 8px"><button class="ovr-btn" onclick="unsplitTxn('' + r.txn_id + '')" style="color:#E63946;border-color:#E63946" title="Remove split">&#10006; Remove split</button></div>'
        + '</td></tr>'
      : '';

    const p4 = r.payin4;
    const p4Badge = p4
      ? `<button class="p4-toggle ovr-btn" onclick="toggleP4('${{r.txn_id}}')"
           title="Pay in 4 — ${{p4.status}}: ${{p4.anz_matched}}/4 bank payments linked"
           style="margin-left:4px;background:#e0f2fe;color:#0369a1;border-color:#7dd3fc">
           &#x1F4B3; 4&times; <span id="p4-arrow-${{r.txn_id}}">&#9660;</span></button>`
      : '';
    const p4Detail = p4 ? (() => {{
      const rows = p4.instalments.map(i => {{
        const paid = i.anz_txn_id
          ? `<span style="color:#16a34a">&#10003; Paid — ${{escHtml(i.anz_account||'')}}</span>`
          : `<span style="color:#dc2626">&#9888; Bank debit not matched</span>`;
        return `<tr style="background:#f0f9ff">
          <td style="padding:4px 10px;color:#64748b">${{i.sequence}}/4</td>
          <td style="padding:4px 10px">${{escHtml(i.date)}}</td>
          <td style="padding:4px 10px;font-weight:600">-$${{i.amount.toFixed(2)}}</td>
          <td style="padding:4px 10px">${{paid}}</td>
        </tr>`;
      }}).join('');
      const allPaid = p4.anz_matched === 4;
      const summary = allPaid
        ? `<span style="color:#16a34a;font-weight:600">&#10003; All 4 payments confirmed — total -$${{p4.anz_total.toFixed(2)}}</span>`
        : `<span style="color:#d97706;font-weight:600">&#9888; ${{p4.anz_matched}}/4 payments matched — $${{p4.anz_total.toFixed(2)}} of $${{p4.total_amount.toFixed(2)}}</span>`;
      return `<tr id="p4-${{r.txn_id}}" style="display:none">
        <td colspan="7" style="padding:0 0 4px 32px;background:#f8fafc">
          <table style="width:100%;border-collapse:collapse;font-size:.83rem;margin:4px 0">
            <thead><tr style="background:#e0f2fe">
              <th style="padding:4px 10px;text-align:left;color:#0369a1">#</th>
              <th style="padding:4px 10px;text-align:left;color:#0369a1">Date</th>
              <th style="padding:4px 10px;text-align:left;color:#0369a1">Amount</th>
              <th style="padding:4px 10px;text-align:left;color:#0369a1">Bank payment</th>
            </tr></thead>
            <tbody>${{rows}}</tbody>
            <tfoot><tr><td colspan="4" style="padding:6px 10px">${{summary}}</td></tr></tfoot>
          </table>
        </td>
      </tr>`;
    }})() : '';

    return `<tr>
      <td>${{escHtml(r.date_display)}}</td>
      <td title="${{escHtml(r.description)}}">${{escHtml(r.description.substring(0,65))}}${{fxLine(r.note)}}${{renderTagChips(r.tags)}}${{splitBadge}}</td>
      <td>${{escHtml(r.account)}}</td>
      <td class="${{amtCls}}">${{escHtml(r.amount_str)}} ${{biz}}${{tax}}${{gst}}</td>
      <td><span class="cat-pill" style="background:${{color}}22;color:${{color}}">${{cat}}</span> ${{ovr}}</td>
      <td>${{noteDisplay}} ${{noteBtn}}</td>
      <td>
        ${{p4Badge}}
        <button class="ovr-btn" onclick="openModal('${{r.txn_id}}')">Change</button>
        <button class="ovr-btn" onclick="openFlagsModal('${{r.txn_id}}')" title="Edit flags" style="margin-left:4px">Flags</button>
        <button class="ovr-btn" onclick="openTagModal('${{r.txn_id}}')" title="Edit tags" style="margin-left:4px">&#x1F3F7;</button>
      </td>
    </tr>${{p4Detail}}${{splitDetail}}`;
  }}).join('');

}}

function toggleSplit(txn_id) {{
  var el = document.getElementById('split-' + txn_id);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}}

function unsplitTxn(txn_id) {{
  if (!confirm('Remove split? Children will be deleted and the original transaction restored.')) return;
  fetch('/api/txn/' + txn_id + '/split', {{method: 'DELETE'}})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      if (d.ok) {{
        alert('Split removed. Re-generate reports to update the view.');
      }} else {{
        alert('Error: ' + (d.error || 'unknown'));
      }}
    }}).catch(function(e) {{ alert('Network error: ' + e); }});
}}

function goPage(p) {{
  _currentPage = p;
  renderPage();
  document.getElementById('tbody').scrollIntoView({{behavior:'smooth', block:'start'}});
}}

function sortBy(col) {{
  document.querySelectorAll('.sort-icon').forEach(el => el.innerHTML = '');
  if (sortCol === col) sortDir *= -1; else {{ sortCol = col; sortDir = 1; }}
  document.getElementById('sort-' + col).innerHTML = sortDir === 1 ? '&#9650;' : '&#9660;';
  _filteredRows.sort((a,b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    return av < bv ? -sortDir : av > bv ? sortDir : 0;
  }});
  _currentPage = 1;
  renderPage();
}}

['search','from-date','to-date','cat-filter','acct-filter','biz-filter','tag-filter'].forEach(id =>
  document.getElementById(id).addEventListener('input', render));

var _amtTimer;
['amt-min','amt-max'].forEach(id =>
  document.getElementById(id).addEventListener('input', function() {{
    clearTimeout(_amtTimer);
    _amtTimer = setTimeout(render, 350);
  }}));

// Pre-populate filters from URL params (used by click-through from reports)
(function() {{
  var p = new URLSearchParams(window.location.search);
  var cat    = p.get('cat');
  var from   = p.get('from');
  var to     = p.get('to');
  var search = p.get('search');
  var changed = false;
  if (cat)    {{ document.getElementById('cat-filter').value = cat;    changed = true; }}
  if (from)   {{ document.getElementById('from-date').value  = from;   changed = true; }}
  if (to)     {{ document.getElementById('to-date').value    = to;     changed = true; }}
  if (search) {{ document.getElementById('search').value     = search; changed = true; }}
  var tag = p.get('tag');
  if (tag) {{
    // tag-filter is populated by _rebuildTagFilter which runs before this IIFE
    var sel = document.getElementById('tag-filter');
    // If tag not in the list yet (no transactions tagged yet), add it temporarily
    if (!Array.from(sel.options).some(function(o){{return o.value === tag;}})) {{
      var opt = document.createElement('option');
      opt.value = tag; opt.textContent = tag;
      sel.appendChild(opt);
    }}
    sel.value = tag;
    changed = true;
  }}
  if (changed) render();
}})();

function toggleP4(txnId) {{
  const detail = document.getElementById('p4-' + txnId);
  const arrow  = document.getElementById('p4-arrow-' + txnId);
  if (!detail) return;
  const open = detail.style.display !== 'none' && detail.style.display !== '';
  detail.style.display = open ? 'none' : 'table-row';
  if (arrow) arrow.textContent = open ? '&#9660;' : '&#9650;';
}}

var SUBCATS = {{}};
fetch('/api/subcats').then(r => r.json()).then(d => {{ SUBCATS = d; }});

function _refreshSubSelect(cat, current) {{
  const sel = document.getElementById('modal-sub');
  const list = SUBCATS[cat] || [];
  const known = current && !list.includes(current) ? [current, ...list] : list;
  sel.innerHTML =
    '<option value="">— None —</option>' +
    known.map(s => `<option value="${{escHtml(s)}}">${{escHtml(s)}}</option>`).join('') +
    '<option value="__new__">+ Add new sub-category…</option>';
  sel.value = current || '';
  document.getElementById('modal-sub-new').style.display = 'none';
}}

function onSubChange() {{
  const newInp = document.getElementById('modal-sub-new');
  if (document.getElementById('modal-sub').value === '__new__') {{
    newInp.style.display = 'block';
    newInp.value = '';
    newInp.focus();
  }} else {{
    newInp.style.display = 'none';
  }}
}}

function _getSubValue() {{
  const sel = document.getElementById('modal-sub');
  if (sel.value === '__new__') return document.getElementById('modal-sub-new').value.trim();
  return sel.value;
}}

function openModal(txnId) {{
  currentTxnId = txnId;
  const r = RAW.find(x => x.txn_id === txnId);
  if (!r) return;
  document.getElementById('modal-info').innerHTML =
    `<strong>${{escHtml(r.date_display)}}</strong> &nbsp; ${{escHtml(r.description.substring(0,60))}}<br>${{escHtml(r.account)}} &nbsp; <strong>${{escHtml(r.amount_str)}}</strong>`;
  document.getElementById('modal-cat').value = r.category;
  document.getElementById('modal-all').checked = true;
  _refreshSubSelect(r.category, r.sub || '');
  document.getElementById('modal-merchant').value = _merchantPrefix(r.description);
  document.getElementById('modal-merchant-row').style.display = 'block';
  document.getElementById('modal-bg').classList.add('open');
}}

document.getElementById('modal-cat').addEventListener('change', function() {{
  _refreshSubSelect(this.value, '');
}});

function closeModal() {{
  document.getElementById('modal-bg').classList.remove('open');
  currentTxnId = null;
}}

function applyOverride() {{
  if (!currentTxnId) return;
  const r   = RAW.find(x => x.txn_id === currentTxnId);
  const cat = document.getElementById('modal-cat').value;
  const sub = _getSubValue();
  const all = document.getElementById('modal-all').checked;

  const label = sub ? cat + ' → ' + sub : cat;
  const msg = 'Change "' + r.description.substring(0,50) + '" to "' + label + '"?' +
    (all ? '\\n\\nThis will also update the category default for all similar descriptions in future runs.' : '');
  if (!confirm(msg)) return;

  const btn = document.querySelector('#modal-bg .btn-apply');
  if (btn) btn.disabled = true;

  fetch('/api/apply-override', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify([{{
      txn_id: currentTxnId,
      category: cat,
      sub_category: sub,
      apply_to_all: all,
      description: r.description,
      amount: r.amount
    }}])
  }})
  .then(res => res.json())
  .then(data => {{
    if (btn) btn.disabled = false;
    if (data.ok) {{
      r.category = cat;
      r.sub = sub;
      if (sub) {{
        if (!SUBCATS[cat]) SUBCATS[cat] = [];
        if (!SUBCATS[cat].includes(sub)) {{
          // Persist new sub-category to server so it appears in Merchant Rules too
          fetch('/api/subcats/add', {{
            method: 'POST', headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{category: cat, subcat: sub}})
          }}).then(r => r.json()).then(d => {{ if (d.ok) SUBCATS = d.subcats; }});
        }}
      }}
      // Save merchant rule if a prefix was specified
      const merchant = (document.getElementById('modal-merchant').value || '').trim().toUpperCase();
      if (all && merchant) {{
        fetch('/api/merchant-rules', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{merchant: merchant, category: cat, sub_category: sub}})
        }}).then(mr => mr.json()).then(md => {{
          if (md.ok && md.backfilled > 0) {{
            const b = document.getElementById('banner');
            b.textContent += ' (' + md.backfilled + ' historical transaction(s) also updated via merchant rule.)';
          }}
        }}).catch(() => {{}});
      }}
      saved[currentTxnId] = cat;
      closeModal();
      renderPage();
      const banner = document.getElementById('banner');
      banner.textContent = '✓ Category updated. Reports refresh automatically on next import.';
      banner.style.display = 'block';
      banner.style.background = '#00BB77';
    }} else {{
      alert('Error saving: ' + (data.error || 'Unknown error'));
    }}
  }})
  .catch(e => {{
    if (btn) btn.disabled = false;
    alert('Network error: ' + e);
  }});
}}

document.getElementById('modal-bg').addEventListener('click', function(e) {{
  if (e.target === this) closeModal();
}});

let currentNoteTxnId = null;

function openNoteModal(txnId) {{
  currentNoteTxnId = txnId;
  const r = RAW.find(x => x.txn_id === txnId);
  if (!r) return;
  document.getElementById('note-modal-info').innerHTML =
    `<strong>${{escHtml(r.date_display)}}</strong> &nbsp; ${{escHtml(r.description.substring(0,60))}}<br>${{escHtml(r.account)}} &nbsp; <strong>${{escHtml(r.amount_str)}}</strong>`;
  document.getElementById('note-modal-text').value = r.user_note || '';
  document.getElementById('note-modal-bg').classList.add('open');
  document.getElementById('note-modal-text').focus();
}}

function closeNoteModal() {{
  document.getElementById('note-modal-bg').classList.remove('open');
  currentNoteTxnId = null;
}}

function saveNote() {{
  if (!currentNoteTxnId) return;
  const note = document.getElementById('note-modal-text').value;
  const btn = document.querySelector('#note-modal-bg .btn-apply');
  if (btn) btn.disabled = true;
  fetch('/api/save-note', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ txn_id: currentNoteTxnId, note: note }})
  }})
  .then(res => res.json())
  .then(data => {{
    if (btn) btn.disabled = false;
    if (data.ok) {{
      const r = RAW.find(x => x.txn_id === currentNoteTxnId);
      if (r) r.user_note = note.trim();
      closeNoteModal();
      render();
    }} else {{
      alert('Error saving note: ' + (data.error || 'Unknown error'));
    }}
  }})
  .catch(e => {{
    if (btn) btn.disabled = false;
    alert('Network error: ' + e);
  }});
}}

document.getElementById('note-modal-bg').addEventListener('click', function(e) {{
  if (e.target === this) closeNoteModal();
}});

let currentFlagsTxnId = null;

function openFlagsModal(txnId) {{
  currentFlagsTxnId = txnId;
  const r = RAW.find(x => x.txn_id === txnId);
  if (!r) return;
  document.getElementById('flags-modal-info').innerHTML =
    `<strong>${{escHtml(r.date_display)}}</strong> &nbsp; ${{escHtml(r.description.substring(0,60))}}<br>${{escHtml(r.account)}} &nbsp; <strong>${{escHtml(r.amount_str)}}</strong>`;
  document.getElementById('flags-modal-biz').checked = !!r.is_business;
  document.getElementById('flags-modal-tax').checked = !!r.is_tax_deductible;
  document.getElementById('flags-modal-gst').checked = !!r.is_gst_claimable;
  document.getElementById('flags-modal-bg').classList.add('open');
}}

function closeFlagsModal() {{
  document.getElementById('flags-modal-bg').classList.remove('open');
  currentFlagsTxnId = null;
}}

function saveFlags() {{
  if (!currentFlagsTxnId) return;
  const r   = RAW.find(x => x.txn_id === currentFlagsTxnId);
  const biz = document.getElementById('flags-modal-biz').checked;
  const tax = document.getElementById('flags-modal-tax').checked;
  const gst = document.getElementById('flags-modal-gst').checked;
  const btn = document.querySelector('#flags-modal-bg .btn-apply');
  if (btn) btn.disabled = true;

  fetch('/api/transactions/' + currentFlagsTxnId, {{
    method: 'PATCH',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ is_business: biz, is_tax_deductible: tax, is_gst_claimable: gst }})
  }})
  .then(res => res.json())
  .then(data => {{
    if (btn) btn.disabled = false;
    if (data.ok) {{
      r.is_business = biz;
      r.is_tax_deductible = tax;
      r.is_gst_claimable = gst;
      closeFlagsModal();
      render();
      const banner = document.getElementById('banner');
      banner.textContent = '✓ Flags updated.';
      banner.style.display = 'block';
      banner.style.background = '#00BB77';
    }} else {{
      alert('Error saving flags: ' + (data.error || 'Unknown error'));
    }}
  }})
  .catch(e => {{
    if (btn) btn.disabled = false;
    alert('Network error: ' + e);
  }});
}}

document.getElementById('flags-modal-bg').addEventListener('click', function(e) {{
  if (e.target === this) closeFlagsModal();
}});

// ── Tag modal ─────────────────────────────────────────────────────────────────
var currentTagTxnId = null;
var _pendingTags = [];

function openTagModal(txnId) {{
  currentTagTxnId = txnId;
  var r = RAW.find(function(x){{return x.txn_id === txnId;}});
  if (!r) return;
  document.getElementById('tag-modal-info').innerHTML =
    '<strong>' + escHtml(r.date_display) + '</strong> &nbsp; ' + escHtml(r.description.substring(0,60)) +
    '<br>' + escHtml(r.account) + ' &nbsp; <strong>' + escHtml(r.amount_str) + '</strong>';
  _pendingTags = (r.tags || '').split(',').map(function(s){{return s.trim();}}).filter(Boolean);
  _refreshTagChipsWrap();
  _populateTagDatalist();
  document.getElementById('tag-input').value = '';
  document.getElementById('tag-modal-bg').classList.add('open');
  setTimeout(function(){{document.getElementById('tag-input').focus();}}, 80);
}}

function closeTagModal() {{
  document.getElementById('tag-modal-bg').classList.remove('open');
  currentTagTxnId = null;
}}

function _refreshTagChipsWrap() {{
  var wrap = document.getElementById('tag-chips-wrap');
  if (!_pendingTags.length) {{
    wrap.innerHTML = '<span style="color:#64748b;font-size:.82rem">No tags yet</span>';
    return;
  }}
  wrap.innerHTML = _pendingTags.map(function(t){{
    return '<span style="display:inline-flex;align-items:center;gap:4px;background:rgba(42,157,143,.15);color:#2a9d8f;border:1px solid rgba(42,157,143,.3);border-radius:10px;padding:3px 10px;font-size:.82rem;font-weight:600">' +
      escHtml(t) +
      '<button onclick="_removeTag('' + escHtml(t) + '')" style="background:none;border:none;color:#2a9d8f;cursor:pointer;font-size:.9rem;padding:0;line-height:1;margin-left:2px">&times;</button></span>';
  }}).join('');
}}

function _removeTag(tag) {{
  _pendingTags = _pendingTags.filter(function(t){{return t !== tag;}});
  _refreshTagChipsWrap();
}}

function addTagFromInput() {{
  var inp = document.getElementById('tag-input');
  var val = inp.value.trim();
  if (!val) return;
  val.split(',').forEach(function(t) {{
    t = t.trim();
    if (t && !_pendingTags.includes(t)) _pendingTags.push(t);
  }});
  inp.value = '';
  _refreshTagChipsWrap();
}}

function _populateTagDatalist() {{
  var dl = document.getElementById('known-tags-list');
  var existing = new Set(_pendingTags);
  var known = [];
  var seen = {{}};
  RAW.forEach(function(r) {{
    (r.tags || '').split(',').forEach(function(t) {{
      t = t.trim();
      if (t && !seen[t]) {{ seen[t] = true; known.push(t); }}
    }});
  }});
  known.sort();
  dl.innerHTML = known.filter(function(t){{return !existing.has(t);}}).map(function(t){{
    return '<option value="' + escHtml(t) + '">';
  }}).join('');
}}

function saveTagsModal() {{
  if (!currentTagTxnId) return;
  var btn = document.querySelector('#tag-modal-bg .btn-apply');
  if (btn) btn.disabled = true;
  fetch('/api/txn/' + currentTagTxnId + '/tags', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{tags: _pendingTags}})
  }}).then(function(resp){{return resp.json();}}).then(function(data) {{
    if (data.ok) {{
      var r = RAW.find(function(x){{return x.txn_id === currentTagTxnId;}});
      if (r) r.tags = _pendingTags.join(',');
      _rebuildTagFilter();
      closeTagModal();
      render();
      var banner = document.getElementById('banner');
      banner.textContent = '✓ Tags updated.';
      banner.style.display = 'block';
      banner.style.background = '#00BB77';
    }} else {{
      alert('Error saving tags: ' + (data.error || 'Unknown error'));
      if (btn) btn.disabled = false;
    }}
  }}).catch(function(e) {{
    alert('Network error: ' + e);
    if (btn) btn.disabled = false;
  }});
}}

function _rebuildTagFilter() {{
  var sel = document.getElementById('tag-filter');
  var prev = sel.value;
  var seen = {{}};
  var allTags = [];
  RAW.forEach(function(r) {{
    (r.tags || '').split(',').forEach(function(t) {{
      t = t.trim();
      if (t && !seen[t]) {{ seen[t] = true; allTags.push(t); }}
    }});
  }});
  allTags.sort(function(a,b){{return a.toLowerCase() < b.toLowerCase() ? -1 : 1;}});
  sel.innerHTML = '<option value="">All tags</option>' +
    allTags.map(function(t){{return '<option value="' + escHtml(t) + '">' + escHtml(t) + '</option>';}}).join('');
  if (allTags.indexOf(prev) >= 0) sel.value = prev;
}}

document.getElementById('tag-modal-bg').addEventListener('click', function(e) {{
  if (e.target === this) closeTagModal();
}});

_rebuildTagFilter();
render();
</script>
{_IMPORT_JS}
</body>
</html>"""

    try:
        output_path.write_text(page_html, encoding="utf-8")
    except OSError as exc:
        logger.error(f"  ERROR: could not write transactions page -> {output_path}: {exc}")
        return
    logger.info(f"  Transactions page -> {output_path} ({len(df)} rows)")


# ── Transfers review page ─────────────────────────────────────────────────────

def prepare_transfers_data(config: dict) -> dict:
    """Load saved transfer candidates and build HTML pair cards.

    Reads from the saved JSON only — does NOT re-run detection or AI scoring.
    Returns: {pending_html, confirmed_html, dismissed_html,
              pending_count, confirmed_count, dismissed_count}
    """
    from src.transfer_detector import load_transfer_candidates, _LABELS

    candidates = load_transfer_candidates(config)
    all_pairs = candidates.get("pairs", [])
    pending   = [p for p in all_pairs if p["status"] == "pending"]
    confirmed = [p for p in all_pairs if p["status"] == "confirmed"]
    dismissed = [p for p in all_pairs if p["status"] == "dismissed"]

    label_opts = "".join(f'<option value="{l}">{l}</option>' for l in _LABELS)

    def _conf_badge(conf) -> str:
        if conf is None or conf == -1:
            return '<span class="ai-badge ai-na">AI: N/A</span>'
        color = "#00BB77" if conf >= 7 else ("#E9C46A" if conf >= 4 else "#E63946")
        return f'<span class="ai-badge" style="background:{color}22;color:{color};border-color:{color}44">AI: {conf}/10</span>'

    def _pair_card(pair: dict, collapsed: bool = False) -> str:
        pid    = html.escape(pair["pair_id"])
        a, b   = pair["txn_a"], pair["txn_b"]
        amt    = pair["amount"]
        days   = pair["days_apart"]
        note   = html.escape(pair.get("ai_note") or "")
        label  = html.escape(pair.get("label") or "Family Loan")
        status = pair["status"]
        conf_html = _conf_badge(pair.get("ai_confidence"))

        status_strip = ""
        if status == "confirmed":
            status_strip = f'<div class="status-strip confirmed-strip">&#10003; Confirmed as {label}</div>'
        elif status == "dismissed":
            status_strip = '<div class="status-strip dismissed-strip">&#10007; Dismissed</div>'

        actions = ""
        if status == "pending":
            actions = (
                f'<div class="pair-actions">'
                f'<select class="label-sel">{label_opts.replace(f"value=\"{label}\"", f"value=\"{label}\" selected")}</select>'
                f'<button class="btn-confirm" onclick="decidePair(this,\'confirm\')">&#10003; Confirm Transfer</button>'
                f'<button class="btn-dismiss" onclick="decidePair(this,\'dismiss\')">&#10007; Dismiss</button>'
                f'</div>'
            )

        note_html = f'<div class="ai-note">{note}</div>' if note else ""
        style_attr = 'style="display:none"' if collapsed else ""

        return (
            f'<div class="pair-card {status}" id="pair-{pid}" {style_attr}>'
            f'{status_strip}'
            f'<div class="pair-header">'
            f'<span class="pair-amount">${amt:,.2f}</span>'
            f'<span class="pair-sep">&middot;</span>'
            f'<span class="pair-days">{days} day{"s" if days != 1 else ""} apart</span>'
            f'{conf_html}'
            f'</div>'
            f'{note_html}'
            f'<div class="pair-row out-row">'
            f'<span class="dir-badge out-badge">OUT</span>'
            f'<span class="pr-date">{a["date"]}</span>'
            f'<span class="pr-desc" title="{html.escape(a["description"])}">{html.escape(a["description"][:60])}</span>'
            f'<span class="pr-acct">{html.escape(a["account"])}</span>'
            f'<span class="pr-cat">{html.escape(a["category"])}</span>'
            f'<span class="pr-amt neg">-${amt:,.2f}</span>'
            f'</div>'
            f'<div class="pair-row in-row">'
            f'<span class="dir-badge in-badge">IN</span>'
            f'<span class="pr-date">{b["date"]}</span>'
            f'<span class="pr-desc" title="{html.escape(b["description"])}">{html.escape(b["description"][:60])}</span>'
            f'<span class="pr-acct">{html.escape(b["account"])}</span>'
            f'<span class="pr-cat">{html.escape(b["category"])}</span>'
            f'<span class="pr-amt pos">+${amt:,.2f}</span>'
            f'</div>'
            f'{actions}'
            f'</div>'
        )

    pending_html   = "".join(_pair_card(p) for p in pending) or \
        '<p class="empty-msg">No pending transfer pairs detected.</p>'
    confirmed_html = "".join(_pair_card(p) for p in confirmed) or \
        '<p class="empty-msg">No confirmed transfers yet.</p>'
    dismissed_html = "".join(_pair_card(p, collapsed=True) for p in dismissed) or \
        '<p class="empty-msg">No dismissed pairs.</p>'

    return {
        "pending_html":    pending_html,
        "confirmed_html":  confirmed_html,
        "dismissed_html":  dismissed_html,
        "pending_count":   len(pending),
        "confirmed_count": len(confirmed),
        "dismissed_count": len(dismissed),
    }


def generate_transfers_page(df: pd.DataFrame, config: dict, output_dir: Path) -> None:
    """Generate reports/transfers.html — potential transfer pair review."""
    from src.transfer_detector import (
        find_transfer_pairs, score_pairs_with_ai,
        load_transfer_candidates, save_transfer_candidates, merge_candidates,
        _LABELS, _SKIP_CATS,
    )

    output_path = output_dir / "transfers.html"

    new_pairs = find_transfer_pairs(df, config=config)
    candidates = load_transfer_candidates(config)
    candidates = merge_candidates(candidates, new_pairs)

    # Prune orphaned pairs: txn no longer in df, or its current category is now skipped
    # (catches stale pairs from before a category rename or re-categorisation)
    txn_ids_str = df["txn_id"].astype(str)
    live_ids = set(txn_ids_str)
    cat_map  = dict(zip(txn_ids_str, df["category"].astype(str)))
    candidates["pairs"] = [
        p for p in candidates["pairs"]
        if (p["txn_a"]["txn_id"] in live_ids
            and p["txn_b"]["txn_id"] in live_ids
            and cat_map.get(p["txn_a"]["txn_id"], "") not in _SKIP_CATS
            and cat_map.get(p["txn_b"]["txn_id"], "") not in _SKIP_CATS)
    ]

    candidates["pairs"] = score_pairs_with_ai(candidates["pairs"], config)
    save_transfer_candidates(candidates, config)

    all_pairs   = candidates["pairs"]
    pending     = [p for p in all_pairs if p["status"] == "pending"]
    confirmed   = [p for p in all_pairs if p["status"] == "confirmed"]
    dismissed   = [p for p in all_pairs if p["status"] == "dismissed"]

    label_opts  = "".join(f'<option value="{l}">{l}</option>' for l in _LABELS)

    def _conf_badge(conf) -> str:
        if conf is None or conf == -1:
            return '<span class="ai-badge ai-na">AI: N/A</span>'
        color = "#00BB77" if conf >= 7 else ("#E9C46A" if conf >= 4 else "#E63946")
        return f'<span class="ai-badge" style="background:{color}22;color:{color};border-color:{color}44">AI: {conf}/10</span>'

    def _pair_card(pair: dict, collapsed: bool = False) -> str:
        pid   = html.escape(pair["pair_id"])
        a, b  = pair["txn_a"], pair["txn_b"]
        amt   = pair["amount"]
        days  = pair["days_apart"]
        note  = html.escape(pair.get("ai_note") or "")
        label = html.escape(pair.get("label") or "Family Loan")
        status = pair["status"]
        conf_html = _conf_badge(pair.get("ai_confidence"))

        status_strip = ""
        if status == "confirmed":
            status_strip = f'<div class="status-strip confirmed-strip">✓ Confirmed as {label}</div>'
        elif status == "dismissed":
            status_strip = '<div class="status-strip dismissed-strip">✕ Dismissed</div>'

        actions = ""
        if status == "pending":
            actions = f"""
    <div class="pair-actions">
      <select class="label-sel">{label_opts.replace(f'value="{label}"', f'value="{label}" selected')}</select>
      <button class="btn-confirm" onclick="decidePair(this,'confirm')">&#10003; Confirm Transfer</button>
      <button class="btn-dismiss" onclick="decidePair(this,'dismiss')">&#10007; Dismiss</button>
    </div>"""

        note_html = f'<div class="ai-note">{note}</div>' if note else ""
        style = 'style="display:none"' if collapsed else ""

        return f"""
<div class="pair-card {status}" id="pair-{pid}" {style}>
  {status_strip}
  <div class="pair-header">
    <span class="pair-amount">${amt:,.2f}</span>
    <span class="pair-sep">&middot;</span>
    <span class="pair-days">{days} day{"s" if days != 1 else ""} apart</span>
    {conf_html}
  </div>
  {note_html}
  <div class="pair-row out-row">
    <span class="dir-badge out-badge">OUT</span>
    <span class="pr-date">{a["date"]}</span>
    <span class="pr-desc" title="{html.escape(a["description"])}">{html.escape(a["description"][:60])}</span>
    <span class="pr-acct">{html.escape(a["account"])}</span>
    <span class="pr-cat">{html.escape(a["category"])}</span>
    <span class="pr-amt neg">-${amt:,.2f}</span>
  </div>
  <div class="pair-row in-row">
    <span class="dir-badge in-badge">IN</span>
    <span class="pr-date">{b["date"]}</span>
    <span class="pr-desc" title="{html.escape(b["description"])}">{html.escape(b["description"][:60])}</span>
    <span class="pr-acct">{html.escape(b["account"])}</span>
    <span class="pr-cat">{html.escape(b["category"])}</span>
    <span class="pr-amt pos">+${amt:,.2f}</span>
  </div>
  {actions}
</div>"""

    pending_html   = "".join(_pair_card(p) for p in pending) or \
        '<p class="empty-msg">No pending transfer pairs detected.</p>'
    confirmed_html = "".join(_pair_card(p, collapsed=False) for p in confirmed) or \
        '<p class="empty-msg">No confirmed transfers yet.</p>'
    dismissed_html = "".join(_pair_card(p, collapsed=True)  for p in dismissed) or \
        '<p class="empty-msg">No dismissed pairs.</p>'

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transfers ({len(pending)} pending)</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f0f4f8;margin:0;padding:20px 24px;color:#1a2535}}
.site-nav{{background:#1d3540;padding:0 24px;display:flex;gap:4px;border-radius:10px;
           margin-bottom:20px;align-items:center}}
.nav-link{{color:rgba(255,255,255,.65);text-decoration:none;padding:12px 18px;
           font-size:.88rem;font-weight:600;border-bottom:3px solid transparent}}
.nav-link:hover{{color:white}}
.nav-link.active{{color:white;border-bottom-color:#2a9d8f}}
.tab-bar{{display:flex;gap:2px;background:#1d3540;border-radius:10px;padding:6px 12px;
          margin-bottom:20px;flex-wrap:wrap;align-items:center}}
.tab-btn{{padding:8px 16px;border:none;border-radius:7px;background:transparent;
          color:rgba(255,255,255,.55);font-size:.82rem;font-weight:600;cursor:pointer;
          border-bottom:3px solid transparent;text-decoration:none;display:inline-block}}
.tab-btn.active{{background:rgba(42,157,143,.15);color:#2a9d8f;border-bottom-color:#2a9d8f}}
.tab-btn:hover:not(.active){{color:white}}
.header{{background:linear-gradient(135deg,#264653 0%,#2a7b8e 100%);color:white;
         padding:24px 32px;border-radius:12px;margin-bottom:20px}}
.header h1{{margin:0 0 4px;font-size:1.5rem}}
.header p{{margin:0;opacity:.75;font-size:.9rem}}
.section-title{{font-size:.85rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
                color:#64748b;margin:24px 0 10px;display:flex;align-items:center;gap:8px}}
.section-count{{background:#e2e8f0;color:#475569;border-radius:12px;padding:1px 8px;font-size:.78rem}}
.pair-card{{background:white;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.07);
            margin-bottom:14px;overflow:hidden;border:1px solid #e2e8f0}}
.pair-card.confirmed{{border-left:4px solid #00BB77}}
.pair-card.dismissed{{border-left:4px solid #cbd5e1;opacity:.7}}
.pair-card.pending{{border-left:4px solid #457B9D}}
.status-strip{{padding:7px 16px;font-size:.82rem;font-weight:600}}
.confirmed-strip{{background:#f0fdf4;color:#15803d}}
.dismissed-strip{{background:#f8fafc;color:#94a3b8}}
.pair-header{{display:flex;align-items:center;gap:10px;padding:12px 16px 6px;flex-wrap:wrap}}
.pair-amount{{font-size:1.1rem;font-weight:700;color:#1a2535}}
.pair-sep{{color:#94a3b8}}
.pair-days{{font-size:.82rem;color:#64748b}}
.ai-badge{{font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;border:1px solid}}
.ai-na{{background:#f1f5f9;color:#64748b;border-color:#cbd5e1}}
.ai-note{{font-size:.8rem;color:#64748b;font-style:italic;padding:0 16px 8px;line-height:1.4}}
.pair-row{{display:flex;align-items:center;gap:10px;padding:7px 16px;font-size:.83rem;
           border-top:1px solid #f1f5f9;flex-wrap:wrap}}
.out-row{{background:#fff8f8}}
.in-row{{background:#f8fff9}}
.dir-badge{{font-size:.7rem;font-weight:700;padding:2px 7px;border-radius:8px;white-space:nowrap}}
.out-badge{{background:#fee2e2;color:#dc2626}}
.in-badge{{background:#dcfce7;color:#16a34a}}
.pr-date{{color:#64748b;white-space:nowrap;min-width:90px}}
.pr-desc{{flex:1;min-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.pr-acct{{color:#64748b;font-size:.78rem;white-space:nowrap}}
.pr-cat{{font-size:.75rem;background:#f1f5f9;padding:1px 6px;border-radius:6px;white-space:nowrap}}
.pr-amt{{font-weight:700;white-space:nowrap;margin-left:auto}}
.neg{{color:#E63946}}
.pos{{color:#00BB77}}
.pair-actions{{display:flex;gap:10px;align-items:center;padding:12px 16px;
               background:#f8fafc;border-top:1px solid #f1f5f9;flex-wrap:wrap}}
.label-sel{{padding:7px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:.85rem;
            background:white;color:#1a2535}}
.btn-confirm{{padding:7px 16px;background:#2a9d8f;color:white;border:none;border-radius:8px;
              cursor:pointer;font-size:.85rem;font-weight:600}}
.btn-confirm:hover{{background:#21867a}}
.btn-dismiss{{padding:7px 16px;background:white;color:#64748b;border:1px solid #cbd5e1;
              border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:600}}
.btn-dismiss:hover{{background:#f1f5f9}}
.empty-msg{{color:#94a3b8;font-style:italic;padding:12px 0}}
.toggle-btn{{font-size:.8rem;color:#2a9d8f;background:none;border:none;cursor:pointer;
             padding:0;font-weight:600}}
.saved-banner{{display:none;background:#00BB77;color:white;padding:10px 20px;
               border-radius:8px;margin-bottom:16px;font-weight:600}}
{_IMPORT_CSS}
</style>
</head>
<body>
{_build_nav_html("transfers")}
<div class="header">
  <h1>Transfer Pairs</h1>
  <p>Transactions with equal and opposite amounts within 60 days &mdash; potential loans, reimbursements or internal transfers.</p>
</div>
{_help_box(
    "How to use this page",
    "Suggested pairs of transactions that may represent money moving between your own accounts.",
    [
        "<strong>Confirm</strong> a pair to categorise both sides as Transfers &mdash; they will be excluded from spending reports and charts.",
        "<strong>Dismiss</strong> a pair to hide it without changing either transaction's category.",
        "Confirmed transfers appear in the Confirmed section at the bottom of the page.",
        "New pairs are detected each time the importer runs.",
    ]
)}
<div class="saved-banner" id="banner"></div>

<div class="section-title">
  Pending Review <span class="section-count">{len(pending)}</span>
</div>
{pending_html}

<div class="section-title" style="margin-top:32px">
  Confirmed <span class="section-count">{len(confirmed)}</span>
</div>
{confirmed_html}

<div class="section-title" style="margin-top:32px">
  Dismissed
  <span class="section-count">{len(dismissed)}</span>
  <button class="toggle-btn" onclick="toggleDismissed()">show / hide</button>
</div>
<div id="dismissed-section">
{dismissed_html}
</div>

<script>
function decidePair(btn, action) {{
  const card = btn.closest('.pair-card');
  const pairId = card.id.replace('pair-', '');
  const label = card.querySelector('.label-sel')?.value || 'Family Loan';
  btn.disabled = true;
  btn.closest('.pair-actions').querySelectorAll('button').forEach(b => b.disabled = true);
  fetch('/api/transfer-decision', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{pair_id: pairId, action: action, label: label}})
  }})
  .then(r => r.json())
  .then(data => {{
    if (data.ok) {{
      const strip = document.createElement('div');
      if (action === 'confirm') {{
        card.classList.add('confirmed');
        strip.className = 'status-strip confirmed-strip';
        strip.textContent = '\\u2713 Confirmed as ' + label;
        if (data.loan_link_needed) {{
          const lnk = document.createElement('a');
          lnk.href = '/financial-goals';
          lnk.textContent = ' \\u2192 Link to loan record';
          lnk.style.cssText = 'color:#2a9d8f;font-size:.8rem;margin-left:8px;text-decoration:underline';
          strip.appendChild(lnk);
        }}
      }} else {{
        card.classList.add('dismissed');
        strip.className = 'status-strip dismissed-strip';
        strip.textContent = '\\u00d7 Dismissed';
      }}
      card.insertBefore(strip, card.firstChild);
      card.querySelector('.pair-actions').remove();
      const banner = document.getElementById('banner');
      banner.textContent = action === 'confirm'
        ? '\\u2713 Transfer confirmed — both transactions categorised as Transfers.'
        : 'Pair dismissed.';
      if (action === 'confirm' && data.loan_link_needed) {{
        banner.textContent += ' These transactions are not yet linked to a loan record.';
      }}
      banner.style.display = 'block';
      setTimeout(() => {{ banner.style.display = 'none'; }}, 4000);
    }} else {{
      alert('Error: ' + (data.error || 'Unknown error'));
      card.querySelectorAll('button').forEach(b => b.disabled = false);
    }}
  }})
  .catch(e => {{
    alert('Network error: ' + e);
    card.querySelectorAll('button').forEach(b => b.disabled = false);
  }});
}}

function toggleDismissed() {{
  const sec = document.getElementById('dismissed-section');
  sec.querySelectorAll('.pair-card').forEach(c => {{
    c.style.display = c.style.display === 'none' ? '' : 'none';
  }});
}}
</script>
{_IMPORT_JS}
</body>
</html>"""

    try:
        output_path.write_text(page_html, encoding="utf-8")
    except OSError as exc:
        logger.error(f"  ERROR: could not write transfers page -> {output_path}: {exc}")
        return
    logger.info(f"  Transfers page -> {output_path} ({len(pending)} pending, {len(confirmed)} confirmed)")


def prepare_net_worth_data(balances_df: pd.DataFrame, config: dict) -> dict:
    """Build chart HTML and table data for the Net Worth live route.

    Returns {has_data: False} when balances_df is empty, otherwise
    {has_data, chart_html, table_body_html, account_count, total_balance}.
    chart_html is a Plotly div rendered without the plotly.js bundle (caller
    must include plotly.js in the page <head>).
    """
    if balances_df.empty:
        return {"has_data": False}

    balances_df = balances_df.copy().sort_values(["account", "date"])
    accounts = sorted(balances_df["account"].unique())
    account_colors = list(CATEGORY_COLORS.values())

    fig = go.Figure()
    for i, acct in enumerate(accounts):
        acct_df = balances_df[balances_df["account"] == acct]
        fig.add_trace(go.Scatter(
            x=acct_df["date"],
            y=acct_df["balance"],
            mode="lines+markers",
            name=acct,
            line=dict(width=2, color=account_colors[i % len(account_colors)]),
            marker=dict(size=6),
            hovertemplate="<b>%{fullData.name}</b><br>%{x|%b %Y}: $%{y:,.2f}<extra></extra>",
        ))

    if len(accounts) > 1:
        pivot = (
            balances_df
            .pivot_table(index="date", columns="account", values="balance", aggfunc="last")
            .resample("ME")
            .last()
            .ffill()
        )
        total = pivot.sum(axis=1)
        fig.add_trace(go.Scatter(
            x=total.index,
            y=total.values,
            mode="lines+markers",
            name="Net Worth (total)",
            line=dict(width=3, dash="dot", color="#00BB77"),
            marker=dict(size=7, symbol="diamond"),
            hovertemplate="<b>Net Worth</b><br>%{x|%b %Y}: $%{y:,.2f}<extra></extra>",
        ))

    fig.update_layout(
        title="Account Balances & Net Worth",
        xaxis_title="Date",
        yaxis=dict(title="Balance (AUD)", tickformat="$,.0f"),
        template="plotly_white",
        height=480,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    chart_html = fig.to_html(include_plotlyjs=False, full_html=False)

    latest = (
        balances_df.sort_values("date")
        .groupby("account", as_index=False)
        .last()
        [["account", "account_type", "date", "balance"]]
        .sort_values("balance", ascending=False)
    )
    total_balance = latest["balance"].sum()

    table_rows = []
    for _, row in latest.iterrows():
        table_rows.append(
            f"<tr>"
            f"<td style='padding:8px 14px'>{html.escape(str(row['account']))}</td>"
            f"<td style='padding:8px 14px;color:#555;font-size:.82rem'>{html.escape(str(row['account_type']))}</td>"
            f"<td style='padding:8px 14px;white-space:nowrap'>"
            f"{row['date'].strftime('%d %b %Y') if hasattr(row['date'], 'strftime') else str(row['date'])[:10]}</td>"
            f"<td style='padding:8px 14px;text-align:right;font-weight:600'>${row['balance']:,.2f}</td>"
            f"</tr>"
        )

    return {
        "has_data": True,
        "chart_html": chart_html,
        "table_body_html": "".join(table_rows),
        "account_count": len(accounts),
        "total_balance": total_balance,
    }


def generate_net_worth_report(balances_df: pd.DataFrame, config: dict, output_dir: Path) -> go.Figure | None:
    """Generate reports/net_worth.html — account balances and total net worth over time."""
    output_path = output_dir / "net_worth.html"

    if balances_df.empty:
        empty_html = (
            "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Net Worth</title>"
            "<style>"
            "*,*::before,*::after{box-sizing:border-box}"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
            "     background:#f0f4f8;margin:0;padding:20px 24px;color:#1a1a2e}"
            ".site-nav{background:#1d3540;padding:0 24px;display:flex;gap:4px;"
            "           border-radius:10px;margin-bottom:20px}"
            ".nav-link{color:rgba(255,255,255,.65);text-decoration:none;padding:12px 18px;"
            "           font-size:.88rem;font-weight:600;border-bottom:3px solid transparent}"
            ".nav-link:hover{color:white}"
            ".nav-link.active{color:white;border-bottom-color:#2a9d8f}"
            ".tab-bar{display:flex;gap:2px;background:#1d3540;border-radius:10px;"
            "          padding:6px 12px;margin-bottom:20px;flex-wrap:wrap}"
            ".tab-btn{padding:8px 16px;border:none;border-radius:7px;background:transparent;"
            "          color:rgba(255,255,255,.55);font-size:.82rem;font-weight:600;cursor:pointer;"
            "          border-bottom:3px solid transparent;text-decoration:none;display:inline-block}"
            ".tab-btn.active{background:rgba(42,157,143,.15);color:#2a9d8f;border-bottom-color:#2a9d8f}"
            ".tab-btn:hover:not(.active){color:white}"
            ".header{background:linear-gradient(135deg,#264653 0%,#2a7b8e 100%);color:white;"
            "         padding:24px 32px;border-radius:12px;margin-bottom:20px}"
            ".header h1{margin:0 0 4px;font-size:1.5rem}"
            ".header p{margin:0;opacity:.75;font-size:.9rem}"
            ".empty-card{background:white;border-radius:12px;padding:40px 32px;"
            "             box-shadow:0 2px 10px rgba(0,0,0,.07);text-align:center;color:#64748b}"
            ".empty-card h3{margin:0 0 8px;color:#334155;font-size:1.1rem}"
            ".empty-card p{margin:0;font-size:.9rem;line-height:1.6}"
            ".empty-card code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:.85rem}"
            "</style></head><body>"
            "<nav class='site-nav'>"
            "  <a href='monthly_summary.html' class='nav-link'>Dashboard</a>"
            "  <a href='monthly_summary.html#reports' class='nav-link'>Reports</a>"
            "  <a href='net_worth.html' class='nav-link active'>Data</a>"
            "  <a href='/settings/accounts' class='nav-link'>Settings</a>"
            "  <a href='/help' class='nav-link'>Help</a>"
            "</nav>"
            "<div class='tab-bar'>"
            "  <a href='transactions.html' class='tab-btn'>All Transactions</a>"
            "  <a href='review.html' class='tab-btn'>Review</a>"
            "  <a href='transfers.html' class='tab-btn'>Transfers</a>"
            "  <a href='fy_summary.html' class='tab-btn'>FY Summary</a>"
            "  <a href='net_worth.html' class='tab-btn active'>Net Worth</a>"
            "  <a href='/commitments' class='tab-btn'>Commitments</a>"
            "  <a href='/merchant-rules' class='tab-btn'>Merchant Rules</a>"
            "  <a href='/reimbursements' class='tab-btn'>Reimbursements</a>"
            "  <a href='/superseded-pairs' class='tab-btn'>Superseded</a>"
            "  <a href='/recommendations' class='tab-btn'>Recommendations</a>"
            "</div>"
            "<div class='header'>"
            "  <h1>Net Worth</h1>"
            "  <p>No balance snapshots recorded yet.</p>"
            "</div>"
            "<div class='empty-card'>"
            "  <h3>No balance history yet</h3>"
            "  <p>Add closing balances to <code>data/account_balances.csv</code> then run the importer"
            "  or click Refresh Charts. Columns: <code>date, account, balance</code>.</p>"
            "</div>"
            "</body></html>"
        )
        output_path.write_text(empty_html, encoding="utf-8")
        logger.info(f"  Net worth -> {output_path} (no data)")
        return None

    balances_df = balances_df.copy().sort_values(["account", "date"])
    accounts = sorted(balances_df["account"].unique())

    # ── Plotly chart ──────────────────────────────────────────────────────────
    fig = go.Figure()
    account_colors = list(CATEGORY_COLORS.values())

    for i, acct in enumerate(accounts):
        acct_df = balances_df[balances_df["account"] == acct]
        fig.add_trace(go.Scatter(
            x=acct_df["date"],
            y=acct_df["balance"],
            mode="lines+markers",
            name=acct,
            line=dict(width=2, color=account_colors[i % len(account_colors)]),
            marker=dict(size=6),
            hovertemplate="<b>%{fullData.name}</b><br>%{x|%b %Y}: $%{y:,.2f}<extra></extra>",
        ))

    # Total net worth line — pivot, forward-fill monthly, sum
    if len(accounts) > 1:
        pivot = (
            balances_df
            .pivot_table(index="date", columns="account", values="balance", aggfunc="last")
            .resample("ME")
            .last()
            .ffill()
        )
        total = pivot.sum(axis=1)
        fig.add_trace(go.Scatter(
            x=total.index,
            y=total.values,
            mode="lines+markers",
            name="Net Worth (total)",
            line=dict(width=3, dash="dot", color="#00BB77"),
            marker=dict(size=7, symbol="diamond"),
            hovertemplate="<b>Net Worth</b><br>%{x|%b %Y}: $%{y:,.2f}<extra></extra>",
        ))

    fig.update_layout(
        title="Account Balances & Net Worth",
        xaxis_title="Date",
        yaxis=dict(title="Balance (AUD)", tickformat="$,.0f"),
        template="plotly_white",
        height=480,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    # PNG export omitted — PNG generation via kaleido is optional

    # ── Balance table: latest snapshot per account ────────────────────────────
    latest = (
        balances_df.sort_values("date")
        .groupby("account", as_index=False)
        .last()
        [["account", "account_type", "date", "balance"]]
        .sort_values("balance", ascending=False)
    )
    total_balance = latest["balance"].sum()

    table_rows = []
    for _, row in latest.iterrows():
        table_rows.append(
            f"<tr>"
            f"<td style='padding:8px 14px'>{html.escape(str(row['account']))}</td>"
            f"<td style='padding:8px 14px;color:#555;font-size:.82rem'>{html.escape(str(row['account_type']))}</td>"
            f"<td style='padding:8px 14px;white-space:nowrap'>{row['date'].strftime('%d %b %Y') if hasattr(row['date'],'strftime') else str(row['date'])[:10]}</td>"
            f"<td style='padding:8px 14px;text-align:right;font-weight:600'>${row['balance']:,.2f}</td>"
            f"</tr>"
        )

    chart_html = fig.to_html(include_plotlyjs="cdn", full_html=False)
    account_count = len(accounts)
    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Net Worth</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f0f4f8;margin:0;padding:20px 24px;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#264653 0%,#2a7b8e 100%);color:white;
         padding:24px 32px;border-radius:12px;margin-bottom:20px}}
.header h1{{margin:0 0 4px;font-size:1.5rem}}
.header p{{margin:0;opacity:.75;font-size:.9rem}}
.card{{background:white;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.07);
       overflow:hidden;margin-bottom:20px}}
.site-nav{{background:#1d3540;padding:0 24px;display:flex;gap:4px;
           border-radius:10px;margin-bottom:20px}}
.nav-link{{color:rgba(255,255,255,.65);text-decoration:none;padding:12px 18px;
           font-size:.88rem;font-weight:600;border-bottom:3px solid transparent}}
.nav-link:hover{{color:white}}
.nav-link.active{{color:white;border-bottom-color:#2a9d8f}}
.tab-bar{{display:flex;gap:2px;background:#1d3540;border-radius:10px;
          padding:6px 12px;margin-bottom:20px;flex-wrap:wrap;align-items:center}}
.tab-btn{{padding:8px 16px;border:none;border-radius:7px;background:transparent;
          color:rgba(255,255,255,.55);font-size:.82rem;font-weight:600;cursor:pointer;
          border-bottom:3px solid transparent;text-decoration:none;display:inline-block}}
.tab-btn.active{{background:rgba(42,157,143,.15);color:#2a9d8f;border-bottom-color:#2a9d8f}}
.tab-btn:hover:not(.active){{color:white}}
{_IMPORT_CSS}
</style>
</head>
<body>
{_build_nav_html("net_worth")}
<div class="header">
  <h1>Net Worth</h1>
  <p>Closing balances across {account_count} account(s). Total as of latest snapshots: <strong>${total_balance:,.2f}</strong></p>
</div>
{_help_box(
    "How to use this page",
    "Track account balances over time to see total net worth at a glance.",
    [
        "Add balance snapshots in <code>data/account_balances.csv</code> &mdash; one row per account per date.",
        "The chart updates automatically each time the importer runs.",
        "Each line on the chart is one account; the bold line is your total net worth.",
        "Use the table below the chart to add or edit individual balance snapshots.",
    ]
)}
<div class="card" style="padding:8px">
{chart_html}
</div>
<div class="card">
  <div style="padding:16px 20px;background:#1d3540;color:white">
    <strong style="font-size:.95rem">Latest Balance Snapshots</strong>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:.88rem">
    <thead><tr style="background:#264653;color:white">
      <th style="padding:8px 14px;text-align:left">Account</th>
      <th style="padding:8px 14px;text-align:left">Type</th>
      <th style="padding:8px 14px;text-align:left">As Of</th>
      <th style="padding:8px 14px;text-align:right">Balance</th>
    </tr></thead>
    <tbody>
      {''.join(table_rows)}
      <tr style="background:#e8f5e9;font-weight:700;border-top:2px solid #c8e6c9">
        <td colspan="3" style="padding:8px 14px">Total</td>
        <td style="padding:8px 14px;text-align:right">${total_balance:,.2f}</td>
      </tr>
    </tbody>
  </table>
</div>
<p style="font-size:.78rem;color:#aaa;padding:4px 0 16px">
  Note: includes only accounts where balance data is available from imported statements.
  Credit card liabilities are not yet included.
</p>
{_IMPORT_JS}
</body>
</html>"""

    try:
        output_path.write_text(page_html, encoding="utf-8")
    except OSError as exc:
        logger.error(f"  ERROR: could not write net worth page -> {output_path}: {exc}")
        return None
    logger.info(f"  Net worth -> {output_path} ({account_count} account(s))")
    return fig


# ── Entry point ───────────────────────────────────────────────────────────────

def generate_all_reports(df: pd.DataFrame, config: dict) -> None:
    from concurrent.futures import ThreadPoolExecutor, wait as _wait, ALL_COMPLETED
    output_dir = Path(config["data"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    from src.balance_tracker import load_balance_history
    balances_df = load_balance_history(config)

    # Run the 5 independent report generators in parallel
    with ThreadPoolExecutor(max_workers=5) as _pool:
        _futs = [
            _pool.submit(generate_net_worth_report,   balances_df, config, output_dir),
            _pool.submit(generate_dashboard,          df, config, output_dir),
            _pool.submit(generate_review_page,        df, config, output_dir),
            _pool.submit(generate_transactions_page,  df, config, output_dir),
            _pool.submit(generate_fy_summary,         df, config, output_dir),
        ]
        _wait(_futs, return_when=ALL_COMPLETED)
    for _f in _futs:
        _f.result()  # re-raise any worker exception

    # Transfers runs after the parallel batch (mutates transfer_candidates.json)
    generate_transfers_page(df, config, output_dir)

    # Auto-detect recurring commitments and merge into data/commitments.json
    from src.commitment_detector import (
        detect_recurring_commitments, load_commitments,
        merge_commitments, save_commitments,
    )
    detected   = detect_recurring_commitments(df)
    existing   = load_commitments(config)
    merged     = merge_commitments(existing, detected)
    save_commitments(merged, config)

    logger.info(f"  Charts saved to {output_dir}/")
