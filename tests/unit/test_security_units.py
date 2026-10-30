"""Permissions, uploads, archives, SSRF, crypto, BOQ arithmetic."""

import io
import zipfile
from decimal import Decimal

import pytest

from app.auth.context import TenantContext
from app.auth.permissions import Perm, can_assign_role, has_permission
from app.core import security
from app.core.errors import PermissionDenied, ValidationFailed
from app.extraction.parsers import filetype
from app.extraction.parsers.archive import extract_zip
from app.integrations import url_fetch


class TestPermissions:
    def test_viewer_is_read_only(self):
        assert has_permission("viewer", Perm.TENDERS_READ)
        assert not has_permission("viewer", Perm.DOCUMENTS_UPLOAD)

    def test_reviewer_can_approve_but_not_manage_members(self):
        assert has_permission("reviewer", Perm.BIDS_APPROVE) and not has_permission("reviewer", Perm.MEMBERS_MANAGE)

    def test_no_role_escalation(self):
        assert not can_assign_role("admin", "owner")
        assert not can_assign_role("bid_manager", "admin")
        assert can_assign_role("owner", "owner")

    def test_token_scopes_restrict_role(self):
        import uuid

        ctx = TenantContext(uuid.uuid4(), uuid.uuid4(), "owner", scopes=frozenset({"tenders:read"}))
        assert ctx.can(Perm.TENDERS_READ) and not ctx.can(Perm.DOCUMENTS_UPLOAD)
        with pytest.raises(PermissionDenied):
            ctx.require(Perm.ORG_DELETE)


class TestUploads:
    def test_identify_by_content_not_extension(self):
        assert filetype.identify(b"%PDF-1.7 ...", "x.txt").content_type == filetype.PDF
        with pytest.raises(ValidationFailed):
            filetype.identify(b"MZ\x90\x00\x03\x00\x00\x00", "invoice.pdf")

    def test_empty_rejected(self):
        with pytest.raises(ValidationFailed):
            filetype.identify(b"", "a.pdf")

    def test_legacy_office_rejected(self):
        with pytest.raises(ValidationFailed):
            filetype.identify(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 100, "old.doc")

    def test_pdf_javascript_flagged(self):
        assert filetype.basic_threat_scan(b"%PDF-1.4 /JavaScript (app.alert(1))", filetype.PDF)


def _zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


class TestArchives:
    def test_path_traversal_rejected(self):
        with pytest.raises(ValidationFailed):
            extract_zip(_zip({"../../evil.txt": b"x"}))

    def test_absolute_path_rejected(self):
        with pytest.raises(ValidationFailed):
            extract_zip(_zip({"/etc/cron.d/x": b"x"}))

    def test_symlink_rejected(self):
        with pytest.raises(ValidationFailed):
            extract_zip(_zip({"a.txt": b"x"}, symlink="link"))

    def test_zip_bomb_ratio_rejected(self):
        with pytest.raises(ValidationFailed):
            extract_zip(_zip({"big.txt": b"0" * 5_000_000}))

    def test_nested_zip_skipped(self):
        members, warnings = extract_zip(_zip({"a.txt": b"hello", "inner.zip": b"PK.."}))
        assert [m.name for m in members] == ["a.txt"] and warnings


class TestSSRF:
    @pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/admin", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/",
                                     "http://[::1]/", "file:///etc/passwd", "gopher://x", "http://user:pw@example.com/", "http://example.com:8080/x",
                                     "http://0.0.0.0/"])
    def test_blocked(self, url):
        with pytest.raises(ValidationFailed):
            url_fetch.validate_url(url)


class TestCrypto:
    def test_password_hashing(self):
        h = security.hash_password("Correct-Horse-9")
        assert security.verify_password(h, "Correct-Horse-9") and not security.verify_password(h, "wrong")

    def test_password_policy(self):
        assert security.validate_password_strength("short")
        assert not security.validate_password_strength("LongEnough123")

    def test_secret_encryption_roundtrip_and_tamper(self):
        token = security.encrypt_secret("s3cr3t")
        assert security.decrypt_secret(token) == "s3cr3t"
        bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(Exception):
            security.decrypt_secret(bad)

    def test_token_hash_is_keyed(self):
        assert security.hash_token("abc") != __import__("hashlib").sha256(b"abc").hexdigest()


def test_signed_local_download_url():
    import time

    from app.storage.backend import LocalStorage

    exp = int(time.time()) + 60
    sig = LocalStorage.sign("org/x/a.pdf", exp)
    assert LocalStorage.verify("org/x/a.pdf", exp, sig)
    assert not LocalStorage.verify("org/y/a.pdf", exp, sig)
    assert not LocalStorage.verify("org/x/a.pdf", int(time.time()) - 1, LocalStorage.sign("org/x/a.pdf", int(time.time()) - 1))


class TestBoq:
    def test_workbook_analysis(self):
        from pathlib import Path

        from app.services.boq.service import analyse_workbook

        data = (Path(__file__).resolve().parents[2] / "sample_data/tenders/msgic_lt_panels_boq.xlsx").read_bytes()
        res = analyse_workbook(data)
        kinds = {i["type"] for i in res["issues"]}
        assert {"duplicate_line_item", "unit_mismatch", "missing_quantity", "missing_rate"} <= kinds
        assert res["summary"]["sheet_protected"] and res["summary"]["formula_cells"] >= 14
        assert res["summary"]["editable_rate_cells"] == res["summary"]["line_items"]

    def test_totals_decimal(self):
        from types import SimpleNamespace

        from app.services.boq.service import compute_totals

        items = [SimpleNamespace(id=1, is_heading=False, is_subtotal=False, quantity=Decimal("3"), quoted_rate=None),
                 SimpleNamespace(id=2, is_heading=False, is_subtotal=False, quantity=Decimal("0.1"), quoted_rate=None),
                 SimpleNamespace(id=3, is_heading=False, is_subtotal=False, quantity=None, quoted_rate=None)]
        t = compute_totals(items, {"1": Decimal("0.1"), "2": Decimal("0.2")}, Decimal("0"), Decimal("18"))
        assert t["subtotal"] == "0.32" and t["gst"] == "0.06" and t["unpriced_items"] == 1 and not t["complete"]
