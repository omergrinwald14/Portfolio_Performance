TWR Portfolio App — Step 2
==========================

LOCATION
--------
Place this folder at:
  G:\My Drive\Finance\Investments\twr_app\

FILES
-----
  app.py          — Flask server + upload page
  database.py     — SQLite schema (auto-creates twr.db on first run)
  parsers.py      — CSV parsers for all 4 file types
  requirements.txt
  README.txt


FIRST-TIME SETUP (do once)
--------------------------
1. Open VS Code
2. Open folder: G:\My Drive\Finance\Investments\twr_app\
3. Open terminal (Ctrl + `)
4. Run:
       pip install -r requirements.txt


RUN THE APP
-----------
In the VS Code terminal:

    python app.py

Open browser: http://localhost:5000

You will see 4 upload cards. Upload your files and watch the
"Database Status" row at the bottom update with row counts.


WHAT GETS STORED (twr.db, created automatically)
-------------------------------------------------
  positions    — IBKR daily position values in ILS (RW excluded)
  transactions — all cashflows: IBKR + IBI, net of tax
  tase_prices  — TASE EOD closing prices in ILS (from agorot /100)
  tase_holdings— share count ranges (managed in Step 3)

UPLOADING MULTIPLE FILES
------------------------
Upload as many files as you like — each upload merges into the
database. Duplicate rows are silently skipped.


NEXT STEP
---------
Once you confirm uploads work and row counts look right,
we move to Step 3: TWR calculation engine.
