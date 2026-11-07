#pragma once
#include "../bootstrap/BridgeProtocol.h"
#include <string>

namespace bridge_native::history {
using Active = bool (*)(void*) noexcept;
using Emit = bool (*)(void*, const std::string&) noexcept;

// Read only the initialized current-user UID; never initialize the product singleton.
bool ReadAccountUid(std::uintptr_t base, std::int64_t& uid) noexcept;

// Enumerate existing conversations and page their messages on a private worker thread.
void Run(std::uintptr_t base, const BootstrapRequest& request, SharedState* state,
         void* context, Active active, Emit emit) noexcept;
}
