// Fork compatibility wrapper around the upstream pinned-arena implementation.
//
// The upstream file is kept byte-for-byte in pinned_upstream_impl.cu.  This wrapper only
// intercepts the Linux anonymous mmap used for PinnedArena when the shared-lane supervisor
// explicitly provides an exact arena byte count and backing path.  With the environment
// variables absent, every mmap call is passed through unchanged, so upstream single-GPU
// and native --layer-split behavior stay on their normal path.
#ifndef _WIN32
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace strata_fork_shared_arena {

inline bool requested_for(size_t bytes) {
    const char* path = std::getenv("STRATA_SHARED_ARENA_FILE");
    const char* exact = std::getenv("STRATA_SHARED_ARENA_BYTES");
    if (path == nullptr || *path == '\0' || exact == nullptr || *exact == '\0') return false;
    errno = 0;
    char* end = nullptr;
    const unsigned long long want = std::strtoull(exact, &end, 10);
    return errno == 0 && end != exact && *end == '\0' && want != 0 && want == bytes;
}

inline void* mmap_compat(void* addr, size_t bytes, int prot, int flags, int fd, off_t off) {
    if (!requested_for(bytes)) return ::mmap(addr, bytes, prot, flags, fd, off);

    // Upstream reserve() first probes hugetlb.  Fail that probe deliberately so it follows
    // its normal 4 KiB fallback and keeps PageBacking/note semantics correct; the fallback
    // anonymous mapping below is the one replaced by the shared file mapping.
    if ((flags & MAP_HUGETLB) != 0) {
        errno = ENOMEM;
        return MAP_FAILED;
    }
    if ((flags & MAP_ANONYMOUS) == 0 || (flags & MAP_PRIVATE) == 0 || fd != -1 || off != 0)
        return ::mmap(addr, bytes, prot, flags, fd, off);

    const char* path = std::getenv("STRATA_SHARED_ARENA_FILE");
    const int shared_fd = ::open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (shared_fd < 0) return MAP_FAILED;

    struct stat st{};
    if (::fstat(shared_fd, &st) != 0 ||
        ((size_t) st.st_size != bytes && ::ftruncate(shared_fd, (off_t) bytes) != 0)) {
        const int saved = errno;
        ::close(shared_fd);
        errno = saved;
        return MAP_FAILED;
    }

    void* p = ::mmap(addr, bytes, prot, MAP_SHARED, shared_fd, 0);
    const int saved = errno;
#if defined(POSIX_FADV_WILLNEED)
    if (p != MAP_FAILED) (void) ::posix_fadvise(shared_fd, 0, (off_t) bytes, POSIX_FADV_WILLNEED);
#endif
    ::close(shared_fd);
    errno = saved;
    return p;
}

}  // namespace strata_fork_shared_arena

#define mmap strata_fork_shared_arena::mmap_compat
#endif

#include "pinned_upstream_impl.cu"

#ifndef _WIN32
#undef mmap
#endif
