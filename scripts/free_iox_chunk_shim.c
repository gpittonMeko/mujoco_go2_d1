// Minimal runtime shim for older/newer CycloneDDS ABI mismatch.
// Provides free_iox_chunk symbol expected by generated ddscxx IDL code.

#ifdef __cplusplus
extern "C" {
#endif

void free_iox_chunk(void *subscriber, void **chunk) {
    (void)subscriber;
    if (chunk) {
        *chunk = 0;
    }
}

void *iceoryx_header_from_chunk(void *chunk) {
    return chunk;
}

#ifdef __cplusplus
}
#endif
