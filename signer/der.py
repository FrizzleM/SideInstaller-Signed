"""Just enough DER to read what the signer needs from an X.509 certificate:
its subject's common name, its expiry and the two Authority Information Access
URLs (the OCSP responder, and where to fetch the issuing certificate). Reading
these here keeps OpenSSL out of the daily status check entirely."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

OID_COMMON_NAME = bytes.fromhex("550403")               # 2.5.4.3
OID_AIA = bytes.fromhex("2b06010505070101")              # 1.3.6.1.5.5.7.1.1
OID_AIA_OCSP = bytes.fromhex("2b06010505073001")         # 1.3.6.1.5.5.7.48.1
OID_AIA_CA_ISSUERS = bytes.fromhex("2b06010505073002")   # 1.3.6.1.5.5.7.48.2


class DERError(ValueError):
    pass


@dataclass
class Certificate:
    der: bytes
    fingerprint: str          # SHA-1 of the DER, upper-case hex: codesign's identity hash
    common_name: str
    not_after: datetime
    ocsp_url: str | None
    issuer_url: str | None


def _tlv(buf: bytes, pos: int) -> tuple[int, int, int]:
    """(tag, value start, value end) of the element at pos."""
    if pos + 2 > len(buf):
        raise DERError("truncated element")
    tag = buf[pos]
    pos += 1
    if tag & 0x1F == 0x1F:                      # high tag number form
        while buf[pos] & 0x80:
            pos += 1
        pos += 1
    length = buf[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4:
            raise DERError("unsupported length")
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    end = pos + length
    if end > len(buf):
        raise DERError("element overruns its buffer")
    return tag, pos, end


def _children(buf: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out = []
    pos = start
    while pos < end:
        tag, vstart, vend = _tlv(buf, pos)
        out.append((tag, vstart, vend))
        pos = vend
    return out


def _time(buf: bytes, tag: int, start: int, end: int) -> datetime:
    text = buf[start:end].decode("ascii").rstrip("Z")
    if tag == 0x17:                              # UTCTime, YYMMDDHHMM[SS]
        year = int(text[:2])
        text = f"{1900 + year if year >= 50 else 2000 + year}{text[2:]}"
    fmt = "%Y%m%d%H%M%S" if len(text) >= 14 else "%Y%m%d%H%M"
    return datetime.strptime(text[:14], fmt).replace(tzinfo=timezone.utc)


def parse_certificate(der: bytes) -> Certificate:
    try:
        _, cstart, cend = _tlv(der, 0)
        tbs_tag, tstart, tend = _children(der, cstart, cend)[0]
        fields = _children(der, tstart, tend)
        i = 1 if fields[0][0] == 0xA0 else 0     # optional [0] version
        validity, subject = fields[i + 3], fields[i + 4]
        not_after = _time(der, *_children(der, validity[1], validity[2])[1])

        common_name = ""
        for _, rstart, rend in _children(der, subject[1], subject[2]):       # RDN sets
            for _, astart, aend in _children(der, rstart, rend):             # attributes
                oid, value = _children(der, astart, aend)[:2]
                if der[oid[1]:oid[2]] == OID_COMMON_NAME:
                    common_name = der[value[1]:value[2]].decode("utf-8", "replace")

        ocsp_url = issuer_url = None
        for tag, estart, eend in fields[i + 6:]:
            if tag != 0xA3:                      # [3] extensions
                continue
            for _, xstart, xend in _children(der, *_children(der, estart, eend)[0][1:]):
                parts = _children(der, xstart, xend)
                if der[parts[0][1]:parts[0][2]] != OID_AIA:
                    continue
                octets = parts[-1]
                seq = _children(der, octets[1], octets[2])[0]
                for _, dstart, dend in _children(der, seq[1], seq[2]):
                    method, location = _children(der, dstart, dend)[:2]
                    if location[0] != 0x86:      # [6] uniformResourceIdentifier
                        continue
                    url = der[location[1]:location[2]].decode("ascii", "replace")
                    oid = der[method[1]:method[2]]
                    if oid == OID_AIA_OCSP and ocsp_url is None:
                        ocsp_url = url
                    elif oid == OID_AIA_CA_ISSUERS and issuer_url is None:
                        issuer_url = url
    except (IndexError, UnicodeDecodeError, ValueError) as exc:
        raise DERError(f"not a readable certificate: {exc}") from None

    return Certificate(
        der=der,
        fingerprint=hashlib.sha1(der).hexdigest().upper(),
        common_name=common_name,
        not_after=not_after,
        ocsp_url=ocsp_url,
        issuer_url=issuer_url,
    )
