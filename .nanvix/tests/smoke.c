/*
 * Copyright(c) The Maintainers of Nanvix.
 * Licensed under the MIT License.
 *
 * smoke.c — single-binary round-trip exercise for liblzma on Nanvix.
 *
 * Compresses a fixed 1 KiB pseudo-random buffer with the one-shot
 * easy encoder, decompresses the result with the one-shot stream
 * decoder, and asserts that the recovered bytes are identical to the
 * input. Prints "XZ_SMOKE_OK" on success so callers driving the
 * binary inside `nanvixd.elf` can grep for the sentinel.
 *
 * Exit codes:
 *   0  success
 *   1  any liblzma call returned a non-OK status
 *   2  the round-trip recovered different bytes than were encoded
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <lzma.h>

#define ORIG_LEN 1024u

int main(void)
{
    uint8_t  orig[ORIG_LEN];
    uint8_t  enc[ORIG_LEN * 2u + 128u];
    uint8_t  dec[ORIG_LEN];
    size_t   enc_pos = 0u;
    size_t   in_pos  = 0u;
    size_t   dec_pos = 0u;
    uint64_t memlimit = UINT64_MAX;
    lzma_ret r;
    unsigned i;

    /* Deterministic, non-trivial input so encode + decode actually
       move bytes around (a constant-fill buffer would not exercise
       the bit packers as thoroughly). */
    for (i = 0u; i < ORIG_LEN; ++i)
        orig[i] = (uint8_t)((i * 31u + 7u) ^ (i >> 3));

    /* Preset 0: smallest dictionary (~64 KiB working set) so the
       round-trip fits comfortably inside the 32 MiB default Nanvix
       microvm heap.  liblzma allocates dictionary memory roughly
       proportional to the preset; presets >= 4 exceed the heap. */
    r = lzma_easy_buffer_encode(
        0u, LZMA_CHECK_CRC64, NULL,
        orig, ORIG_LEN,
        enc, &enc_pos, sizeof(enc));
    if (r != LZMA_OK) {
        fprintf(stderr, "lzma_easy_buffer_encode: %d\n", (int)r);
        return 1;
    }

    r = lzma_stream_buffer_decode(
        &memlimit, 0u, NULL,
        enc, &in_pos, enc_pos,
        dec, &dec_pos, sizeof(dec));
    if (r != LZMA_OK) {
        fprintf(stderr, "lzma_stream_buffer_decode: %d\n", (int)r);
        return 1;
    }

    if (dec_pos != ORIG_LEN || memcmp(orig, dec, ORIG_LEN) != 0) {
        fprintf(stderr,
                "round-trip mismatch: dec_pos=%zu (expected %u)\n",
                dec_pos, ORIG_LEN);
        return 2;
    }

    puts("XZ_SMOKE_OK");
    return 0;
}
