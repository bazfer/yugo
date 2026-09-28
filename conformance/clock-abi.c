/* Qualified Linux x86_64 glibc timespec ABI. Compile/run on the qualified deployment host via run-qualified-clock.sh; not a CI-runner qualification. */
#include <time.h>
#include <stddef.h>
#include <stdio.h>
#include <gnu/libc-version.h>
_Static_assert(CLOCK_MONOTONIC == 1, "CLOCK_MONOTONIC identifier changed");
_Static_assert(sizeof(time_t) == 8, "64-bit time_t required");
_Static_assert(sizeof(struct timespec) == 16, "timespec size");
_Static_assert(_Alignof(struct timespec) == 8, "timespec alignment");
_Static_assert(offsetof(struct timespec, tv_sec) == 0, "tv_sec offset");
_Static_assert(offsetof(struct timespec, tv_nsec) == 8, "tv_nsec offset");
int main(void) { printf("glibc %s: CLOCK_MONOTONIC=1, timespec=16 bytes/8 aligned\n", gnu_get_libc_version()); return 0; }
