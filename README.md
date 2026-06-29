# Bovonto Inventory Bot 🥤

Telegram bot for salesman weekly closing stock entry, backed by Google Sheets.

---

## Project Structure

```
bovonto_bot/
├── bot.py              # Main bot logic (ConversationHandler)
├── sheets.py           # Google Sheets read/write helpers
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template  ← copy to .env
├── credentials.json    # Google Service Account key    ← you add this
└── README.md
```

---

## Setup (Step-by-Step)

### 1. Clone / copy these files into a folder

```bash
mkdir bovonto_bot && cd bovonto_bot
# paste the project files here
```

### 2. Create a Python virtual environment

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create your Telegram Bot

1. Open Telegram → search **@BotFather** → `/newbot`
2. Follow prompts, get your **Bot Token**

### 5. Set up Google Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → enable **Google Sheets API** + **Google Drive API**
3. IAM & Admin → Service Accounts → Create → download JSON key
4. Rename the key file to `credentials.json` and place it in the project folder
5. In Google Sheets, **share your spreadsheet** with the service account email
   (it looks like `xxx@xxx.iam.gserviceaccount.com`) — give it **Editor** access

### 6. Configure environment variables

```bash
cp .env.example .env
# Edit .env and fill in your values:
```

```env
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
SPREADSHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
GOOGLE_SERVICE_ACCOUNT_JSON=credentials.json
MASTER_SHEET_NAME=Master
SALESMEN_SHEET_NAME=Salesmen
DISTRIBUTORS_SHEET_NAME=Distributors
PRODUCTS_SHEET_NAME=Products
```

**How to find your Spreadsheet ID:** Open your Google Sheet — the URL is:
`https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`

### 7. Run the bot

```bash
python bot.py
```

You should see: `🚀 Bovonto Inventory Bot started.`

---

## Google Sheets Structure Required

### Salesmen sheet

| A (Salesman Name) |
| ----------------- |
| Ravi Kumar        |
| Suresh M          |

### Distributors sheet

| A (Distributor)      | B (Active ✓) | C (Salesman) |
| -------------------- | ------------ | ------------ |
| Sri Murugan Agencies | ✓            | Ravi Kumar   |
| Lakshmi Traders      | ✓            | Suresh M     |

### Products sheet

| A (Product)   | B (Category) |
| ------------- | ------------ |
| Bovonto 200ml | Returnable   |
| Bovonto 500ml | PET          |

### Master sheet

| A Month | B Week   | C Week Dates | D Distributor        | E Salesman | F Product     | G Category | H Opening Stock | I Receipt | J Closing Stock    |
| ------- | -------- | ------------ | -------------------- | ---------- | ------------- | ---------- | --------------- | --------- | ------------------ |
| June    | 1st Week | 1-7 Jun      | Sri Murugan Agencies | Ravi Kumar | Bovonto 200ml | Returnable | 100             | 50        | _(bot fills this)_ |

---

## Bot Commands

| Command   | Action                                               |
| --------- | ---------------------------------------------------- |
| `/start`  | Begin stock entry flow                               |
| `/skip`   | Skip current product (leave closing stock unchanged) |
| `/cancel` | Cancel and restart                                   |

---

## Deploying to Railway / Render

### Railway

```bash
# Install Railway CLI
npm install -g @railway/cli
railway login
railway init
railway up
```

Set environment variables in Railway dashboard under **Variables**.
Upload `credentials.json` content as a variable `GOOGLE_CREDENTIALS_JSON` and update `sheets.py` to parse it from env if needed.

### Render

1. Push code to GitHub
2. New Web Service → connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Add environment variables in Render dashboard

### Handling credentials.json on cloud

For Railway/Render, instead of a file, load credentials from an env variable:

```python
# In sheets.py, replace Credentials.from_service_account_file(...) with:
import json
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
creds_dict = json.loads(creds_json)
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
```

Then paste the entire `credentials.json` content as the `GOOGLE_CREDENTIALS_JSON` env variable.

---

## Bot Flow Diagram

```
/start
  └─► [Select Salesman]  (inline buttons)
        └─► [Select Week]  (1st/2nd/3rd/4th)
              └─► Distributor 1
                    └─► Product 1: Opening=X, Receipt=Y → Enter Closing Stock
                    └─► Product 2: ...
                    └─► ...
                    └─► [Continue → Distributor 2]
              └─► Distributor 2
                    └─► ...
              └─► [Summary: all entries]
                    └─► [Confirm & Submit] → writes to Master sheet col J ✅
                    └─► [Cancel]
```

---

## Troubleshooting

| Error                                    | Fix                                                                        |
| ---------------------------------------- | -------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN is not set`          | Check your `.env` file exists and is populated                             |
| `gspread.exceptions.SpreadsheetNotFound` | Check `SPREADSHEET_ID` and that the service account has Editor access      |
| `No salesmen found`                      | Check `SALESMEN_SHEET_NAME` matches your actual sheet tab name             |
| `No distributors`                        | Check Distributors sheet column C matches salesman name exactly            |
| `No products in Master`                  | Ensure Master sheet has rows for the Month+Week+Distributor+Salesman combo |
