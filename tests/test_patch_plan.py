import struct
import unittest
from tools.plan_patch import BASE, HOOKS, OEM_SIZE, thumb_bw, check_hooks, validate_oem, read_elf


def decode_branch(source, encoded):
    first, second = struct.unpack('<HH', encoded)
    sign = first >> 10 & 1
    i1 = ~(second >> 13 ^ sign) & 1
    i2 = ~(second >> 11 ^ sign) & 1
    value = (sign << 24) | (i1 << 23) | (i2 << 22) | ((first & 1023) << 12) | ((second & 2047) << 1)
    if sign:
        value -= 1 << 25
    return source + 4 + value


class PatchPlanTests(unittest.TestCase):
    def test_known_instruction(self):
        self.assertEqual(thumb_bw(0x8000000, 0x8000004), bytes.fromhex('00f000b8'))

    def test_branch_boundaries_and_negative_displacements(self):
        source = 0x802459a
        for delta in [-(1 << 24), -65538, -4, 0, 2, 65538, (1 << 24) - 2]:
            target = source + 4 + delta
            self.assertEqual(decode_branch(source, thumb_bw(source, target)), target)
        for delta in [-(1 << 24) - 2, (1 << 24), 1]:
            with self.assertRaises(ValueError):
                thumb_bw(source, source + 4 + delta)

    def test_guards_every_hook(self):
        image = bytearray(OEM_SIZE)
        for _, address, expected in HOOKS:
            image[address - BASE:address - BASE + 4] = bytes.fromhex(expected)
        check_hooks(image)
        for _, address, _ in HOOKS:
            changed = bytearray(image)
            changed[address - BASE] ^= 1
            with self.assertRaises(ValueError):
                check_hooks(changed)
        # Matching instructions alone never authorize an otherwise unknown image.
        with self.assertRaises(ValueError):
            validate_oem(image)

    def test_rejects_non_elf_and_wrong_machine(self):
        for blob in [b'', b'\0' * 100, b'\x7fELF\x01\x01\x01' + b'\0' * 100]:
            with self.assertRaises(ValueError):
                read_elf(blob)


if __name__ == '__main__':
    unittest.main()
