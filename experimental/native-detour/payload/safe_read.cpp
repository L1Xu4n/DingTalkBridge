#include "safe_read.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <cstdint>
#include <cstring>
#include <limits>

namespace dingtalkbridge::native::payload::detail {
namespace {

bool readable_protection(DWORD protection) noexcept {
    if ((protection & (PAGE_GUARD | PAGE_NOACCESS)) != 0) return false;
    switch (protection & 0xff) {
        case PAGE_READONLY:
        case PAGE_READWRITE:
        case PAGE_WRITECOPY:
        case PAGE_EXECUTE_READ:
        case PAGE_EXECUTE_READWRITE:
        case PAGE_EXECUTE_WRITECOPY:
            return true;
        default:
            return false;
    }
}

int read_exception_filter(DWORD code) noexcept {
    switch (code) {
        case EXCEPTION_ACCESS_VIOLATION:
        case EXCEPTION_IN_PAGE_ERROR:
        case EXCEPTION_GUARD_PAGE:
        case EXCEPTION_DATATYPE_MISALIGNMENT:
            return EXCEPTION_EXECUTE_HANDLER;
        default:
            return EXCEPTION_CONTINUE_SEARCH;
    }
}

}  // namespace

bool safe_read(const void* source, void* owned_destination, std::size_t bytes) noexcept {
    if (bytes == 0) return true;
    // The parser's largest single read is a bounded UTF-8 string.
    if (!source || !owned_destination || bytes > 64 * 1024) return false;
    const auto begin = reinterpret_cast<std::uintptr_t>(source);
    if (begin < 0x10000 || bytes > (std::numeric_limits<std::uintptr_t>::max)() - begin)
        return false;
    const auto end = begin + bytes;
    auto cursor = begin;
    // A byte span can cross allocation/protection boundaries. Check only the
    // regions intersecting the requested span, never adjacent unused memory.
    while (cursor < end) {
        MEMORY_BASIC_INFORMATION region{};
        if (VirtualQuery(reinterpret_cast<const void*>(cursor), &region, sizeof(region))
                != sizeof(region) ||
            region.State != MEM_COMMIT || !readable_protection(region.Protect))
            return false;
        const auto region_begin = reinterpret_cast<std::uintptr_t>(region.BaseAddress);
        if (region_begin > cursor || region.RegionSize == 0 ||
            region.RegionSize > (std::numeric_limits<std::uintptr_t>::max)() - region_begin)
            return false;
        const auto region_end = region_begin + region.RegionSize;
        if (region_end <= cursor) return false;
        cursor = region_end < end ? region_end : end;
    }
    // VirtualQuery alone is not a lifetime guarantee. A mapping can change
    // between the checks above and memcpy, so read faults are contained here.
    __try {
        std::memcpy(owned_destination, source, bytes);
        return true;
    } __except (read_exception_filter(GetExceptionCode())) {
        return false;
    }
}

}  // namespace dingtalkbridge::native::payload::detail
