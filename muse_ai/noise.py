  1 | """Noise_XX_25519_AESGCM_SHA256 initiator matching the Muse web bundle.
  2 | 
  3 | This is intentionally small and explicit instead of relying on a generic Noise library,
  4 | so the state transitions match the captured Muse client.
  5 | """
  6 | from __future__ import annotations
  7 | 
  8 | import hashlib
  9 | import hmac
 10 | import os
 11 | 
 12 | from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
 13 | from cryptography.hazmat.primitives.ciphers.aead import AESGCM
 14 | from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
 15 | 
 16 | PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"
 17 | EMPTY_AD = b""
 18 | 
 19 | 
 20 | def sha256(data: bytes) -> bytes:
 21 |     return hashlib.sha256(data).digest()
 22 | 
 23 | 
 24 | def hkdf_noise(chaining_key: bytes, input_key_material: bytes, outputs: int = 2) -> tuple[bytes, ...]:
 25 |     temp = hmac.new(chaining_key, input_key_material, hashlib.sha256).digest()
 26 |     out1 = hmac.new(temp, b"\x01", hashlib.sha256).digest()
 27 |     out2 = hmac.new(temp, out1 + b"\x02", hashlib.sha256).digest()
 28 |     if outputs == 2:
 29 |         return out1, out2
 30 |     if outputs != 3:
 31 |         raise ValueError("Noise HKDF only supports 2 or 3 outputs here")
 32 |     out3 = hmac.new(temp, out2 + b"\x03", hashlib.sha256).digest()
 33 |     return out1, out2, out3
 34 | 
 35 | 
 36 | def nonce12(counter: int) -> bytes:
 37 |     if counter < 0 or counter >= 1 << 64:
 38 |         raise ValueError("nonce counter out of range")
 39 |     return b"\x00" * 4 + counter.to_bytes(8, "big")
 40 | 
 41 | 
 42 | class CipherState:
 43 |     def __init__(self, key: bytes | None = None) -> None:
 44 |         self._key = bytes(key) if key is not None else None
 45 |         self._nonce = 0
 46 | 
 47 |     @property
 48 |     def has_key(self) -> bool:
 49 |         return self._key is not None
 50 | 
 51 |     @property
 52 |     def nonce(self) -> int:
 53 |         return self._nonce
 54 | 
 55 |     def initialize_key(self, key: bytes) -> None:
 56 |         if len(key) != 32:
 57 |             raise ValueError("AES-GCM key must be 32 bytes")
 58 |         self._key = bytes(key)
 59 |         self._nonce = 0
 60 | 
 61 |     def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
 62 |         if self._key is None:
 63 |             return bytes(plaintext)
 64 |         nonce = nonce12(self._nonce)
 65 |         self._nonce += 1
 66 |         return AESGCM(self._key).encrypt(nonce, plaintext, ad)
 67 | 
 68 |     def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
 69 |         if self._key is None:
 70 |             return bytes(ciphertext)
 71 |         nonce = nonce12(self._nonce)
 72 |         self._nonce += 1
 73 |         return AESGCM(self._key).decrypt(nonce, ciphertext, ad)
 74 | 
 75 | 
 76 | class SymmetricState:
 77 |     def __init__(self) -> None:
 78 |         # Noise InitializeSymmetric(): if the protocol name is shorter than
 79 |         # HASHLEN (SHA-256 => 32), pad it with zero bytes.  If it is longer,
 80 |         # hash it.  Muse's protocol name is shorter than 32 bytes.
 81 |         if len(PROTOCOL) <= 32:
 82 |             initial_hash = PROTOCOL.ljust(32, b"\x00")
 83 |         else:
 84 |             initial_hash = sha256(PROTOCOL)
 85 | 
 86 |         self.ck = initial_hash
 87 |         self.h = initial_hash
 88 |         self.cipher = CipherState()
 89 | 
 90 |         # The Muse bundle performs the same explicit empty mixHash step after
 91 |         # initialization.
 92 |         self.mix_hash(b"")
 93 | 
 94 |     def mix_hash(self, data: bytes) -> None:
 95 |         self.h = sha256(self.h + data)
 96 | 
 97 |     def mix_key(self, ikm: bytes) -> None:
 98 |         self.ck, temp_k = hkdf_noise(self.ck, ikm, 2)
 99 |         self.cipher = CipherState(temp_k)
100 | 
101 |     def encrypt_and_hash(self, plaintext: bytes) -> bytes:
102 |         ciphertext = self.cipher.encrypt_with_ad(self.h, plaintext)
103 |         self.mix_hash(ciphertext)
104 |         return ciphertext
105 | 
106 |     def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
107 |         plaintext = self.cipher.decrypt_with_ad(self.h, ciphertext)
108 |         self.mix_hash(ciphertext)
109 |         return plaintext
110 | 
111 |     def split(self) -> tuple[CipherState, CipherState]:
112 |         k1, k2 = hkdf_noise(self.ck, b"", 2)
113 |         return CipherState(k1), CipherState(k2)
114 | 
115 | 
116 | class NoiseXXInitiator:
117 |     def __init__(self) -> None:
118 |         self.symmetric = SymmetricState()
119 |         self.e_priv: X25519PrivateKey | None = None
120 |         self.s_priv: X25519PrivateKey | None = None
121 |         self.re: bytes | None = None
122 |         self.rs: bytes | None = None
123 |         self.phase = 1
124 | 
125 |     @staticmethod
126 |     def _pub_bytes(priv: X25519PrivateKey) -> bytes:
127 |         return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
128 | 
129 |     @staticmethod
130 |     def _dh(priv: X25519PrivateKey, peer_raw: bytes) -> bytes:
131 |         if len(peer_raw) != 32:
132 |             raise ValueError("X25519 public key must be 32 bytes")
133 |         out = priv.exchange(X25519PublicKey.from_public_bytes(peer_raw))
134 |         if out == b"\x00" * 32:
135 |             raise ValueError("X25519 produced all-zero output")
136 |         return out
137 | 
138 |     def write_message1(self, payload: bytes = b"") -> bytes:
139 |         if self.phase != 1:
140 |             raise RuntimeError("write_message1 in wrong phase")
141 |         self.e_priv = X25519PrivateKey.generate()
142 |         e_pub = self._pub_bytes(self.e_priv)
143 |         self.symmetric.mix_hash(e_pub)
144 |         encrypted_payload = self.symmetric.encrypt_and_hash(payload)
145 |         self.phase = 2
146 |         return e_pub + encrypted_payload
147 | 
148 |     def read_message2(self, msg: bytes) -> bytes:
149 |         if self.phase != 2:
150 |             raise RuntimeError("read_message2 in wrong phase")
151 |         if len(msg) < 96:
152 |             raise ValueError(f"Noise message2 too short: {len(msg)}")
153 |         assert self.e_priv is not None
154 |         offset = 0
155 |         self.re = msg[offset:offset + 32]
156 |         offset += 32
157 |         self.symmetric.mix_hash(self.re)
158 |         self.symmetric.mix_key(self._dh(self.e_priv, self.re))  # ee
159 |         self.rs = self.symmetric.decrypt_and_hash(msg[offset:offset + 48])
160 |         offset += 48
161 |         self.symmetric.mix_key(self._dh(self.e_priv, self.rs))  # es
162 |         payload = self.symmetric.decrypt_and_hash(msg[offset:])
163 |         self.phase = 3
164 |         return payload
165 | 
166 |     def write_message3(self, payload: bytes = b"") -> bytes:
167 |         if self.phase != 3:
168 |             raise RuntimeError("write_message3 in wrong phase")
169 |         if self.re is None:
170 |             raise RuntimeError("missing responder ephemeral key")
171 |         self.s_priv = X25519PrivateKey.generate()
172 |         s_pub = self._pub_bytes(self.s_priv)
173 |         encrypted_static = self.symmetric.encrypt_and_hash(s_pub)
174 |         self.symmetric.mix_key(self._dh(self.s_priv, self.re))  # se
175 |         encrypted_payload = self.symmetric.encrypt_and_hash(payload)
176 |         self.phase = 4
177 |         return encrypted_static + encrypted_payload
178 | 
179 |     def split(self) -> tuple[CipherState, CipherState]:
180 |         if self.phase != 4:
181 |             raise RuntimeError("split in wrong phase")
182 |         self.phase = 5
183 |         return self.symmetric.split()
184 | 
185 |     @property
186 |     def handshake_hash(self) -> bytes:
187 |         return bytes(self.symmetric.h)
188 | 
189 |     @property
190 |     def remote_static_public_key(self) -> bytes | None:
191 |         return self.rs
192 | 
193 | 
194 | def encode_client_nonce_message1(nonce: bytes) -> bytes:
195 |     # protobuf field 1, bytes: tag 0x0a followed by 32-byte length.
196 |     if len(nonce) != 32:
197 |         raise ValueError("client nonce must be exactly 32 bytes")
198 |     return b"\x0a\x20" + nonce
199 | 
200 | 
201 | def make_message1_payload() -> tuple[bytes, bytes]:
202 |     nonce = os.urandom(32)
203 |     return nonce, encode_client_nonce_message1(nonce)
204 | 