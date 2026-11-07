/*
 *  MinHook - The Minimalistic API Hooking Library for x64/x86
 *  Copyright (C) 2009-2017 Tsuda Kageyu.
 *  All rights reserved.
 *
 *  Redistribution and use in source and binary forms, with or without
 *  modification, are permitted provided that the following conditions
 *  are met:
 *
 *   1. Redistributions of source code must retain the above copyright
 *      notice, this list of conditions and the following disclaimer.
 *   2. Redistributions in binary form must reproduce the above copyright
 *      notice, this list of conditions and the following disclaimer in the
 *      documentation and/or other materials provided with the distribution.
 *
 *  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 *  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
 *  TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
 *  PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER
 *  OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 *  EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 *  PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
 *  PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
 *  LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
 *  NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
 *  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

// DingTalkBridge dedicated strict fork. One target, one install attempt.
// Successful CreateHook publishes a process-lifetime trampoline. No disable,
// removal, queued mutation or uninitialization is permitted afterwards.
#include <windows.h>
#include <tlhelp32.h>
#include <string.h>
#include "../include/MinHook.h"
#include "buffer.h"
#include "trampoline.h"
#ifdef MH_STRICT_TEST
#include "../tests/faults.h"
#endif

#define SNAPSHOT_BYTES 32 /* validation only; JMP_REL is five bytes */
#define MAX_THREADS 4096
#define THREAD_ACCESS (THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_SET_CONTEXT | THREAD_QUERY_INFORMATION | SYNCHRONIZE)
typedef struct HOOK_ENTRY {
    LPVOID target, detour, trampoline;
    UINT8 snapshot[SNAPSHOT_BYTES], oldIPs[8], newIPs[8];
    UINT nIP;
    BOOL published;
} HOOK_ENTRY;
typedef struct STRICT_THREAD {
    DWORD id;
    HANDLE handle;
    BOOL suspended;
    CONTEXT context;
} STRICT_THREAD;
typedef struct FROZEN_THREADS {
    STRICT_THREAD *items;
    UINT count, capacity;
} FROZEN_THREADS;
static volatile LONG g_lock;
static HANDLE g_heap;
static DWORD g_pageSize;
static HOOK_ENTRY g_hook;
static BOOL g_attempted;
static FROZEN_THREADS g_pending;
static MH_STRICT_DIAGNOSTICS g_diag;

#ifdef MH_STRICT_TEST
static volatile LONG g_testFault, g_testNth, g_testCalls[MH_TEST_OP_COUNT];
void WINAPI MH_TestSetFault(UINT operation, UINT nth) {
    UINT i;
    InterlockedExchange(&g_testFault, (LONG)operation);
    InterlockedExchange(&g_testNth, (LONG)nth);
    for(i=0;i<MH_TEST_OP_COUNT;++i) InterlockedExchange(&g_testCalls[i],0);
}
UINT WINAPI MH_TestCalls(UINT op) { return op<MH_TEST_OP_COUNT ? (UINT)g_testCalls[op] : 0; }
static BOOL TestFail(UINT op) {
    LONG n=InterlockedIncrement(&g_testCalls[op]);
    if((UINT)g_testFault==op && (!g_testNth || n==g_testNth)) {
        SetLastError(ERROR_ACCESS_DENIED); return TRUE;
    }
    return FALSE;
}
#define TEST_FAIL(op) TestFail(op)
#else
#define TEST_FAIL(op) FALSE
#endif
static MH_STATUS Failure(MH_STATUS status) {
    if(!g_diag.win32_error) g_diag.win32_error=GetLastError();
    return status;
}
static HANDLE Snapshot(void) {
    if(TEST_FAIL(MH_TEST_SNAPSHOT)) return INVALID_HANDLE_VALUE;
    return CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD,0);
}
static HANDLE Open(DWORD id) {
    if(TEST_FAIL(MH_TEST_OPEN)) return NULL;
    return OpenThread(THREAD_ACCESS,FALSE,id);
}
static DWORD Suspend(HANDLE handle) {
    if(TEST_FAIL(MH_TEST_SUSPEND)) return (DWORD)-1;
    return SuspendThread(handle);
}
static BOOL GetContext(HANDLE handle, CONTEXT *context) {
    if(TEST_FAIL(MH_TEST_GET_CONTEXT)) return FALSE;
    return GetThreadContext(handle,context);
}
static BOOL SetContext(HANDLE handle, const CONTEXT *context) {
    if(TEST_FAIL(MH_TEST_SET_CONTEXT)) return FALSE;
    return SetThreadContext(handle,context);
}
static DWORD Resume(HANDLE handle) {
    if(TEST_FAIL(MH_TEST_RESUME)) return (DWORD)-1;
    return ResumeThread(handle);
}
static BOOL Protect(LPVOID p,SIZE_T n,DWORD protection,PDWORD old,BOOL restore) {
#ifdef MH_STRICT_TEST
    if(TEST_FAIL(restore ? MH_TEST_RESTORE : MH_TEST_PROTECT)) return FALSE;
#else
    (void)restore;
#endif
    return VirtualProtect(p,n,protection,old);
}
static BOOL Flush(LPCVOID p,SIZE_T n) {
    if(TEST_FAIL(MH_TEST_FLUSH)) return FALSE;
    return FlushInstructionCache(GetCurrentProcess(),p,n);
}
static void Lock(void) {
    UINT spins=0;
    while(InterlockedCompareExchange(&g_lock,TRUE,FALSE)) Sleep(spins++<32 ? 0:1);
}
static void Unlock(void) { InterlockedExchange(&g_lock,FALSE); }

// Validate protection without touching a guard page. The caller must pin the
// target module. The five-byte patch must fit one page, so oldProtect restores
// exactly that page's prior flags rather than copying flags across a boundary.
static BOOL ReadableEntry(LPVOID target) {
    MEMORY_BASIC_INFORMATION region;
    DWORD protection;
    ULONG_PTR start=(ULONG_PTR)target,end;
    if(!start || start > (ULONG_PTR)-1-SNAPSHOT_BYTES) return FALSE;
    end=start+SNAPSHOT_BYTES;
    if(VirtualQuery(target,&region,sizeof(region))!=sizeof(region) || region.State!=MEM_COMMIT ||
       (region.Protect & (PAGE_GUARD|PAGE_NOACCESS)) ||
       (ULONG_PTR)region.BaseAddress>start || region.RegionSize<end-(ULONG_PTR)region.BaseAddress) return FALSE;
    protection=region.Protect & 0xff;
    if(protection!=PAGE_EXECUTE_READ && protection!=PAGE_EXECUTE_READWRITE && protection!=PAGE_EXECUTE_WRITECOPY) return FALSE;
    return g_pageSize>=sizeof(JMP_REL) && start%g_pageSize<=g_pageSize-sizeof(JMP_REL);
}
static int ReadFault(DWORD code) {
    return code==EXCEPTION_ACCESS_VIOLATION || code==EXCEPTION_IN_PAGE_ERROR ||
        code==EXCEPTION_GUARD_PAGE || code==EXCEPTION_DATATYPE_MISALIGNMENT ?
        EXCEPTION_EXECUTE_HANDLER : EXCEPTION_CONTINUE_SEARCH;
}
static BOOL EntrySnapshot(LPVOID target, UINT8 *snapshot, BOOL compare) {
    if(!ReadableEntry(target)) return FALSE;
    __try {
        if(compare) return memcmp(target,snapshot,SNAPSHOT_BYTES)==0;
        memcpy(snapshot,target,SNAPSHOT_BYTES); return TRUE;
    } __except(ReadFault(GetExceptionCode())) { return FALSE; }
}

static MH_STATUS Enumerate(FROZEN_THREADS *threads) {
    HANDLE snapshot=Snapshot();
    THREADENTRY32 entry;
    BOOL next;
    MH_STATUS status=MH_OK;
    if(snapshot==INVALID_HANDLE_VALUE) return Failure(MH_ERROR_THREAD_ENUM);
    entry.dwSize=sizeof(entry);
    next=Thread32First(snapshot,&entry);
    if(!next) status=Failure(MH_ERROR_THREAD_ENUM);
    while(next && status==MH_OK) {
        if(entry.dwSize < FIELD_OFFSET(THREADENTRY32,th32OwnerProcessID)+sizeof(DWORD)) {
            SetLastError(ERROR_BAD_LENGTH); status=Failure(MH_ERROR_THREAD_ENUM); break;
        }
        if(entry.th32OwnerProcessID==GetCurrentProcessId() && entry.th32ThreadID!=GetCurrentThreadId()) {
            if(threads->count==MAX_THREADS) {
                SetLastError(ERROR_BUFFER_OVERFLOW); status=Failure(MH_ERROR_THREAD_ENUM); break;
            }
            if(threads->count==threads->capacity) {
                UINT capacity=threads->capacity ? threads->capacity*2 : 128;
                STRICT_THREAD *items;
                if(capacity>MAX_THREADS) capacity=MAX_THREADS;
                items=threads->items ? (STRICT_THREAD*)HeapReAlloc(g_heap,HEAP_ZERO_MEMORY,threads->items,capacity*sizeof(*items)) :
                    (STRICT_THREAD*)HeapAlloc(g_heap,HEAP_ZERO_MEMORY,capacity*sizeof(*items));
                if(!items) { status=MH_ERROR_MEMORY_ALLOC; break; }
                threads->items=items; threads->capacity=capacity;
            }
            threads->items[threads->count++].id=entry.th32ThreadID;
        }
        entry.dwSize=sizeof(entry);
        next=Thread32Next(snapshot,&entry);
        if(!next && GetLastError()!=ERROR_NO_MORE_FILES) status=Failure(MH_ERROR_THREAD_ENUM);
    }
    CloseHandle(snapshot);
    return status;
}

// A second snapshot detects observed churn; it cannot prohibit creation of a
// thread after this check. This residual is documented, not hidden by MH_OK.
static MH_STATUS VerifyThreadSet(FROZEN_THREADS *threads) {
    HANDLE snapshot=Snapshot();
    THREADENTRY32 entry;
    BOOL next;
    UINT seen=0,i;
    MH_STATUS status=MH_OK;
    if(snapshot==INVALID_HANDLE_VALUE) return Failure(MH_ERROR_THREAD_ENUM);
    entry.dwSize=sizeof(entry);
    next=Thread32First(snapshot,&entry);
    if(!next) status=Failure(MH_ERROR_THREAD_ENUM);
    while(next && status==MH_OK) {
        if(entry.dwSize < FIELD_OFFSET(THREADENTRY32,th32OwnerProcessID)+sizeof(DWORD)) {
            SetLastError(ERROR_BAD_LENGTH); status=Failure(MH_ERROR_THREAD_ENUM); break;
        }
        if(entry.th32OwnerProcessID==GetCurrentProcessId() && entry.th32ThreadID!=GetCurrentThreadId()) {
            for(i=0;i<threads->count;++i) if(threads->items[i].id==entry.th32ThreadID) break;
            if(i==threads->count) { status=MH_ERROR_THREAD_SET_CHANGED; break; }
            ++seen;
        }
        entry.dwSize=sizeof(entry);
        next=Thread32Next(snapshot,&entry);
        if(!next && GetLastError()!=ERROR_NO_MORE_FILES) status=Failure(MH_ERROR_THREAD_ENUM);
    }
    CloseHandle(snapshot);
    if(status==MH_OK && seen!=threads->count) status=MH_ERROR_THREAD_SET_CHANGED;
    for(i=0;status==MH_OK && i<threads->count;++i)
        if(WaitForSingleObject(threads->items[i].handle,0)!=WAIT_TIMEOUT) status=MH_ERROR_THREAD_SET_CHANGED;
    if(status==MH_OK) g_diag.thread_set_checked=TRUE;
    return status;
}
static MH_STATUS MoveIP(STRICT_THREAD *thread) {
    CONTEXT context=thread->context;
#if defined(_M_X64) || defined(__x86_64__)
    DWORD64 *ip=&context.Rip;
#else
    DWORD *ip=&context.Eip;
#endif
    UINT i;
    for(i=0;i<g_hook.nIP;++i) {
        if(*ip==(DWORD_PTR)g_hook.target+g_hook.oldIPs[i]) {
            *ip=(DWORD_PTR)g_hook.trampoline+g_hook.newIPs[i];
            if(!SetContext(thread->handle,&context)) return Failure(MH_ERROR_THREAD_CONTEXT);
            ++g_diag.contexts_moved;
            return MH_OK;
        }
    }
    if(*ip>=(DWORD_PTR)g_hook.target && *ip<(DWORD_PTR)g_hook.target+sizeof(JMP_REL)) {
        SetLastError(ERROR_INVALID_ADDRESS); return Failure(MH_ERROR_THREAD_CONTEXT);
    }
    return MH_OK;
}
static MH_STATUS Freeze(FROZEN_THREADS *threads) {
    UINT i;
    MH_STATUS status;
    ZeroMemory(threads,sizeof(*threads));
    status=Enumerate(threads);
    g_diag.threads_seen=threads->count;
    if(status!=MH_OK) return status;
    // Complete allocation and handle acquisition before any host thread stops.
    for(i=0;i<threads->count;++i) {
        STRICT_THREAD *entry=&threads->items[i];
        entry->handle=Open(entry->id);
        if(!entry->handle || GetProcessIdOfThread(entry->handle)!=GetCurrentProcessId()) return Failure(MH_ERROR_THREAD_OPEN);
    }
    for(i=0;i<threads->count;++i) {
        STRICT_THREAD *entry=&threads->items[i];
        if(Suspend(entry->handle)==(DWORD)-1) return Failure(MH_ERROR_THREAD_SUSPEND);
        entry->suspended=TRUE; ++g_diag.threads_suspended;
    }
    for(i=0;i<threads->count;++i) {
        STRICT_THREAD *entry=&threads->items[i];
        entry->context.ContextFlags=CONTEXT_CONTROL;
        if(!GetContext(entry->handle,&entry->context)) return Failure(MH_ERROR_THREAD_CONTEXT);
    }
    status=VerifyThreadSet(threads);
    if(status!=MH_OK) return status;
    // Reject an intervening entry change before moving any thread into the
    // trampoline built from the previously validated original instructions.
    if(!EntrySnapshot(g_hook.target,g_hook.snapshot,TRUE)) return MH_ERROR_TARGET_CHANGED;
    for(i=0;i<threads->count;++i) {
        status=MoveIP(&threads->items[i]);
        if(status!=MH_OK) return status;
    }
    return MH_OK;
}

// Always run, including partial Freeze failure. Never reopen by TID. Three
// bounded retries release only our added suspend count; persistent failures
// keep their handles/array alive for explicit recovery, not silent abandonment.
static MH_STATUS Unfreeze(FROZEN_THREADS *threads) {
    MH_STATUS status=MH_OK;
    UINT i,pending=0;
    for(i=0;i<threads->count;++i) {
        STRICT_THREAD *entry=&threads->items[i];
        if(entry->suspended) {
            UINT attempt;
            for(attempt=0;attempt<3 && entry->suspended;++attempt) {
                DWORD result=Resume(entry->handle);
                if(result!=(DWORD)-1) {
                    entry->suspended=FALSE;
                    if(result==0) { SetLastError(ERROR_INVALID_STATE); status=Failure(MH_ERROR_THREAD_RESUME); }
                    else ++g_diag.threads_resumed;
                } else {
                    status=Failure(MH_ERROR_THREAD_RESUME);
                    if(WaitForSingleObject(entry->handle,0)==WAIT_OBJECT_0) entry->suspended=FALSE;
                }
            }
        }
        if(entry->suspended) ++pending;
        else if(entry->handle) { CloseHandle(entry->handle); entry->handle=NULL; }
    }
    g_diag.pending_resume=pending;
    if(!pending) {
        if(threads->items) HeapFree(g_heap,0,threads->items);
        ZeroMemory(threads,sizeof(*threads));
    }
    return status;
}

static MH_STATUS Patch(void) {
    JMP_REL patch;
    DWORD oldProtect,ignored;
    BOOL restored,flushed;
    if(!EntrySnapshot(g_hook.target,g_hook.snapshot,TRUE)) return MH_ERROR_TARGET_CHANGED;
    patch.opcode=0xE9;
    patch.operand=(UINT32)((LPBYTE)g_hook.detour-((LPBYTE)g_hook.target+sizeof(patch)));
    if(!Protect(g_hook.target,sizeof(patch),PAGE_EXECUTE_READWRITE,&oldProtect,FALSE)) return Failure(MH_ERROR_MEMORY_PROTECT);
    memcpy(g_hook.target,&patch,sizeof(patch));
    g_diag.patch_written=TRUE;
    restored=Protect(g_hook.target,sizeof(patch),oldProtect,&ignored,TRUE);
    if(!restored) { g_diag.protection_restore_failed=TRUE; Failure(MH_ERROR_PROTECTION_RESTORE); }
    flushed=Flush(g_hook.target,sizeof(patch));
    if(!flushed) { g_diag.cache_flush_failed=TRUE; Failure(MH_ERROR_CACHE_FLUSH); }
    if(!restored) return MH_ERROR_PROTECTION_RESTORE;
    return flushed ? MH_OK : MH_ERROR_CACHE_FLUSH;
}

MH_STATUS WINAPI MH_Initialize(void) {
    MH_STATUS status=MH_OK;
    Lock();
    if(g_heap) status=MH_ERROR_ALREADY_INITIALIZED;
    else {
        SYSTEM_INFO system;
        GetSystemInfo(&system); g_pageSize=system.dwPageSize;
        g_heap=HeapCreate(0,0,0);
        if(!g_heap) status=MH_ERROR_MEMORY_ALLOC;
        else InitializeBuffer();
    }
    Unlock(); return status;
}
MH_STATUS WINAPI MH_Uninitialize(void) {
    MH_STATUS status;
    Lock();
    if(!g_heap) status=MH_ERROR_NOT_INITIALIZED;
    else if(g_hook.published || g_pending.items) status=MH_ERROR_PROCESS_LIFETIME;
    else { UninitializeBuffer(); HeapDestroy(g_heap); g_heap=NULL; status=MH_OK; }
    Unlock(); return status;
}
MH_STATUS WINAPI MH_CreateHook(LPVOID target,LPVOID detour,LPVOID *original) {
    MH_STATUS status=MH_OK;
    LPVOID buffer=NULL;
    TRAMPOLINE ct;
    UINT8 snapshot[SNAPSHOT_BYTES];
    Lock();
    if(!g_heap) status=MH_ERROR_NOT_INITIALIZED;
    else if(g_hook.published || g_attempted) status=MH_ERROR_PROCESS_LIFETIME;
    else if(!IsExecutableAddress(target) || !IsExecutableAddress(detour)) status=MH_ERROR_NOT_EXECUTABLE;
    else if(!EntrySnapshot(target,snapshot,FALSE)) status=MH_ERROR_TARGET_CHANGED;
    else if(!(buffer=AllocateBuffer(target))) status=MH_ERROR_MEMORY_ALLOC;
    else {
        ZeroMemory(&ct,sizeof(ct));
        ct.pTarget=target; ct.pDetour=detour; ct.pTrampoline=buffer;
        if(!CreateTrampolineFunction(&ct) || ct.patchAbove) status=MH_ERROR_UNSUPPORTED_FUNCTION;
        else if(!EntrySnapshot(target,snapshot,TRUE)) status=MH_ERROR_TARGET_CHANGED;
        else if(!Flush(buffer,MEMORY_SLOT_SIZE)) status=MH_ERROR_CACHE_FLUSH;
        else {
            g_hook.target=target;
#if defined(_M_X64) || defined(__x86_64__)
            g_hook.detour=ct.pRelay;
#else
            g_hook.detour=detour;
#endif
            g_hook.trampoline=buffer; g_hook.nIP=ct.nIP;
            memcpy(g_hook.snapshot,snapshot,sizeof(snapshot));
            memcpy(g_hook.oldIPs,ct.oldIPs,sizeof(ct.oldIPs));
            memcpy(g_hook.newIPs,ct.newIPs,sizeof(ct.newIPs));
            g_hook.published=TRUE;
            if(original) *original=buffer;
        }
        // Only an unpublished buffer may be returned to the allocator.
        if(status!=MH_OK) FreeBuffer(buffer);
    }
    Unlock(); return status;
}
MH_STATUS WINAPI MH_EnableHook(LPVOID target) {
    MH_STATUS status,cleanup;
    FROZEN_THREADS threads;
    Lock();
    if(!g_heap) status=MH_ERROR_NOT_INITIALIZED;
    else if(!target || g_attempted || g_pending.items) status=MH_ERROR_PROCESS_LIFETIME;
    else if(!g_hook.published || target!=g_hook.target) status=MH_ERROR_NOT_CREATED;
    else {
        g_attempted=TRUE;
        ZeroMemory(&g_diag,sizeof(g_diag)); g_diag.install_attempted=TRUE;
        status=Freeze(&threads);
        if(status==MH_OK) status=Patch();
        cleanup=Unfreeze(&threads); // mandatory even when Freeze failed.
        if(threads.items) g_pending=threads;
        if(cleanup!=MH_OK) status=cleanup;
        g_diag.status=status;
    }
    Unlock(); return status;
}
MH_STATUS WINAPI MH_RecoverSuspendedThreads(void) {
    MH_STATUS status;
    Lock();
    status=g_heap ? Unfreeze(&g_pending) : MH_ERROR_NOT_INITIALIZED;
    if(status!=MH_OK) g_diag.status=status;
    Unlock(); return status;
}
MH_STATUS WINAPI MH_GetStrictDiagnostics(MH_STRICT_DIAGNOSTICS *out,UINT size) {
    if(!out || size!=sizeof(*out)) return MH_UNKNOWN;
    Lock(); *out=g_diag; out->size=sizeof(*out); Unlock(); return MH_OK;
}
MH_STATUS WINAPI MH_RemoveHook(LPVOID target) { (void)target; return MH_ERROR_PROCESS_LIFETIME; }
MH_STATUS WINAPI MH_DisableHook(LPVOID target) { (void)target; return MH_ERROR_PROCESS_LIFETIME; }
MH_STATUS WINAPI MH_QueueEnableHook(LPVOID target) { (void)target; return MH_ERROR_PROCESS_LIFETIME; }
MH_STATUS WINAPI MH_QueueDisableHook(LPVOID target) { (void)target; return MH_ERROR_PROCESS_LIFETIME; }
MH_STATUS WINAPI MH_ApplyQueued(void) { return MH_ERROR_PROCESS_LIFETIME; }
MH_STATUS WINAPI MH_CreateHookApiEx(LPCWSTR module,LPCSTR name,LPVOID detour,LPVOID *original,LPVOID *target) {
    (void)module; (void)name; (void)detour; (void)original; (void)target;
    return MH_ERROR_PROCESS_LIFETIME; // dedicated fixed-RVA scope only.
}
MH_STATUS WINAPI MH_CreateHookApi(LPCWSTR module,LPCSTR name,LPVOID detour,LPVOID *original) {
    return MH_CreateHookApiEx(module,name,detour,original,NULL);
}
const char *WINAPI MH_StatusToString(MH_STATUS status) {
#define S(x) case x: return #x;
    switch(status) {
        S(MH_UNKNOWN) S(MH_OK) S(MH_ERROR_ALREADY_INITIALIZED) S(MH_ERROR_NOT_INITIALIZED)
        S(MH_ERROR_ALREADY_CREATED) S(MH_ERROR_NOT_CREATED) S(MH_ERROR_ENABLED) S(MH_ERROR_DISABLED)
        S(MH_ERROR_NOT_EXECUTABLE) S(MH_ERROR_UNSUPPORTED_FUNCTION) S(MH_ERROR_MEMORY_ALLOC)
        S(MH_ERROR_MEMORY_PROTECT) S(MH_ERROR_MODULE_NOT_FOUND) S(MH_ERROR_FUNCTION_NOT_FOUND)
        S(MH_ERROR_THREAD_ENUM) S(MH_ERROR_THREAD_OPEN) S(MH_ERROR_THREAD_SUSPEND)
        S(MH_ERROR_THREAD_CONTEXT) S(MH_ERROR_THREAD_RESUME) S(MH_ERROR_THREAD_SET_CHANGED)
        S(MH_ERROR_TARGET_CHANGED) S(MH_ERROR_PROTECTION_RESTORE) S(MH_ERROR_CACHE_FLUSH)
        S(MH_ERROR_PROCESS_LIFETIME)
        default: return "MH_UNKNOWN";
    }
#undef S
}
