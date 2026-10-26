import json
import os
import socket
import struct

# Settings used by both sides.
LENGTH_FORMAT = "!I"  # "!I" means a 4 byte whole number
LENGTH_SIZE = struct.calcsize(LENGTH_FORMAT)
MAX_FRAME_SIZE = 8 * 1024 * 1024  # biggest message we accept (8 MB)
CHUNK_SIZE = 64 * 1024  # we send files in 64 KB pieces
PROTOCOL_VERSION = "2.0"

# Try to load the encryption library.
# If the user did not install it, we set a flag and run without encryption.
try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)
    CRYPTO_AVAILABLE = True
except ImportError:  # library missing, that is OK
    CRYPTO_AVAILABLE = False


# Our own error types, so we can tell our problems apart from normal ones.
class ProtocolError(Exception):
    """The peer sent something that is not a valid frame."""


class IdleTimeout(Exception):
    """The socket was quiet between frames; normal, not an error."""


#  encryption
# This class locks and unlocks messages. Each connection gets its own one.
class SecureChannel:
    NONCE_SIZE = 12

    def __init__(self, shared_secret):
        # Turn the shared secret into a proper 32 byte key, then make the cipher.
        key = HKDF(algorithm=SHA256(), length=32, salt=None,
                   info=b"lan-chat-v2").derive(shared_secret)
        self._cipher = AESGCM(key)

    def encrypt(self, body):
        # A nonce is a random number used once, so the same text looks different
        # every time we send it.
        nonce = os.urandom(self.NONCE_SIZE)
        return nonce + self._cipher.encrypt(nonce, body, None)

    def decrypt(self, blob):
        # The nonce is at the front, the locked message is the rest.
        if len(blob) <= self.NONCE_SIZE:
            raise ProtocolError("encrypted frame is too short")
        try:
            return self._cipher.decrypt(blob[:self.NONCE_SIZE],
                                        blob[self.NONCE_SIZE:], None)
        except Exception:  # wrong key, or somebody changed the bytes
            raise ProtocolError("frame failed authentication (tampering?)")


# genrates a local private key then convert its public key to hex string to travel over the network
def new_key_pair():
    private_key = X25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    return private_key, public_bytes.hex()


# Our private key + their public key = the same secret on both sides.

def shared_channel(private_key, peer_public_hex):
    peer_key = X25519PublicKey.from_public_bytes(bytes.fromhex(peer_public_hex))
    return SecureChannel(private_key.exchange(peer_key))


# framing
# Read exactly the number of bytes we asked for.
# TCP can hand us only part of what we want, so we keep reading in a loop
# until we have all of it. Without this loop messages would get mixed up.
def _receive_exactly(sock, byte_count, idle_ok=False):
    buffer = bytearray()
    while len(buffer) < byte_count:
        try:
            chunk = sock.recv(byte_count - len(buffer))
        except socket.timeout:
            # Nothing arrived. If we were waiting for a new message that is fine.
            if idle_ok and not buffer:
                raise IdleTimeout()
            raise ProtocolError("timed out in the middle of a frame")
        if not chunk:
            # Empty result means the other side closed the connection.
            if idle_ok and not buffer:
                raise ConnectionError("peer closed the connection")
            raise ProtocolError("connection lost mid frame")
        buffer += chunk
    return bytes(buffer)


# Send one message.

def send_frame(sock, send_lock, header, payload=b"", channel=None):
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    body = struct.pack(LENGTH_FORMAT, len(header_bytes)) + header_bytes + payload
    if channel is not None:
        body = channel.encrypt(body)  
    # The lock lets only one thread write at a time. Without it two threads
    # could mix their bytes together and break the message.
    with send_lock:
        sock.sendall(struct.pack(LENGTH_FORMAT, len(body)) + body)


def recv_frame(sock, channel=None):
    """Read one frame and return (header dictionary, payload bytes)."""
    # Step 1: read the 4 bytes that tell us how long the message is.
    raw_length = _receive_exactly(sock, LENGTH_SIZE, idle_ok=True)
    (body_length,) = struct.unpack(LENGTH_FORMAT, raw_length)
    if not 0 < body_length <= MAX_FRAME_SIZE:
        raise ProtocolError("illegal frame size: %d" % body_length)
    # Step 2: read exactly that many bytes, then unlock them if needed.
    body = _receive_exactly(sock, body_length)
    if channel is not None:
        body = channel.decrypt(body)
    if len(body) < LENGTH_SIZE:
        raise ProtocolError("truncated frame body")
    # Step 3: the first 4 bytes say how long the text header is.
    (header_length,) = struct.unpack(LENGTH_FORMAT, body[:LENGTH_SIZE])
    if header_length > len(body) - LENGTH_SIZE:
        raise ProtocolError("header length out of range")
    # Step 4: read the header as JSON and check it really is a dictionary.
    try:
        header = json.loads(body[LENGTH_SIZE:LENGTH_SIZE + header_length])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("bad JSON header: %s" % error)
    if not isinstance(header, dict):
        raise ProtocolError("header is not an object")
    # Whatever is left after the header is the file / audio / video bytes.
    return header, body[LENGTH_SIZE + header_length:]


# helpers
# Turn a raw byte count into human readable string
def human_size(byte_count):
    """1536 -> '1.5 KB'."""
    size = float(byte_count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0


# Find this computer's address on the Wi-Fi, so we can print it for the others.
# We only pretend to connect to Google; no data is actually sent.
def local_address():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()