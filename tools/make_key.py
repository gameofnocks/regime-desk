"""Creates the site's key pair and locks the private half for the owner.

Usage: python3 make_key.py <out key.json> <out site_pub.pem> <link secret file>
Reads the passphrase from the environment variable DESK_PASS, so it never lands in a file.

The public key (site_pub.pem) can sit in the open: it can only lock things. Anything that builds the site uses it,
so no build ever needs a secret. The private key is written only in locked form (key.json), and opens with the
owner's passphrase together with the secret part of their personal link.
"""
import base64, json, os, sys
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 600000
out_key, out_pub, link_file = sys.argv[1:4]
link = open(link_file).read().strip()
pw = (os.environ["DESK_PASS"] + "\n" + link).encode()
priv = rsa.generate_private_key(public_exponent=65537, key_size=3072)
der = priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
salt, nonce = os.urandom(16), os.urandom(12)
kek = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw)
b = lambda x: base64.b64encode(x).decode()
json.dump({"v": 2, "kdf": "PBKDF2-SHA256", "iter": ITER, "link": True, "salt": b(salt), "nonce": b(nonce),
           "wrapped": b(AESGCM(kek).encrypt(nonce, der, None))}, open(out_key, "w"))
open(out_pub, "wb").write(priv.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
print("key pair made; private half locked")
