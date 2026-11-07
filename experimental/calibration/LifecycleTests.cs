using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

internal static class LifecycleTests {
    private sealed class Fixture : IDisposable {
        internal OwnedBackendProcess Owned;
        internal Process Parent,Child;
        public void Dispose(){if(Owned!=null)Owned.Dispose();if(Parent!=null)Parent.Dispose();if(Child!=null)Child.Dispose();}
    }
    private static Fixture Start() {
        string fixture=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"CalibrationLifecycleFixture.exe"); Descriptor.Need(File.Exists(fixture),"lifecycle_fixture_missing");
        ProcessStartInfo info=new ProcessStartInfo(fixture,"--parent"); info.UseShellExecute=false;info.CreateNoWindow=true;info.RedirectStandardInput=true;info.RedirectStandardOutput=true;info.RedirectStandardError=true;
        Process p=new Process();p.StartInfo=info;Fixture f=new Fixture();
        try {
            Descriptor.Need(p.Start(),"lifecycle_fixture_start_failed"); f.Owned=new OwnedBackendProcess(p); f.Parent=Process.GetProcessById(p.Id);
            p.StandardInput.WriteLine("{\"type\":\"ui_job_ready\"}");p.StandardInput.Flush();
            Task<string> line=p.StandardOutput.ReadLineAsync(); Descriptor.Need(line.Wait(3000),"lifecycle_fixture_timeout");
            Dictionary<string,object> metadata=Descriptor.Obj(new JavaScriptSerializer().DeserializeObject(line.Result));
            Descriptor.Need(Descriptor.Int(metadata,"parent_pid")==p.Id,"lifecycle_fixture_identity_error");
            f.Child=Process.GetProcessById(checked((int)Descriptor.Int(metadata,"child_pid"))); return f;
        }catch{f.Dispose();try{if(!p.HasExited)p.Kill();}catch{}p.Dispose();throw;}
    }
    private static void Exited(Fixture f) {Descriptor.Need(f.Parent.WaitForExit(3000)&&f.Child.WaitForExit(3000),"lifecycle_orphan_process");}
    internal static int Run() {
        int cases=0;
        using(Fixture f=Start()){f.Owned.Dispose();Exited(f);cases++;}
        using(Fixture f=Start()) {
            try{throw new InvalidOperationException("synthetic");}catch(InvalidOperationException){}finally{f.Owned.Dispose();}
            Exited(f);cases++;
        }
        using(Fixture f=Start()) {
            Task writing=Task.Run(delegate{try{f.Owned.Process.StandardInput.Write(new string('x',1024*1024));f.Owned.Process.StandardInput.Flush();}catch{}});
            Stopwatch clock=Stopwatch.StartNew(); f.Owned.Dispose(); clock.Stop(); Exited(f);
            Descriptor.Need(clock.ElapsedMilliseconds<3500 && writing.Wait(3000),"lifecycle_blocked_pipe_cleanup_failed");cases++;
        }
        Fixture late=null;
        Task<OwnedBackendProcess> starting=Task.Run(delegate {late=Start();Thread.Sleep(300);return late.Owned;});
        Descriptor.Need(!starting.Wait(20),"lifecycle_late_start_fixture_failed"); BackendBridge.CleanupLateStart(starting);
        Descriptor.Need(starting.Wait(5000),"lifecycle_late_start_timeout");
        try {Exited(late);cases++;}finally{late.Dispose();}
        Console.WriteLine(new JavaScriptSerializer().Serialize(new {status="lifecycle_test_passed",cases=cases,orphan_children=0,ui_shown=false,pixels_read=false,ocr_started=false,live_target_queried=false,authorization_granted=false}));return 0;
    }
}
