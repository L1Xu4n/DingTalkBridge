#pragma once

#include <cstddef>

namespace dingtalkbridge::native::payload::detail {

// Current process only. Queries only the exact supplied object/string span.
// No handle opening, process enumeration, heap search, or remote-memory API.
// Implemented separately with POD locals to keep SEH out of C++ object scopes.
bool safe_read(const void* source, void* owned_destination, std::size_t bytes) noexcept;

}  // namespace dingtalkbridge::native::payload::detail
