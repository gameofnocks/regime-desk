# Regime Desk

A private dashboard, served as a static site.

The page and its data are encrypted (`app.bin`, `data.bin`). They open only with the owner's personal link and passphrase,
neither of which is stored here. `key.json` holds the site key in locked form.

`tools/` has the script that rebuilds the two encrypted files each morning.
