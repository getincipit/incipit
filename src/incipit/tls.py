"""The site's own HTTPS certificate, for the address the router opens to the internet.

No certificate authority vouches for a computer in someone's office, so the site makes one itself
on first start and keeps it in the data folder. Browsers warn about it once ("Your connection is
not private"); the team checks the fingerprint the Access page shows and continues, or installs the
certificate to stop the warning. The certificate is kept for years, because a new one means a new
warning for everyone.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

CERT_NAME = "certificate.pem"
KEY_NAME = "private-key.pem"
VALID_DAYS = 5 * 365
# Renewed this long before it ends, at a start, so it never lapses while the team uses it.
RENEW_DAYS = 30


def paths(tls_dir: str | Path) -> tuple[Path, Path]:
    tls_dir = Path(tls_dir)
    return tls_dir / CERT_NAME, tls_dir / KEY_NAME


def _alt_names(names: list[str], addresses: list[str]) -> list[x509.GeneralName]:
    alt: list[x509.GeneralName] = []
    for name in dict.fromkeys(n for n in names if n):
        alt.append(x509.DNSName(name))
    for address in dict.fromkeys(a for a in addresses if a):
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            continue
    return alt


def create(tls_dir: str | Path, *, common_name: str, names: list[str], addresses: list[str]) -> Path:
    """Writes a new key and self-signed certificate for these names and addresses; returns the certificate."""
    cert_path, key_path = paths(tls_dir)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64] or "Incipit"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Incipit (made by this site)")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_alt_names(names, addresses)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    logger.info("made the site's HTTPS certificate for %s", ", ".join(names + addresses))
    return cert_path


def load(tls_dir: str | Path) -> x509.Certificate | None:
    cert_path, key_path = paths(tls_dir)
    if not (cert_path.is_file() and key_path.is_file()):
        return None
    try:
        return x509.load_pem_x509_certificate(cert_path.read_bytes())
    except ValueError:
        return None


def ensure(tls_dir: str | Path, *, common_name: str, names: list[str], addresses: list[str]) -> tuple[Path, Path]:
    """The certificate and key, made on first use and renewed only when they are about to end or unreadable."""
    current = load(tls_dir)
    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=RENEW_DAYS)
    if current is None or current.not_valid_after_utc < soon:
        create(tls_dir, common_name=common_name, names=names, addresses=addresses)
    return paths(tls_dir)


def fingerprint(tls_dir: str | Path) -> str:
    """The SHA-256 fingerprint as browsers show it (AA:BB:…), or empty when there is no certificate."""
    cert_path, _ = paths(tls_dir)
    try:
        der = x509.load_pem_x509_certificate(cert_path.read_bytes()).public_bytes(serialization.Encoding.DER)
    except (OSError, ValueError):
        return ""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# Whether the running site serves HTTPS, which the Access page and the forwarding read.
serving = {"port": 0, "error": ""}


def expires(tls_dir: str | Path) -> str:
    current = load(tls_dir)
    return current.not_valid_after_utc.date().isoformat() if current else ""
