# Amul stock alert

Pings your phone (free **ntfy** app) the moment Amul Chocolate Whey Protein is back in stock for your
pincode. It runs on GitHub Actions, so your PC can be off.

- **What it watches:** the product links in `config.json` (Chocolate Whey Protein, 34 g, packs of 60 and 30).
  Add more links to that list to watch other products.
- **How it checks:** stock on shop.amul.com depends on the delivery pincode, so a headless Chrome sets the
  pincode like a person would, then reads the stock count (`available`, `inventory_quantity`) from the
  site's own product data.
- **Alerts:** 🚨 an urgent alert when a product goes from sold out to in stock (tap it to open the product),
  and a quiet one when it sells out. The first run only records the current stock; alerts start from the
  next change.
- **How often:** each run checks every minute for about 5 minutes. A run starts every 5 minutes, triggered by
  GitHub's schedule and by cron-job.org, because GitHub on its own often skips scheduled runs for hours.
- **Secrets:** `NTFY_TOPIC` (the ntfy topic your phone subscribes to) and `PINCODE` (your delivery pincode)
  are repository secrets, so they never sit in this public code.
- **Run it by hand:** Actions tab → "Amul stock check" → "Run workflow".
- **Test locally:** `NTFY_TOPIC=<topic> PINCODE=<pincode> python amul_stock_alert.py --once`
  (needs `pip install playwright` and `python -m playwright install chromium`).
