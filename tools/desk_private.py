"""Private inputs of the daily run: files that only this repository's jobs can open.

The jobs derive an X25519 key from the PAPER_KEY secret. Its public half is published as tools/desk_pub.txt, so a
file can be sealed for the jobs from anywhere (no secret needed), and only a job can open it.

  desk_private.bin  the owner's wallet addresses and the hand-entered amounts of the book; replaced from outside
  desk_state.bin    what the daily run carries from one day to the next (the book as last read, its price log, the
                    last brief); written by the job itself

Sealed file: "RD1", the sender's one-time public key (32 bytes), a 12-byte nonce, then AES-256-GCM over gzip JSON.
Usage: python3 tools/desk_private.py pubkey            (in a job: writes tools/desk_pub.txt)
       python3 tools/desk_private.py seal in.json out  (anywhere: seals in.json for the jobs)
"""
import base64, gzip, hashlib, json, os, sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
PUB = os.path.join(HERE, "desk_pub.txt")
RAW = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _priv():
    k = os.environ.get("PAPER_KEY", "")
    if len(k) < 16:
        raise RuntimeError("PAPER_KEY is missing or shorter than 16 characters")
    return X25519PrivateKey.from_private_bytes(hashlib.sha256(("regime-desk private inputs\n" + k).encode()).digest())


def write_pub():
    b = base64.b64encode(_priv().public_key().public_bytes(**RAW)).decode()
    if not os.path.exists(PUB) or open(PUB).read().strip() != b:
        open(PUB, "w").write(b + "\n")
    return b


def _key(eph_pub, to_pub, shared):
    return hashlib.sha256(b"regime-desk seal\n" + eph_pub + to_pub + shared).digest()


def seal(obj, pub_b64=None):
    to = base64.b64decode(pub_b64 or open(PUB).read().strip())
    eph = X25519PrivateKey.generate(); eph_pub = eph.public_key().public_bytes(**RAW)
    nonce = os.urandom(12)
    body = AESGCM(_key(eph_pub, to, eph.exchange(X25519PublicKey.from_public_bytes(to)))).encrypt(
        nonce, gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), 9), None)
    return b"RD1" + eph_pub + nonce + body


def open_file(path):
    b = open(path, "rb").read()
    if b[:3] != b"RD1":
        raise RuntimeError("not a sealed desk file")
    priv = _priv(); eph_pub, nonce = b[3:35], b[35:47]
    key = _key(eph_pub, priv.public_key().public_bytes(**RAW), priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)))
    return json.loads(gzip.decompress(AESGCM(key).decrypt(nonce, b[47:], None)))


if __name__ == "__main__":
    if sys.argv[1:2] == ["pubkey"]:
        print("public key written:", write_pub())
    elif sys.argv[1:2] == ["seal"]:
        open(sys.argv[3], "wb").write(seal(json.load(open(sys.argv[2]))))
        print("sealed", sys.argv[3])
