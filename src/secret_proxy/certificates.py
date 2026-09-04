import datetime
import hashlib
import ipaddress
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class CertificateAuthority:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.ca_key_path = directory / "ca.key"
        self.ca_certificate_path = directory / "ca.crt"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.ca_key_path.exists() and self.ca_certificate_path.exists():
            key = serialization.load_pem_private_key(
                self.ca_key_path.read_bytes(),
                password=None,
            )
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise ValueError("secret proxy CA key has an invalid type")
            self._key = key
            self._certificate = x509.load_pem_x509_certificate(
                self.ca_certificate_path.read_bytes()
            )
            key_public = self._key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            certificate_public = self._certificate.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if key_public != certificate_public:
                raise ValueError("secret proxy CA key does not match its certificate")
        else:
            self._key, self._certificate = self._create()

    def server_context(self, hostname: str) -> ssl.SSLContext:
        certificate_path, key_path = self._certificate_for(hostname)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])
        context.load_cert_chain(certificate_path, key_path)
        return context

    def _create(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "drukbox secret proxy")])
        now = datetime.datetime.now(datetime.UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        self._write_private_key(self.ca_key_path, key)
        self.ca_certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        os.chmod(self.ca_certificate_path, 0o644)
        return key, certificate

    def _certificate_for(self, hostname: str) -> tuple[Path, Path]:
        stem = hashlib.sha256(hostname.encode()).hexdigest()
        certificate_path = self.directory / f"{stem}.crt"
        key_path = self.directory / f"{stem}.key"
        if certificate_path.exists() and key_path.exists():
            try:
                certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
                key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
                certificate_public = certificate.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                key_public = key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                signature_hash = certificate.signature_hash_algorithm
                if not signature_hash:
                    raise ValueError("cached secret proxy certificate has no signature hash")
                self._key.public_key().verify(
                    certificate.signature,
                    certificate.tbs_certificate_bytes,
                    ec.ECDSA(signature_hash),
                )
                refresh_after = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
                if (
                    certificate_public == key_public
                    and certificate.not_valid_after_utc > refresh_after
                ):
                    return certificate_path, key_path
            except (InvalidSignature, TypeError, ValueError):
                pass

        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        try:
            subject_name: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(hostname))
        except ValueError:
            subject_name = x509.DNSName(hostname)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(self._certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([subject_name]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )
        self._write_private_key(key_path, key)
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        os.chmod(certificate_path, 0o644)
        return certificate_path, key_path

    @staticmethod
    def _write_private_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
        encoded = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
