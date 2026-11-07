#include "BridgeProtocol.h"
#include <atomic>
namespace {bridge_native::SharedState* g_state=nullptr;std::atomic<bool> g_enabled{false};}
namespace bridge_native {
bool InitializeReadOnlyDetour(const BootstrapRequest& request,SharedState* state) noexcept {
    if(request.build_flavor!=2||!state)return false;g_state=state;g_enabled=true;
    InterlockedIncrement64(&state->callbacks_seen);return true;
}
void DisableReadOnlyCapture(SharedState* owner) noexcept {if(owner==g_state){g_enabled=false;if(g_state)InterlockedExchange(&g_state->Active,0);}}
}
