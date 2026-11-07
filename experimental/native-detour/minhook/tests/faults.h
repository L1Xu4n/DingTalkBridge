#pragma once
#ifndef MH_STRICT_TEST
#error Fault injection is test-only and must never be compiled into the DLL.
#endif
#include <windows.h>
enum MH_TEST_OPERATION { MH_TEST_NONE, MH_TEST_OPEN, MH_TEST_SUSPEND,
 MH_TEST_GET_CONTEXT, MH_TEST_SET_CONTEXT, MH_TEST_RESUME, MH_TEST_PROTECT,
 MH_TEST_RESTORE, MH_TEST_FLUSH, MH_TEST_SNAPSHOT, MH_TEST_OP_COUNT };
#ifdef __cplusplus
extern "C" {
#endif
void WINAPI MH_TestSetFault(UINT operation, UINT nth);
UINT WINAPI MH_TestCalls(UINT operation);
#ifdef __cplusplus
}
#endif
