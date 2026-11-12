"""Locks a site key with a passphrase and, optionally, the secret part of a personal link.
Usage: python3 make_key.py <site key file, base64> <out key.json> [link secret file]
Reads the passphrase from the environment variable DESK_PASS so it never lands in a file.
With a link secret, the passphrase alone cannot open the site: a new device also needs the personal link once."""
import base64, json, os, sys
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
ITER = 600000
key = base64.b64decode(open(sys.argv[1]).read().strip())
link = open(sys.argv[3]).read().strip() if len(sys.argv) > 3 else ""
pw = (os.environ["DESK_PASS"] + ("\n" + link if link else "")).encode()
salt, nonce = os.urandom(16), os.urandom(12)
kek = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw)
b = lambda x: base64.b64encode(x).decode()
json.dump({"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER, "link": bool(link), "salt": b(salt), "nonce": b(nonce), "wrapped": b(AESGCM(kek).encrypt(nonce, key, None))}, open(sys.argv[2], "w"))
print("key locked")
