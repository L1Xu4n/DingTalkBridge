// The only live authorization channel is created inside the visible button Click handler.
// No grant file is read or written. This file does not capture or OCR anything.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

internal sealed class BackendBridge : IDisposable {
    private const string Schema="dingtalkbridge.roi-once-backend-binding.v1";
    private readonly string configPath,configSha,pythonPath,pythonSha,adapterPath,adapterSha,applicationConfig,applicationConfigSha,expectedDigest,uiPath,uiSha;
    private Process process;
    private OwnedBackendProcess ownedProcess;
    private bool attempted;
    private volatile bool cancelled,disposed;
    private IntPtr eventHandle,childEventHandle,nativeEventHandle,childNativeEventHandle,revocationEventHandle,childRevocationEventHandle;
    internal bool Ready { get; private set; }
    internal string BackendDigest { get; private set; }
    internal string SceneBinding { get; private set; }
    internal string Reason { get; private set; }
    internal bool Attempted { get { return attempted; } }
    internal void ProtectAuditOutput(string output) {foreach(string input in new string[]{configPath,pythonPath,adapterPath,applicationConfig,uiPath})Descriptor.Need(!String.Equals(output,input,StringComparison.OrdinalIgnoreCase),"output_must_not_replace_input");}
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern IntPtr CreateEvent(IntPtr security,bool manualReset,bool initialState,string name);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool DuplicateHandle(IntPtr sourceProcess,IntPtr source,IntPtr targetProcess,out IntPtr target,uint access,bool inherit,uint options);
    [DllImport("kernel32.dll")] private static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] private static extern bool SetEvent(IntPtr handle);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);

    internal BackendBridge(string path) {
        configPath=Descriptor.FullPath(path); configSha=Descriptor.Hash(configPath);
        Dictionary<string,object> c=Descriptor.Read(configPath,65536); Descriptor.Need(Descriptor.Str(c,"schema")==Schema,"unsupported_backend_config");
        pythonPath=Descriptor.FullPath(Descriptor.Str(c,"python_exe")); pythonSha=Descriptor.Sha(c,"python_sha256");
        adapterPath=Descriptor.FullPath(Descriptor.Str(c,"adapter_script")); adapterSha=Descriptor.Sha(c,"adapter_sha256");
        applicationConfig=Descriptor.FullPath(Descriptor.Str(c,"config_path")); applicationConfigSha=Descriptor.Sha(c,"config_sha256");
        expectedDigest=Descriptor.Sha(c,"expected_backend_digest"); uiPath=Descriptor.FullPath(Descriptor.Str(c,"ui_exe")); uiSha=Descriptor.Sha(c,"ui_sha256");
        VerifyFiles(); Reason="not_checked";
    }
    private void VerifyFiles() {
        Descriptor.Need(String.Equals(Path.GetFullPath(Assembly.GetExecutingAssembly().Location),uiPath,StringComparison.OrdinalIgnoreCase),"unexpected_ui_executable_path");
        foreach(string[] pair in new string[][]{new string[]{configPath,configSha},new string[]{pythonPath,pythonSha},new string[]{adapterPath,adapterSha},new string[]{applicationConfig,applicationConfigSha},new string[]{uiPath,uiSha}}) Descriptor.Need(Descriptor.Hash(pair[0])==pair[1],"backend_or_ui_file_hash_mismatch");
    }
    private static string Arguments(string[] args) {
        List<string> quoted=new List<string>();
        foreach(string arg in args) { Descriptor.Need(arg.IndexOf('"')<0 && arg.IndexOf('\r')<0 && arg.IndexOf('\n')<0 && !arg.EndsWith("\\",StringComparison.Ordinal),"invalid_backend_argument"); quoted.Add("\""+arg+"\""); }
        return String.Join(" ",quoted.ToArray());
    }
    private OwnedBackendProcess Start(string[] args) {
        VerifyFiles(); ProcessStartInfo info=new ProcessStartInfo(); info.FileName=pythonPath; info.Arguments=Arguments(args);
        info.UseShellExecute=false; info.CreateNoWindow=true; info.RedirectStandardInput=true; info.RedirectStandardOutput=true; info.RedirectStandardError=true;
        info.WorkingDirectory=Path.GetDirectoryName(adapterPath); info.StandardOutputEncoding=new UTF8Encoding(false,true); info.StandardErrorEncoding=new UTF8Encoding(false,true);
        Process p=new Process(); p.StartInfo=info; OwnedBackendProcess owned=null;
        try {
            Descriptor.Need(p.Start(),"backend_start_failed"); owned=new OwnedBackendProcess(p);
            if(cancelled || disposed) { owned.Dispose(); throw new CalibrationFailure("backend_start_cancelled"); }
            p.StandardInput.WriteLine("{\"type\":\"ui_job_ready\"}"); p.StandardInput.Flush();
            return owned;
        } catch { if(owned!=null)owned.Dispose();else{try { if(!p.HasExited)p.Kill(); }catch{} p.Dispose();}throw; }
    }
    internal static void CleanupLateStart(Task<OwnedBackendProcess> start) { start.ContinueWith(delegate(Task<OwnedBackendProcess> task){if(task.Status==TaskStatus.RanToCompletion)task.Result.Dispose();else if(task.IsFaulted){Exception ignored=task.Exception;}},TaskScheduler.Default); }
    internal void Probe(Descriptor descriptor) {
        Descriptor.Need(!attempted && !disposed,"backend_session_unavailable");
        Task<OwnedBackendProcess> starting=Task.Run(delegate{return Start(new string[]{"-I",adapterPath,"readiness","--ui-managed","--config",applicationConfig,"--profile",descriptor.FilePath,"--ui-binding",configPath});});
        if(!starting.Wait(15000)) { cancelled=true; CleanupLateStart(starting); throw new CalibrationFailure("backend_readiness_start_timeout"); }
        using(OwnedBackendProcess owned=starting.GetAwaiter().GetResult()) {
            Process probe=owned.Process;
            // Readiness is metadata only. Its stdout is bounded by the reviewed adapter contract.
            Task<string> stdout=probe.StandardOutput.ReadToEndAsync(), stderr=probe.StandardError.ReadToEndAsync();
            if(!probe.WaitForExit(15000))throw new CalibrationFailure("backend_readiness_timeout");
            Descriptor.Need(Task.WaitAll(new Task[]{stdout,stderr},3000),"backend_readiness_output_timeout");
            string text=stdout.GetAwaiter().GetResult(); string error=stderr.GetAwaiter().GetResult();
            Descriptor.Need(probe.ExitCode==0 && String.IsNullOrWhiteSpace(error) && text.Length<=65536,"backend_readiness_failed");
            ApplyReadiness(Parse(text),expectedDigest,descriptor.FileSha256);
        }
    }
    private static Dictionary<string,object> Parse(string json) { Descriptor.Need(json!=null && json.Length>0 && json.Length<=65536,"invalid_backend_response"); return Descriptor.Obj(new JavaScriptSerializer().DeserializeObject(json)); }
    internal static bool ValidateReadiness(Dictionary<string,object> r,string expected,string profileHash,out string digest,out string scene,out string reason) {
        Descriptor.Need(Descriptor.Str(r,"schema")=="dingtalkbridge.wgc-once-readiness.v1","unsupported_readiness_schema");
        Descriptor.Bool(r,"capture_started",false); Descriptor.Bool(r,"pixels_read",false); Descriptor.Bool(r,"authorization_granted",false);
        digest=Descriptor.Sha(r,"backend_digest"); Descriptor.Need(digest==expected,"backend_digest_mismatch");
        Descriptor.Need(Descriptor.Sha(r,"profile_sha256")==profileHash,"backend_profile_hash_mismatch");
        bool ready=Descriptor.Boolean(r,"ready"); scene=ready?Descriptor.Sha(r,"scene_binding_sha256"):null;
        if(ready) { Descriptor.Bool(r,"technical_complete",true); Descriptor.Bool(r,"known_strong_input_geometry_complete",true); }
        object[] reasons=Descriptor.Array(r,"reasons"); Descriptor.Need(reasons.Length<=64,"invalid_readiness_reasons");
        foreach(object item in reasons) { string code=item as string; Descriptor.Need(!String.IsNullOrEmpty(code) && code.Length<=120,"invalid_readiness_reason"); foreach(char c in code)Descriptor.Need((c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='_',"invalid_readiness_reason"); }
        reason=ready?"ready_for_manual_click_only":(reasons.Length>0?(string)reasons[0]:"backend_not_ready");
        Descriptor.Need(!ready || reasons.Length==0,"inconsistent_readiness"); return ready;
    }
    private void ApplyReadiness(Dictionary<string,object> r,string expected,string profileHash) {
        string digest,scene,reason; Ready=ValidateReadiness(r,expected,profileHash,out digest,out scene,out reason); BackendDigest=digest; SceneBinding=scene; Reason=reason;
    }
    private static async Task<string> ReadLine(Process p,int milliseconds) {
        Task<string> line=p.StandardOutput.ReadLineAsync(); Task completed=await Task.WhenAny(line,Task.Delay(milliseconds));
        Descriptor.Need(completed==line,"backend_live_channel_timeout"); string result=await line; Descriptor.Need(result!=null && result.Length<=65536,"backend_live_channel_closed"); return result;
    }
    internal async Task<Dictionary<string,object>> RunFromClick(Descriptor d,Func<Dictionary<string,object>> currentBinding) {
        Descriptor.Need(Ready && !attempted && !cancelled && !disposed,"backend_not_ready_for_attempt"); attempted=true;
        DateTimeOffset clicked=DateTimeOffset.UtcNow;
        try {
            // Both handles are unnamed. The child receives SYNCHRONIZE only; only this UI can signal.
            eventHandle=CreateEvent(IntPtr.Zero,false,false,null); nativeEventHandle=CreateEvent(IntPtr.Zero,false,false,null); revocationEventHandle=CreateEvent(IntPtr.Zero,true,false,null);
            Descriptor.Need(eventHandle!=IntPtr.Zero && nativeEventHandle!=IntPtr.Zero && revocationEventHandle!=IntPtr.Zero,"click_event_creation_failed");
            Descriptor.Need(DuplicateHandle(GetCurrentProcess(),eventHandle,GetCurrentProcess(),out childEventHandle,0x00100000,true,0),"click_event_duplicate_failed");
            Descriptor.Need(DuplicateHandle(GetCurrentProcess(),nativeEventHandle,GetCurrentProcess(),out childNativeEventHandle,0x00100000,true,0) && DuplicateHandle(GetCurrentProcess(),revocationEventHandle,GetCurrentProcess(),out childRevocationEventHandle,0x00100000,true,0),"click_event_duplicate_failed");
            string[] arguments=new string[]{"-I",adapterPath,"run-once","--ui-managed","--config",applicationConfig,"--profile",d.FilePath,"--ui-binding",configPath,"--click-event-handle",childEventHandle.ToInt64().ToString(System.Globalization.CultureInfo.InvariantCulture),"--native-event-handle",childNativeEventHandle.ToInt64().ToString(System.Globalization.CultureInfo.InvariantCulture),"--revocation-event-handle",childRevocationEventHandle.ToInt64().ToString(System.Globalization.CultureInfo.InvariantCulture)};
            Task<OwnedBackendProcess> starting=Task.Run(delegate{return Start(arguments);});
            if(await Task.WhenAny(starting,Task.Delay(10000))!=starting) { cancelled=true; CleanupLateStart(starting); throw new CalibrationFailure("backend_start_timeout"); }
            ownedProcess=await starting; process=ownedProcess.Process; Descriptor.Need(!cancelled && !disposed,"backend_start_cancelled");
            // Closing the inheritable parent copy prevents inheritance by any later process.
            CloseHandle(childEventHandle); childEventHandle=IntPtr.Zero;
            CloseHandle(childNativeEventHandle); childNativeEventHandle=IntPtr.Zero; CloseHandle(childRevocationEventHandle); childRevocationEventHandle=IntPtr.Zero;
            Task<string> error=process.StandardError.ReadToEndAsync();
            Dictionary<string,object> challenge=Parse(await ReadLine(process,10000));
            Descriptor.Need(!cancelled && !disposed && DateTimeOffset.UtcNow-clicked<TimeSpan.FromSeconds(15),"click_expired_or_cancelled");
            Descriptor.Need(Descriptor.Str(challenge,"schema")=="dingtalkbridge.roi-once-challenge.v1" && Descriptor.Str(challenge,"type")=="challenge","invalid_backend_challenge"); Descriptor.Bool(challenge,"ready",true);
            string nonceChallenge=Descriptor.Sha(challenge,"challenge");
            DateTimeOffset challengeExpiry=Descriptor.Time(challenge,"expires_utc"); Descriptor.Need(challengeExpiry>DateTimeOffset.UtcNow && challengeExpiry<=DateTimeOffset.UtcNow.AddSeconds(16),"challenge_expired_or_invalid");
            Descriptor.Need(Descriptor.Sha(challenge,"backend_digest")==expectedDigest && Descriptor.Sha(challenge,"scene_binding_sha256")==SceneBinding && Descriptor.Sha(challenge,"profile_sha256")==d.FileSha256,"challenge_binding_changed");
            VerifyFiles(); d.VerifyLive(true);
            // Called after button mouse action and challenge delivery, on the UI thread.
            Dictionary<string,object> response=currentBinding();
            Descriptor.Need(!cancelled && DateTimeOffset.UtcNow-clicked<TimeSpan.FromSeconds(15),"click_expired_or_cancelled");
            byte[] nonce=new byte[32]; using(RandomNumberGenerator rng=RandomNumberGenerator.Create())rng.GetBytes(nonce);
            DateTimeOffset created=DateTimeOffset.UtcNow;
            response["schema"]="dingtalkbridge.roi-once-live-grant.v1"; response["type"]="confirm_once"; response["issuer"]="calibration_ui";
            response["action"]="read_once_and_local_mcp"; response["nonce"]=BitConverter.ToString(nonce).Replace("-","").ToLowerInvariant();
            DateTimeOffset expires=created.AddSeconds(15)<challengeExpiry?created.AddSeconds(15):challengeExpiry;
            response["challenge"]=nonceChallenge; response["created_utc"]=created.ToString("o"); response["expires_utc"]=expires.ToString("o");
            response["created_filetime"]=created.UtcDateTime.ToFileTimeUtc().ToString(System.Globalization.CultureInfo.InvariantCulture); response["expires_filetime"]=expires.UtcDateTime.ToFileTimeUtc().ToString(System.Globalization.CultureInfo.InvariantCulture);
            response["clicked_utc"]=clicked.ToString("o"); response["profile_sha256"]=d.FileSha256; response["backend_digest"]=expectedDigest;
            response["scene_binding_sha256"]=SceneBinding; response["ui_pid"]=Process.GetCurrentProcess().Id; response["ui_path"]=uiPath; response["ui_sha256"]=uiSha;
            response["ui_process_start_utc"]=Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("o");
            response["issuer_pid"]=Process.GetCurrentProcess().Id; response["issuer_process_start_filetime"]=Process.GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc().ToString(System.Globalization.CultureInfo.InvariantCulture);
            response["authorization_granted"]=true;
            Descriptor.Need(SetEvent(eventHandle) && SetEvent(nativeEventHandle),"click_event_signal_failed");
            Task sending=Send(process,ProtocolJson(response)); Descriptor.Need(await Task.WhenAny(sending,Task.Delay(3000))==sending,"backend_live_send_timeout"); await sending;
            Dictionary<string,object> result=Parse(await ReadLine(process,45000));
            Descriptor.Need(Descriptor.Str(result,"type")=="result","invalid_backend_result");
            Descriptor.Need(!cancelled,"attempt_cancelled");
            // The adapter returns diagnostics/counts only, never message text or image data.
            Dictionary<string,object> summary=Descriptor.Child(result,"summary"); string status=Descriptor.Str(summary,"status"); Descriptor.Need(status=="completed" || status=="rejected" || status=="cancelled" || status=="failed","invalid_backend_result_status");
            if(!process.WaitForExit(3000))throw new CalibrationFailure("backend_exit_timeout");
            Descriptor.Need(await Task.WhenAny(error,Task.Delay(3000))==error,"backend_diagnostic_timeout"); Descriptor.Need(String.IsNullOrWhiteSpace(await error),"backend_diagnostic_error"); return result;
        } catch { Cancel("attempt_failed_or_cancelled"); throw; }
        finally { if(ownedProcess!=null)ownedProcess.Dispose(); CloseEventHandles(); }
    }
    private static async Task Send(Process p,string line) { await p.StandardInput.WriteLineAsync(line); await p.StandardInput.FlushAsync(); }
    private static string ProtocolJson(object value) {string json=new JavaScriptSerializer().Serialize(value);StringBuilder ascii=new StringBuilder();foreach(char c in json){if(c>127)ascii.Append("\\u"+((int)c).ToString("x4",System.Globalization.CultureInfo.InvariantCulture));else ascii.Append(c);}return ascii.ToString();}
    internal void Cancel(string reason) {
        cancelled=true; Ready=false;
        if(revocationEventHandle!=IntPtr.Zero)SetEvent(revocationEventHandle);
        // Cancellation must not depend on a readable/writable pipe. Signal, then kill the owned job.
        if(ownedProcess!=null)ownedProcess.Dispose();
    }
    private void CloseEventHandles() {
        foreach(IntPtr h in new IntPtr[]{childEventHandle,eventHandle,childNativeEventHandle,nativeEventHandle,childRevocationEventHandle,revocationEventHandle})if(h!=IntPtr.Zero)CloseHandle(h);
        childEventHandle=eventHandle=childNativeEventHandle=nativeEventHandle=childRevocationEventHandle=revocationEventHandle=IntPtr.Zero;
    }
    public void Dispose() { if(disposed)return; disposed=true; Cancel("ui_session_closed"); CloseEventHandles(); process=null; }
}

// A non-inheritable Windows job owns only the subprocess launched by this UI.
// Assignment happens before either click event can be signalled.
internal sealed class OwnedBackendProcess : IDisposable {
    [StructLayout(LayoutKind.Sequential)] private struct BASIC { public long ProcessTime,JobTime; public uint Flags; public UIntPtr MinWorking,MaxWorking; public uint ActiveLimit; public UIntPtr Affinity; public uint Priority,Scheduling; }
    [StructLayout(LayoutKind.Sequential)] private struct IO { public ulong ReadOps,WriteOps,OtherOps,ReadBytes,WriteBytes,OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] private struct EXTENDED { public BASIC Basic; public IO Io; public UIntPtr ProcessMemory,JobMemory,PeakProcessMemory,PeakJobMemory; }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern IntPtr CreateJobObject(IntPtr security,string name);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool SetInformationJobObject(IntPtr job,int kind,ref EXTENDED info,int size);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll")] private static extern bool TerminateJobObject(IntPtr job,uint exitCode);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    private IntPtr job;
    private int stopped;
    internal Process Process { get; private set; }
    internal OwnedBackendProcess(Process process) {
        Process=process; job=CreateJobObject(IntPtr.Zero,null);
        try {
            Descriptor.Need(job!=IntPtr.Zero,"backend_job_creation_failed"); EXTENDED info=new EXTENDED(); info.Basic.Flags=0x2000;
            Descriptor.Need(SetInformationJobObject(job,9,ref info,Marshal.SizeOf(typeof(EXTENDED))) && AssignProcessToJobObject(job,process.Handle),"backend_job_assignment_failed");
        } catch { try {if(!process.HasExited)process.Kill();}catch{} if(job!=IntPtr.Zero)CloseHandle(job);job=IntPtr.Zero;throw; }
    }
    public void Dispose() {
        if(Interlocked.Exchange(ref stopped,1)!=0)return;
        try { if(!Process.HasExited)Process.WaitForExit(300); } catch { }
        if(job!=IntPtr.Zero) { TerminateJobObject(job,1); CloseHandle(job); job=IntPtr.Zero; }
        try { Process.WaitForExit(2000); }catch{} Process.Dispose();
    }
}
