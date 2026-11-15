#!/usr/bin/env python3
"""Build an encrypted, explicitly identified X6100 V1.1.9 laboratory image.

Only the exact stock XGF and an explicitly compiled test payload are accepted.
The key is read from a separate file; it is never emitted into metadata or
passed to another process. This tool does not communicate with a radio.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

try:
    from . import plan_patch
except ImportError:  # Running as python tools/build_test_image.py.
    import plan_patch


OEM_XGF_SHA256 = '87a3e96ea21abf16a14b0fb771aad19a9e0bf8c267f93ec71a09d38e59c25737'
IDENTITY_ADDRESS = 0x0803d3b0
OEM_IDENTITY = b'10:31:57\0\0\0\0'
TEST_IDENTITY = b'tx1a2-test\0\0'
TEST_MARKER_SYMBOL = 'bb_test_build_marker'
TEST_MARKER = 0x54535432
VECTOR_BYTES = 432  # STM32F427: 107 vectors plus the OEM alignment word.
CALIBRATION_BOUNDARY = 0x080e0000
TRANSFER_BLOCK = 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_key(path):
    """Accept exactly 32 raw bytes, or a 64-digit hex file with whitespace."""
    data = Path(path).read_bytes()
    if len(data) == 32:
        return data
    try:
        key = bytes.fromhex(data.decode('ascii'))
    except (UnicodeError, ValueError):
        raise ValueError('key file must contain 32 raw bytes or 64 hex digits') from None
    if len(key) != 32:
        raise ValueError('key file must contain 32 raw bytes or 64 hex digits')
    return key


def aes_ecb(data, key, *, decrypt=False):
    """Raw AES-256-ECB, as used by XGF; never adds or removes padding."""
    if len(key) != 32:
        raise ValueError('AES-256 requires a 32-byte key')
    if not data or len(data) % 16:
        raise ValueError('XGF data must contain a nonempty whole number of AES blocks')
    try:
        from Crypto.Cipher import AES
    except ImportError:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise ValueError('install pycryptodome or cryptography for XGF packaging') from None
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        operation = cipher.decryptor() if decrypt else cipher.encryptor()
        return operation.update(data) + operation.finalize()
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.decrypt(data) if decrypt else cipher.encrypt(data)


def decrypt_stock(container, key):
    """Reject a wrong key against independently known plaintext, not a roundtrip."""
    if (len(container) != plan_patch.OEM_SIZE or
            sha256(container) != OEM_XGF_SHA256):
        raise ValueError('unsupported encrypted stock image: exact V1.1.9 251127001 required')
    plaintext = aes_ecb(container, key, decrypt=True)
    try:
        plan_patch.validate_oem(plaintext)
    except ValueError:
        raise ValueError('key does not decode the verified stock XGF to the expected OEM image') from None
    if aes_ecb(plaintext, key) != container:
        raise ValueError('stock XGF encryption does not reproduce the original container')
    return plaintext


def load_segments(elf):
    """Return checked physical-address/data pairs and symbol addresses."""
    checked, symbols = plan_patch.read_elf(elf)
    header = struct.unpack_from('<HHIIIIIHHHHHH', elf, 16)
    phoff, phsize, phnum = header[4], header[8], header[9]
    segments = []
    extracted = []
    for index in range(phnum):
        kind, offset, virtual, physical, size, _, flags, _ = struct.unpack_from(
            '<8I', elf, phoff + index * phsize)
        if kind == 1 and size:
            data = elf[offset:offset + size]
            segments.append((physical, data))
            extracted.append({'address': physical, 'virtual_address': virtual,
                              'size': size, 'flags': flags, 'sha256': sha256(data)})
    segments.sort()
    extracted.sort(key=lambda segment: segment['address'])
    if extracted != checked:
        raise ValueError('ELF segment extraction disagrees with validated load map')
    return segments, symbols


def read_payload_address(segments, address, size):
    for start, data in segments:
        if start <= address and address + size <= start + len(data):
            offset = address - start
            return data[offset:offset + size]
    raise ValueError('required test-build marker is outside file-backed payload')


def padded_extent(size, alignment):
    if size <= 0 or alignment <= 0:
        raise ValueError('invalid image size or transfer alignment')
    return plan_patch.BASE + (size + alignment - 1) // alignment * alignment


def verify_extent(size):
    for alignment in (16, 128, 1024):
        if padded_extent(size, alignment) > CALIBRATION_BOUNDARY:
            raise ValueError('image/transport padding reaches the calibration sector')


def build_plaintext(stock, elf):
    """Apply only the five validated hooks, test identity and ELF segments."""
    patch_plan = plan_patch.plan(stock, elf)
    segments, symbols = load_segments(elf)
    marker_address = symbols.get(TEST_MARKER_SYMBOL)
    if marker_address is None or read_payload_address(segments, marker_address, 4) != struct.pack('<I', TEST_MARKER):
        raise ValueError('payload is not an explicit BB_TEST_BUILD laboratory build')
    identity_offset = IDENTITY_ADDRESS - plan_patch.BASE
    if stock[identity_offset:identity_offset + len(OEM_IDENTITY)] != OEM_IDENTITY:
        raise ValueError('OEM build-time identity mismatch')
    if len(OEM_IDENTITY) != len(TEST_IDENTITY):
        raise ValueError('test identity must preserve the OEM string allocation')
    program_end = max(address + len(data) for address, data in segments)
    final_size = padded_extent(program_end - plan_patch.BASE, TRANSFER_BLOCK) - plan_patch.BASE
    verify_extent(final_size)
    plaintext = bytearray(b'\xff' * final_size)
    plaintext[:len(stock)] = stock
    allowed = set(range(identity_offset, identity_offset + len(OEM_IDENTITY)))
    for hook in patch_plan['hooks']:
        offset = int(hook['address'], 16) - plan_patch.BASE
        expected, replacement = bytes.fromhex(hook['expected']), bytes.fromhex(hook['replacement'])
        if offset < VECTOR_BYTES or offset + len(expected) > len(stock):
            raise ValueError('hook escapes the allowed OEM code region')
        if plaintext[offset:offset + len(expected)] != expected or len(replacement) != len(expected):
            raise ValueError('hook bytes changed after validation')
        if any(pos in allowed for pos in range(offset, offset + len(expected))):
            raise ValueError('overlapping hook or identity writes')
        plaintext[offset:offset + len(replacement)] = replacement
        allowed.update(range(offset, offset + len(replacement)))
    plaintext[identity_offset:identity_offset + len(TEST_IDENTITY)] = TEST_IDENTITY
    for address, data in segments:
        offset = address - plan_patch.BASE
        if offset < len(stock) or offset + len(data) > len(plaintext):
            raise ValueError('payload overlaps OEM image or exceeds output extent')
        plaintext[offset:offset + len(data)] = data
    if plaintext[:VECTOR_BYTES] != stock[:VECTOR_BYTES]:
        raise ValueError('vector table changed')
    differences = [index for index, (before, after) in enumerate(zip(stock, plaintext)) if before != after]
    if not set(differences) <= allowed:
        raise ValueError('unexpected OEM modification outside hooks and test identity')
    manifest = {
        'schema_version': 1,
        'status': 'experimental-test-image; hardware validation pending',
        'test_only': True,
        'hardware_validated': False,
        'image_generated': True,
        'target': 'X6100 BASE V1.1.9 (251127001)',
        'display_build_identity': TEST_IDENTITY.rstrip(b'\0').decode('ascii'),
        'activation': {'boot_default': 'bypass', 'register': 36,
                       'mask': '0xffff0000', 'value': '0x54580000',
                       'apply_while': 'RX; TX uses the previously accepted configuration'},
        'load_address': hex(plan_patch.BASE),
        'program_end_exclusive': hex(program_end),
        'image_end_exclusive': hex(plan_patch.BASE + len(plaintext)),
        'calibration_boundary': hex(CALIBRATION_BOUNDARY),
        'plaintext_size': len(plaintext),
        'plaintext_sha256': sha256(plaintext),
        'stock_plaintext_sha256': sha256(stock),
        'stock_xgf_sha256': OEM_XGF_SHA256,
        'payload_elf_sha256': sha256(elf),
        'payload_segments': patch_plan['payload_segments'],
        'hooks': patch_plan['hooks'],
        'identity_patch': {'address': hex(IDENTITY_ADDRESS),
                           'expected': OEM_IDENTITY.hex(), 'replacement': TEST_IDENTITY.hex()},
        'oem_changed_byte_count': len(differences),
        'vector_table_bytes_preserved': VECTOR_BYTES,
        'padding': {'byte': 'ff', 'plaintext_alignment': TRANSFER_BLOCK,
                    'reason': 'avoid updater-added ciphertext padding; cover both XMODEM block sizes'},
        'encryption': 'AES-256-ECB, raw blocks; no header or authentication tag',
        'remaining_target_validation': patch_plan['deployment_blockers'],
    }
    return bytes(plaintext), manifest


def build_container(stock_xgf, elf, key):
    stock = decrypt_stock(stock_xgf, key)
    plaintext, manifest = build_plaintext(stock, elf)
    encrypted = aes_ecb(plaintext, key)
    verify_extent(len(encrypted))
    if aes_ecb(encrypted, key, decrypt=True) != plaintext:
        raise ValueError('complete image encryption roundtrip failed')
    manifest['xgf_size'] = len(encrypted)
    manifest['xgf_sha256'] = sha256(encrypted)
    manifest['checks'] = {
        'exact_stock_ciphertext_and_plaintext': True,
        'stock_ciphertext_reproduced': True,
        'explicit_test_build_marker': True,
        'only_expected_oem_changes': True,
        'complete_encryption_roundtrip': True,
        'aes_and_xmodem_extents_bounded': True,
    }
    return encrypted, manifest


def atomic_write_new(path, data):
    """Publish a complete local file without overwriting an existing artifact."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)  # Atomic, refuses an existing name.
    finally:
        os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-xgf', type=Path, required=True)
    parser.add_argument('--payload-elf', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True,
                        help='external file containing 32 raw key bytes or 64 hex digits')
    parser.add_argument('--output', type=Path, required=True, help='new .xgf output path')
    parser.add_argument('--manifest', type=Path, required=True, help='new JSON build report path')
    args = parser.parse_args()
    try:
        inputs = {p.resolve() for p in (args.stock_xgf, args.payload_elf, args.key_file)}
        outputs = [args.output.resolve(), args.manifest.resolve()]
        if len(set(outputs)) != 2 or any(path in inputs or path.exists() for path in outputs):
            raise ValueError('outputs must be distinct new files and must not replace inputs')
        if args.output.suffix.lower() != '.xgf':
            raise ValueError('encrypted firmware output must use the .xgf extension')
        encrypted, manifest = build_container(args.stock_xgf.read_bytes(),
                                               args.payload_elf.read_bytes(), read_key(args.key_file))
        manifest['xgf_filename'] = args.output.name
        manifest_data = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
        atomic_write_new(args.output, encrypted)
        atomic_write_new(args.manifest, manifest_data)
    except (OSError, ValueError, struct.error) as error:
        parser.exit(1, f'Refused: {error}\n')
    print(f'Built TEST ONLY XGF: {args.output.name} ({len(encrypted)} bytes)')
    print(f'SHA-256: {manifest["xgf_sha256"]}')
    print('Encryption verified; target timing, RF behavior and recovery remain unvalidated.')


if __name__ == '__main__':
    main()
