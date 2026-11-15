"""Packaging tests use synthetic firmware and published AES known-answer data.

No vendor firmware or device encryption key is included in these fixtures.
"""
import contextlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from tools import build_test_image as builder
from tools import plan_patch


def synthetic_elf():
    """A tiny independently laid out ELF32 with flash and CCM PT_LOAD entries."""
    blob = bytearray(0x4a0)
    blob[:16] = b'\x7fELF\x01\x01\x01' + bytes(9)
    struct.pack_into('<HHIIIIIHHHHHH', blob, 16, 2, 40, 1,
                     plan_patch.FLASH_START + 1, 52, 0x400, 0x05000400,
                     52, 32, 2, 40, 4, 0)
    struct.pack_into('<8I', blob, 52, 1, 0x100, plan_patch.FLASH_START,
                     plan_patch.FLASH_START, 0x100, 0x100, 5, 4)
    struct.pack_into('<8I', blob, 84, 1, 0, plan_patch.CCM_START,
                     plan_patch.FLASH_START + 0x100, 0, 2048, 6, 4)
    blob[0x100:0x200] = bytes(range(256))
    struct.pack_into('<I', blob, 0x180, builder.TEST_MARKER)
    names = bytearray(b'\0')
    symbols = bytearray(16)
    for index, (name, _, _) in enumerate(plan_patch.HOOKS):
        name_offset = len(names)
        names.extend(name.encode() + b'\0')
        symbols.extend(struct.pack('<IIIBBH', name_offset,
                                   plan_patch.FLASH_START + index * 4 + 1,
                                   4, 0x12, 0, 1))
    name_offset = len(names)
    names.extend(builder.TEST_MARKER_SYMBOL.encode() + b'\0')
    symbols.extend(struct.pack('<IIIBBH', name_offset,
                               plan_patch.FLASH_START + 0x80, 4, 0x11, 0, 1))
    blob[0x220:0x220 + len(names)] = names
    blob[0x300:0x300 + len(symbols)] = symbols
    struct.pack_into('<10I', blob, 0x428, 0, 1, 6, plan_patch.FLASH_START,
                     0x100, 0x100, 0, 0, 4, 0)
    struct.pack_into('<10I', blob, 0x450, 0, 2, 0, 0,
                     0x300, len(symbols), 3, 1, 4, 16)
    struct.pack_into('<10I', blob, 0x478, 0, 3, 0, 0,
                     0x220, len(names), 0, 0, 1, 0)
    return bytes(blob)


def synthetic_stock():
    image = bytearray(b'\xa5' * plan_patch.OEM_SIZE)
    image[:builder.VECTOR_BYTES] = bytes(builder.VECTOR_BYTES)
    struct.pack_into('<II', image, 0, 0x20030000, 0x08034a19)
    for _, address, expected in plan_patch.HOOKS:
        offset = address - plan_patch.BASE
        image[offset:offset + 4] = bytes.fromhex(expected)
    offset = builder.IDENTITY_ADDRESS - plan_patch.BASE
    image[offset:offset + len(builder.OEM_IDENTITY)] = builder.OEM_IDENTITY
    return bytes(image)


class XgfPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = bytes(range(32))  # Synthetic test key, unrelated to any radio.
        cls.stock = synthetic_stock()
        cls.elf = synthetic_elf()
        cls.container = builder.aes_ecb(cls.stock, cls.key)

    def fixture_hashes(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(plan_patch, 'OEM_SHA256', builder.sha256(self.stock)))
        stack.enter_context(mock.patch.object(builder, 'OEM_XGF_SHA256', builder.sha256(self.container)))
        return stack

    def test_aes256_ecb_nist_known_answer_and_aligned_length(self):
        # NIST SP 800-38A, F.1.5: AES-256 ECB, first block.
        # https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-38a.pdf
        key = bytes.fromhex('603deb1015ca71be2b73aef0857d7781'
                            '1f352c073b6108d72d9810a30914dff4')
        plaintext = bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')
        expected = bytes.fromhex('f3eed1bdb5d2a03c064b5a7e3db181f8')
        self.assertEqual(builder.aes_ecb(plaintext, key), expected)
        self.assertEqual(builder.aes_ecb(expected, key, decrypt=True), plaintext)
        for malformed in (b'', b'x', bytes(15), bytes(17)):
            with self.assertRaises(ValueError):
                builder.aes_ecb(malformed, key)
        with self.assertRaises(ValueError):
            builder.aes_ecb(plaintext, key[:-1])

    def test_complete_image_has_only_allowed_oem_changes_and_preserves_vectors(self):
        with self.fixture_hashes():
            encrypted, report = builder.build_container(self.container, self.elf, self.key)
        plaintext = builder.aes_ecb(encrypted, self.key, decrypt=True)
        self.assertEqual(plaintext[:builder.VECTOR_BYTES], self.stock[:builder.VECTOR_BYTES])
        changed = {n for n, (a, b) in enumerate(zip(self.stock, plaintext)) if a != b}
        allowed = set()
        for name, address, _ in plan_patch.HOOKS:
            index = address - plan_patch.BASE
            allowed.update(range(index, index + 4))
            self.assertNotEqual(plaintext[index:index + 4], self.stock[index:index + 4], name)
        identity = builder.IDENTITY_ADDRESS - plan_patch.BASE
        allowed.update(range(identity, identity + len(builder.TEST_IDENTITY)))
        self.assertLessEqual(changed, allowed)
        self.assertEqual(plaintext[identity:identity + 12], builder.TEST_IDENTITY)
        start = plan_patch.FLASH_START - plan_patch.BASE
        self.assertEqual(plaintext[start:start + 256], self.elf[0x100:0x200])
        self.assertEqual(plaintext[len(self.stock):start], b'\xff' * (start - len(self.stock)))
        self.assertEqual(plaintext[start + 256:], b'\xff' * (len(plaintext) - start - 256))
        self.assertEqual(len(encrypted) % 1024, 0)
        self.assertEqual(report['xgf_sha256'], builder.sha256(encrypted))
        self.assertEqual(report['plaintext_sha256'], builder.sha256(plaintext))
        self.assertTrue(all(report['checks'].values()))
        self.assertNotIn(self.key.hex(), json.dumps(report))

    def test_wrong_key_and_corrupted_container_are_rejected(self):
        with self.fixture_hashes():
            with self.assertRaisesRegex(ValueError, 'key does not decode'):
                builder.decrypt_stock(self.container, bytes([42]) * 32)
            for corrupt in (self.container[:-1], self.container + bytes(16),
                            bytes([self.container[0] ^ 1]) + self.container[1:]):
                with self.assertRaisesRegex(ValueError, 'unsupported encrypted stock'):
                    builder.decrypt_stock(corrupt, self.key)

    def test_rejects_plaintext_corruption_before_patching(self):
        with self.fixture_hashes():
            damaged = bytearray(self.stock)
            damaged[0] ^= 1
            with self.assertRaisesRegex(ValueError, 'unsupported OEM image'):
                builder.build_plaintext(damaged, self.elf)

    def test_refuses_analysis_payload_or_wrong_test_marker(self):
        for marker in (0, builder.TEST_MARKER ^ 1):
            elf = bytearray(self.elf)
            struct.pack_into('<I', elf, 0x180, marker)
            with self.fixture_hashes(), self.assertRaisesRegex(ValueError, 'BB_TEST_BUILD'):
                builder.build_plaintext(self.stock, elf)
        elf = self.elf.replace(builder.TEST_MARKER_SYMBOL.encode(), b'x' * len(builder.TEST_MARKER_SYMBOL))
        with self.fixture_hashes(), self.assertRaisesRegex(ValueError, 'BB_TEST_BUILD'):
            builder.build_plaintext(self.stock, elf)

    def test_rejects_payload_overlaps_bounds_and_truncation(self):
        damaged = []
        for offset, value in ((52 + 12, plan_patch.BASE),  # physical load overlaps OEM
                              (52 + 20, 0x30000),       # memory extends beyond flash budget
                              (84 + 8, 0x20000000),     # private state escapes CCM
                              (52 + 4, len(self.elf))): # truncated payload data
            elf = bytearray(self.elf)
            struct.pack_into('<I', elf, offset, value)
            damaged.append(elf)
        damaged.extend((self.elf[:40], self.elf[:-1]))
        for elf in damaged:
            with self.fixture_hashes(), self.assertRaises((ValueError, struct.error)):
                builder.build_plaintext(self.stock, elf)

    def test_rejects_flash_relocation_and_nonexecutable_hook_segments(self):
        for offset, value, error in (
                (52 + 8, plan_patch.FLASH_START + 4, 'execution address'),
                (52 + 24, 4, 'outside executable')):
            elf = bytearray(self.elf)
            struct.pack_into('<I', elf, offset, value)
            with self.fixture_hashes(), self.assertRaisesRegex(ValueError, error):
                builder.build_plaintext(self.stock, elf)

    def test_rejects_malformed_elf_headers_symbol_and_string_tables(self):
        cases = [
            (20, '<I', 2),           # Wrong e_version.
            (40, '<H', 51),          # Wrong e_ehsize.
            (0x450 + 20, '<I', 111), # Incomplete final symbol entry.
            (0x478 + 4, '<I', 1),    # Symbol table links to PROGBITS, not STRTAB.
            (0x478 + 16, '<I', len(self.elf) - 2), # Truncated string table.
            (0x478 + 20, '<I', 0),   # Empty string table.
            (0x220, '<B', 1),        # First string-table byte must be NUL.
        ]
        string_size = struct.unpack_from('<I', self.elf, 0x478 + 20)[0]
        cases.append((0x220 + string_size - 1, '<B', 1))  # Missing final terminator.
        for offset, fmt, value in cases:
            elf = bytearray(self.elf)
            struct.pack_into(fmt, elf, offset, value)
            with self.subTest(offset=offset, value=value), self.fixture_hashes():
                with self.assertRaises(ValueError):
                    builder.build_plaintext(self.stock, elf)

    def test_identity_mismatch_is_refused_even_with_matching_oem_fixture_hash(self):
        image = bytearray(self.stock)
        image[builder.IDENTITY_ADDRESS - plan_patch.BASE] ^= 1
        with mock.patch.object(plan_patch, 'OEM_SHA256', builder.sha256(image)):
            with self.assertRaisesRegex(ValueError, 'build-time identity mismatch'):
                builder.build_plaintext(image, self.elf)

    def test_transport_padding_cannot_cross_calibration_boundary(self):
        room = builder.CALIBRATION_BOUNDARY - plan_patch.BASE
        builder.verify_extent(room)
        builder.verify_extent(room - 1)
        with self.assertRaises(ValueError):
            builder.verify_extent(room + 1)
        with mock.patch.object(builder, 'CALIBRATION_BOUNDARY', plan_patch.BASE + 513):
            # The raw/AES image fits but the final 1024-byte XMODEM block does not.
            with self.assertRaises(ValueError):
                builder.verify_extent(512)

    def test_key_files_and_atomic_output_never_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / 'external-key'
            key_file.write_bytes(self.key)
            self.assertEqual(builder.read_key(key_file), self.key)
            key_file.write_text(self.key.hex() + '\n')
            self.assertEqual(builder.read_key(key_file), self.key)
            key_file.write_text('invalid-key')
            with self.assertRaises(ValueError):
                builder.read_key(key_file)
            output = Path(directory) / 'test.xgf'
            builder.atomic_write_new(output, b'complete')
            with self.assertRaises(FileExistsError):
                builder.atomic_write_new(output, b'overwrite')
            self.assertEqual(output.read_bytes(), b'complete')
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                             ['external-key', 'test.xgf'])


if __name__ == '__main__':
    unittest.main()
