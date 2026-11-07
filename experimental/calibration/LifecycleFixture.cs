// Synthetic subprocess only. Never connects to DingTalk or opens a window.
using System;
using System.Diagnostics;
using System.Reflection;
using System.Threading;
using System.Web.Script.Serialization;

internal static class LifecycleFixture {
    private static int Main(string[] args) {
        if(args.Length==1 && args[0]=="--child") {Thread.Sleep(60000);return 0;}
        if(args.Length!=1 || args[0]!="--parent")return 2;
        if(Console.ReadLine()!="{\"type\":\"ui_job_ready\"}")return 3;
        ProcessStartInfo info=new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,"--child"); info.UseShellExecute=false;info.CreateNoWindow=true;
        using(Process child=Process.Start(info)) {
            Console.WriteLine(new JavaScriptSerializer().Serialize(new {parent_pid=Process.GetCurrentProcess().Id,child_pid=child.Id})); Console.Out.Flush();
            // Deliberately never read stdin again: cancellation must not depend on it.
            Thread.Sleep(60000);
        }
        return 0;
    }
}
