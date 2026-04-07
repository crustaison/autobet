# CLYDE PLAN - Autobet TODO Implementation
Last updated: 2026-04-07 | Written by Claude

---

## !! THE ONLY FILE THAT MATTERS !!

~/autobet/autobet_main.py

NOT server.py — that is dead Clyde-era code, not running.
NOT server_simple.py — dead.
NOT server.py.bak — dead.
NOT server_minimal.py — dead.
NOT any other .py file in ~/autobet/

The running process is: python3 autobet_main.py
Confirm with: pgrep -a python3 | grep autobet
The dashboard is at: http://ryz.local:7778/

If you edit anything other than autobet_main.py, nothing will change and you will be confused.

---

## CRITICAL RULES

1. All code in ONE file: ~/autobet/autobet_main.py (~3600 lines). Do not create new files.
2. Always syntax-check first: python3 -c "import ast; ast.parse(open('autobet_main.py').read()); print('OK')"
3. Always restart after changes: bash ~/autobet/start.sh
4. Push to GitHub: cd ~/autobet && git add autobet_main.py && git commit -m "..." && git push
5. DO NOT REMOVE OR CHANGE THESE:
   - ENTRY_FLOOR=0.05 and ENTRY_CEILING=0.95 in check_risk() -- hard entry bans
   - MAX_CONTRACTS=500 cap in decision_loop and replay
   - Fallback is DISABLED -- must stay disabled (was net negative on every coin)
   - WAL mode in db_connect(): conn.execute("PRAGMA journal_mode=WAL")
   - get_active_run_id(coin, conn=None) -- the conn= param is required to prevent DB locks

---

## KEY CONSTANTS (for reference)

COINS = ["BTC", "XRP", "SOL", "ETH"]
ENTRY_FLOOR = 0.05, ENTRY_CEILING = 0.95, MAX_CONTRACTS = 500
KALSHI_FEE_RATE = 0.07, STARTING_CAPITAL = 1000.0, TRADE_SIZE = 20.0
DB_PATH = ~/autobet/data/autobet.db
BETBOT_SIGNAL_FILES maps to ~/autoresearch/data/kalshi_signals[_eth|_sol|_xrp].json

---

## TASK 1 - Full Rationale Hover on Decisions Tables (Easy -- do this first)

Problem: Rationale text is truncated to 50-60 chars in all decision/trade tables.
Fix: Use native HTML title= attribute for full-text tooltip on hover. No JS needed.

Search the file for these patterns and fix each one:
  - (rationale or "")[:50]
  - rat[:60]
  - rationale[:50]

For each one, change the surrounding td tag to:

  rat_display = rat[:55] + "..." if len(rat) > 55 else rat
  rat_escaped = rat.replace('"', "&quot;").replace("'", "&#39;")
  # then in the td:
  # style="font-size:11px;cursor:help" title="{rat_escaped}"
  # content: {rat_display}

---

## TASK 2 - Sortable Table Columns (Medium)

Add click-to-sort on all trade/decision tables.

Step 1: Find page_shell() function. Find where the HTML template ends (look for </body></html>).
Add a script block just before </body>:

  <script>
  function sortTable(th) {
    var table = th.closest("table");
    var idx = Array.from(th.parentNode.children).indexOf(th);
    var asc = th.dataset.sort !== "asc";
    th.dataset.sort = asc ? "asc" : "desc";
    var rows = Array.from(table.querySelectorAll("tr:not(:first-child)"));
    rows.sort(function(a,b){
      var av=(a.children[idx]||{}).innerText||"";
      var bv=(b.children[idx]||{}).innerText||"";
      av=av.replace(/[$+,]/g,""); bv=bv.replace(/[$+,]/g,"");
      var an=parseFloat(av),bn=parseFloat(bv);
      if(!isNaN(an)&&!isNaN(bn)) return asc?an-bn:bn-an;
      return asc?av.localeCompare(bv):bv.localeCompare(av);
    });
    rows.forEach(function(r){table.appendChild(r);});
  }
  </script>

Step 2: On every <th> element in trade/decision/tick tables, add:
  onclick="sortTable(this)" style="cursor:pointer;user-select:none"

---

## TASK 3 - Per-Coin Min/Max Stake (Medium)

Add coin-specific stake ranges so BTC/ETH/SOL/XRP can have different sizes.

Step 1: Add get_setting() helper near db_connect() (check it doesn't already exist first):

  def get_setting(key, default=None):
      try:
          conn = db_connect()
          row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
          conn.close()
          return row[0] if row else default
      except:
          return default

Step 2: Find calc_stake(coin, confidence, capital) -- around line 3200.
Change the min_s / max_s lines to read coin-specific keys first:

  min_s = float(get_setting("min_stake_" + coin, get_setting("min_stake", 10)))
  max_s = float(get_setting("max_stake_" + coin, get_setting("max_stake", 30)))

Step 3: In build_settings_page(), add a "Per-Coin Stakes" card section after the Risk Engine section.
Form with a table -- one row per coin with min_stake and max_stake number inputs.
POST to /settings/coin-stakes.

Step 4: Add POST handler for /settings/coin-stakes in the request handler (look at how other
/settings/* POST routes work -- they parse form body and save to settings table).
Save min_stake_BTC, max_stake_BTC, min_stake_XRP, etc. using INSERT OR REPLACE INTO settings.

---

## TASK 4 - Rolling Win Rate + Size Scaling (Medium)

Automatically reduce trade size when a coin is on a losing streak.

In calc_stake(coin, confidence, capital), after computing the base size, add:

  try:
      conn = db_connect()
      recent = conn.execute(
          "SELECT result FROM paper_trades WHERE coin=? AND result IN ('WIN','LOSS') ORDER BY window_ts DESC LIMIT 10",
          (coin,)).fetchall()
      conn.close()
      if len(recent) >= 5:
          rwr = sum(1 for r in recent if r[0] == "WIN") / len(recent)
          if rwr < 0.35:
              size = size * 0.5    # half size on bad streak
          elif rwr < 0.45:
              size = size * 0.75   # reduce 25% on weak streak
  except:
      pass

In build_dashboard(), in the coin card loop (where each card is built), add a query for
last-10 results per coin and add a "Roll WR" stat row showing the rolling win rate:
  - green if >= 50%
  - yellow if 40-49%
  - red if < 40%
  - gray/muted if < 5 trades (not enough data)

---

## TASK 5 - AI Chat History Persistence (Medium-Hard)

Problem: Chat history is lost on page refresh.
Fix: Store in DB, load on page open.

Step 1: Add to db_migrate() (before the final conn.commit()):

  conn.execute("""CREATE TABLE IF NOT EXISTS chat_sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL DEFAULT 'anon',
      role TEXT NOT NULL,
      content TEXT NOT NULL,
      created_at TEXT
  )""")

Step 2: In handle_chat(message, user=None), before building the API request:
  - Get username = (user or {}).get("username", "anon")
  - Load last 20 rows: SELECT role, content FROM chat_sessions WHERE username=?
    ORDER BY id DESC LIMIT 20, then reverse to get chronological order
  - Build messages list: history rows + {"role": "user", "content": message}
  - After getting the reply text, INSERT both user message and assistant reply into chat_sessions
  - DELETE FROM chat_sessions WHERE username=? AND id NOT IN
    (SELECT id FROM chat_sessions WHERE username=? ORDER BY id DESC LIMIT 100)
    to keep only last 100 per user

Step 3: Add route GET /chat/history that:
  - Reads user from session (same pattern as other authenticated routes)
  - Queries last 20 chat_sessions rows for that user ORDER BY id ASC
  - Returns JSON: {"messages": [{"role": "user", "content": "..."}, ...]}

Step 4: In page_shell(), find the chat panel JavaScript section.
  Search for: sendChat or #chat-input or chat-panel.
  Find the button click handler that opens the chat panel.
  Add fetch("/chat/history") call that runs once when panel is first opened.
  On success, render each message into the chat display div.
  Use a boolean flag (historyLoaded = false) to only fetch once per page load.

---

## TASK 6 - Research Page Fixes (Medium)

Find build_research_page() in the file.

6a. Coin selector:
  The page reads BETBOT_SIGNAL_FILES["BTC"] hardcoded. Change to support ?coin= param.
  Look at how other pages parse query params -- search for "query_string" or "urlparse" usage.
  Default coin = "BTC" if not provided or invalid.
  Add tab buttons at top: BTC | XRP | SOL | ETH, each linking to /research?coin=X.
  Use BETBOT_SIGNAL_FILES.get(coin, BETBOT_SIGNAL_FILES["BTC"]) for the file path.

6b. Copy button:
  Find where <pre> code blocks are rendered (search for "<pre>" in build_research_page).
  Wrap each pre in a div with position:relative.
  Add a button inside with onclick="navigator.clipboard.writeText(this.nextElementSibling.innerText)"
  Style: position:absolute; right:8px; top:8px; font-size:11px; padding:2px 8px;
         background:#21262d; border:1px solid #444; border-radius:4px; color:#8b949e; cursor:pointer
  Button text: "Copy"

6c. Model display:
  Search for any static string like "M2.5", "M2.7", "unknown", or "minimax_model" in build_research_page.
  Replace with get_minimax_model() which reads from settings DB at runtime.

---

## TASK 7 - Export Page (Medium)

Add an Export page mirroring the Import page.

Step 1: Add build_export_page() function.
Page shows a simple card with download links:
  - Paper Trades CSV: /export/trades.csv
  - Decisions CSV: /export/decisions.csv
  - Kalshi Ticks CSV: /export/ticks.csv?coin=BTC (with coin selector)
  - Price History CSV: /export/prices.csv?coin=BTC (with coin selector)

Step 2: Add download route handlers for each:
  - Query the DB table
  - Use Python's csv module with io.StringIO to write CSV
  - Return as self.send_response(200) with headers:
      Content-Type: text/csv
      Content-Disposition: attachment; filename=trades.csv
  - Write the StringIO content as bytes

Step 3: Add "Export" to nav bar in page_shell().
Find the nav link HTML (search for "/import" in the nav section).
Add an Export link right after Import.

---

## TASK 8 - Engine Descriptions in UI (Easy)

In build_engines_page(), find where engine names/options are listed.
Add a short description under each engine name:

minimax_llm:
  "Calls MiniMax LLM API each 15-min window with order book snapshot and price context.
  Best signal quality. Uses API tokens. ~2-5s latency per coin."

rules_engine:
  "Simple threshold: Kalshi mid > 0.62 YES, mid < 0.38 NO, else PASS.
  Zero latency, no API calls. Good baseline for comparison."

vector_knn:
  "Finds the 10 most similar historical windows using 8-feature cosine similarity and votes direction.
  No API calls. Gets smarter as more trade history accumulates."

hybrid:
  "Rules gate (0.62/0.38 threshold) combined with KNN confidence boost.
  Both signals must agree before a trade fires. More conservative than either alone."

betbot_signal:
  "Reads signal files from betbot's autoresearch loop (~autoresearch/data/kalshi_signals*.json).
  MiniMax M2.7 iteratively rewrites the strategy script each window based on live P&L.
  Most sophisticated option. Requires betbot running on ryz.local."

---

## TESTING CHECKLIST (run after EVERY task)

1. python3 -c "import ast; ast.parse(open('autobet_main.py').read()); print('OK')"
2. bash ~/autobet/start.sh
3. tail -5 ~/autobet/autobet.log  -- must show "Dashboard at http://ryz.local:7778/"
4. Visit the affected page in browser, confirm it loads without 500 error
5. git add autobet_main.py && git commit -m "Task N: description" && git push

---

## DO NOT IMPLEMENT -- CLAUDE HANDLES THESE

- Real Kalshi order book API integration (needs careful RSA auth + slippage modeling)
- Confidence score recalibration (requires statistical analysis of full trade history)
- RBAC / Admin user management (security-sensitive, easy to get wrong)
- Providers page full rebuild (needs architectural decisions)
- Slippage tracking (depends on order book API being done first)
