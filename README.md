# Regime Desk

A private dashboard, served as a static site.

The page and its data are encrypted (`app.bin`, `data.bin`, `aixbt.bin`). Each file is locked with the site's public key
(`tools/site_pub.pem`), so anything can build an update without holding a secret. They open only in the owner's browser,
with a personal link and a passphrase. Neither is stored here. `key.json` holds the private key in locked form.

`tools/build_site.py` rebuilds the page and data files each morning. `tools/aixbt_fetch.py` runs in GitHub Actions and
refreshes the AIXBT narratives; `aixbt_status.json` records what those requests returned, without any content.
