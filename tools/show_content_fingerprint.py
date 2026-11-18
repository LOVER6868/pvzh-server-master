"""Print a UnityFS *internal serialized-content* SHA-256, for future card_data updates.

This is a diagnostic utility, NOT an installation script. It never edits files.
Supports UnityFS uncompressed blocks; supports LZ4 if python-lz4 is installed.
"""
import hashlib
import struct
import sys
from pathlib import Path


def digest_unityfs(path):
    buf = Path(path).read_bytes()
    pos = 0

    def cstr():
        nonlocal pos
        q = buf.index(b'\x00', pos)
        text = buf[pos:q].decode('ascii')
        pos = q + 1
        return text

    def read(fmt):
        nonlocal pos
        values = struct.unpack_from('>' + fmt, buf, pos)
        pos += struct.calcsize('>' + fmt)
        return values[0] if len(values) == 1 else values

    if cstr() != 'UnityFS':
        raise ValueError('not UnityFS')
    version = read('I')
    if not 6 <= version <= 9:
        raise ValueError('unsupported UnityFS version')
    cstr()  # Unity version
    cstr()  # build revision
    size, compressed, plain_size, flags = read('QIII')
    if size != len(buf):
        raise ValueError('bundle file size mismatch')
    header_end = pos
    aligned_header = (pos + 15) & ~15 if flags & 0x200 else pos
    info_offset = len(buf) - compressed if flags & 0x80 else aligned_header
    info = decompress(buf[info_offset: info_offset+compressed], flags & 0x3f, plain_size)
    cursor = 16  # UnityFS metadata content hash
    count = struct.unpack_from('>I', info, cursor)[0]
    cursor += 4
    if not 1 <= count <= 4096:
        raise ValueError('invalid block count')
    blocks = []
    for _ in range(count):
        unpacked_len, packed_len, block_flags = struct.unpack_from('>IIH', info, cursor)
        cursor += 10
        blocks.append((unpacked_len, packed_len, block_flags))
    offset = aligned_header if flags & 0x80 else info_offset + compressed
    h = hashlib.sha256()
    for unpacked_len, packed_len, block_flags in blocks:
        h.update(decompress(buf[offset:offset+packed_len], block_flags & 0x3f, unpacked_len))
        offset += packed_len
    return h.hexdigest()


def decompress(data, mode, size):
    if mode == 0:
        result = data
    elif mode in (2, 3):
        try:
            import lz4.block
        except ImportError:
            raise RuntimeError('this bundle uses LZ4; install python-lz4 to inspect it')
        result = lz4.block.decompress(data, uncompressed_size=size)
    else:
        raise ValueError('unsupported UnityFS compression method')
    if len(result) != size:
        raise ValueError('UnityFS block length mismatch')
    return result


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('Usage: python show_content_fingerprint.py path/to/cards/card_data')
        sys.exit(2)
    print(digest_unityfs(sys.argv[1]))
