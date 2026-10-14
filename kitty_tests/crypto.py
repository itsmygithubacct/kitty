#!/usr/bin/env python
# License: GPLv3 Copyright: 2022, Kovid Goyal <kovid at kovidgoyal.net>


import gc
import json
import os
import subprocess

from . import BaseTest


def is_rlimit_memlock_too_low() -> bool:
    ''' On supported systems, return true if the MEMLOCK limit is too low to
    run the crypto test. '''
    try:
        import resource
    except ModuleNotFoundError:
        return False

    memlock_limit, _ = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    pagesize = resource.getpagesize()
    return memlock_limit <= pagesize


def process_locked_memory_kib():
    '''Return this Linux process's VmLck value when procfs exposes it.'''
    try:
        with open('/proc/self/status', encoding='ascii') as f:
            for line in f:
                if line.startswith('VmLck:'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


class TestCrypto(BaseTest):

    def test_private_key_is_locked_secret(self):
        if is_rlimit_memlock_too_low():
            self.skipTest('RLIMIT_MEMLOCK is too low')
        from kitty.fast_data_types import EllipticCurveKey, Secret
        key = EllipticCurveKey()
        public_before = key.public
        first = key.private
        second = key.private
        self.assertIsInstance(first, Secret)
        self.assertIsInstance(second, Secret)
        self.ae(len(first), 32)
        self.ae(first, second)
        self.ae(key.public, public_before)
        self.assertIsInstance(public_before, bytes)

    def test_secret_storage_releases_locked_pages(self):
        locked_before = process_locked_memory_kib()
        if locked_before is None:
            self.skipTest('process locked-memory accounting is unavailable')
        try:
            import resource
        except ModuleNotFoundError:
            self.skipTest('RLIMIT_MEMLOCK is unavailable')
        page_size = resource.getpagesize()
        if page_size % 1024:
            self.skipTest('page size cannot be compared with VmLck')

        from kitty.fast_data_types import AES256GCMDecrypt, AES256GCMEncrypt, EllipticCurveKey
        alice, bob = EllipticCurveKey(), EllipticCurveKey()
        bob_public = bob.public
        locked_before = process_locked_memory_kib()
        if locked_before is None:
            self.skipTest('process locked-memory accounting is unavailable')
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        if soft_limit == resource.RLIM_INFINITY:
            count = 4
        else:
            remaining_pages = (soft_limit - locked_before * 1024) // page_size
            if remaining_pages < 2:
                self.skipTest('fewer than two pages remain under RLIMIT_MEMLOCK')
            # Keep one page available while derive_secret holds both mappings.
            count = min(4, remaining_pages - 1)

        private_secrets = [alice.private for _ in range(count)]
        page_kib = page_size // 1024
        self.ae(locked_before + count * page_kib, process_locked_memory_kib())
        del private_secrets
        gc.collect()
        self.ae(locked_before, process_locked_memory_kib())

        shared_secrets = [alice.derive_secret(bob_public) for _ in range(count)]
        self.ae(locked_before + count * page_kib, process_locked_memory_kib())
        self.ae(True, all(len(secret) == 32 for secret in shared_secrets))

        aad = b'locked-memory lifetime test aad'
        plaintext = b'public crypto API lifetime regression'
        encryptor = AES256GCMEncrypt(shared_secrets[0])
        encryptor.add_authenticated_but_unencrypted_data(aad)
        ciphertext = encryptor.add_data_to_be_encrypted(plaintext, True)
        decryptor = AES256GCMDecrypt(shared_secrets[0], encryptor.iv, encryptor.tag)
        decryptor.add_data_to_be_authenticated_but_not_decrypted(aad)
        self.ae(plaintext, decryptor.add_data_to_be_decrypted(ciphertext, True))
        del encryptor, decryptor, shared_secrets
        gc.collect()
        self.ae(locked_before, process_locked_memory_kib())

    def test_private_getter_locks_only_its_secret_pages_in_a_fresh_process(self):
        # Locked pages persist for the process, so a getter that also locks some
        # unrelated page (for example its own object) only shows up the first time
        # it runs. Measure it in a process in which nothing else has run yet.
        if process_locked_memory_kib() is None:
            self.skipTest('process locked-memory accounting is unavailable')
        if is_rlimit_memlock_too_low():
            self.skipTest('RLIMIT_MEMLOCK is too low')
        from kitty.constants import kitty_exe
        code = '''\
import gc, json, resource
from kitty.fast_data_types import EllipticCurveKey
def locked():
    with open('/proc/self/status') as f:
        return next(int(line.split()[1]) for line in f if line.startswith('VmLck:'))
key = EllipticCurveKey()
before = locked()
secrets = [key.private for _ in range(3)]
during = locked()
del secrets
gc.collect()
print(json.dumps([before, during, locked(), resource.getpagesize() // 1024]))
'''
        cp = subprocess.run([kitty_exe(), '+runpy', code], capture_output=True, text=True, timeout=60)
        self.ae(cp.returncode, 0, cp.stderr)
        before, during, after, page_kib = json.loads(cp.stdout.strip().splitlines()[-1])
        self.ae(before + 3 * page_kib, during)
        self.ae(before, after)

    def test_elliptic_curve_data_exchange(self):
        if is_rlimit_memlock_too_low():
            self.skipTest('RLIMIT_MEMLOCK is too low')
        from kitty.fast_data_types import AES256GCMDecrypt, AES256GCMEncrypt, CryptoError, EllipticCurveKey
        alice = EllipticCurveKey()
        bob = EllipticCurveKey()
        alice_secret = alice.derive_secret(bob.public)
        bob_secret = bob.derive_secret(alice.public)
        self.assertEqual(len(alice_secret), 32)
        self.assertEqual(len(bob_secret), 32)
        self.assertEqual(alice_secret, bob_secret)

        auth_data = os.urandom(213)
        plaintext = os.urandom(1011)
        e = AES256GCMEncrypt(alice_secret)
        e.add_authenticated_but_unencrypted_data(auth_data)
        ciphertext = e.add_data_to_be_encrypted(plaintext, True)

        d = AES256GCMDecrypt(bob_secret, e.iv, e.tag)
        d.add_data_to_be_authenticated_but_not_decrypted(auth_data)
        q = d.add_data_to_be_decrypted(ciphertext, True)
        self.ae(q, plaintext)

        def corrupt_data(data):
            b = bytearray(data)
            b[0] = (b[0] + 13) % 256
            return bytes(b)

        d = AES256GCMDecrypt(bob_secret, e.iv, corrupt_data(e.tag))
        d.add_data_to_be_authenticated_but_not_decrypted(auth_data)
        self.assertRaises(CryptoError, d.add_data_to_be_decrypted, ciphertext, True)

        d = AES256GCMDecrypt(bob_secret, e.iv, e.tag)
        d.add_data_to_be_authenticated_but_not_decrypted(corrupt_data(auth_data))
        self.assertRaises(CryptoError, d.add_data_to_be_decrypted, ciphertext, True)

        d = AES256GCMDecrypt(bob_secret, e.iv, e.tag)
        d.add_data_to_be_authenticated_but_not_decrypted(auth_data)
        self.assertRaises(CryptoError, d.add_data_to_be_decrypted, corrupt_data(ciphertext), True)
