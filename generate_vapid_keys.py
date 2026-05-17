"""
One-time utility — generate VAPID keys for Web Push.

Run once and add the output to your .env file:
    python generate_vapid_keys.py
"""

import base64
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid

v = Vapid()
v.generate_keys()

priv_pem = v.private_pem().decode().strip()
pub_bytes = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

print("Add these to your .env file:\n")
print(f'VAPID_PRIVATE_KEY="{priv_pem}"')
print(f'VAPID_PUBLIC_KEY="{pub_b64}"')
print(f'VAPID_CONTACT="mailto:admin@palletpro.app"')
print("\nKeep these keys — changing them invalidates all existing push subscriptions.")
