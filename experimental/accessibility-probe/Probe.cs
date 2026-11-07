// One bounded, read-only MSAA feasibility probe. Production collector is untouched.
// No Name/Value/Description reads, input, settings changes, injection or memory reads.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Accessibility;

internal static class Probe
{
    private delegate bool EnumProc(IntPtr window, IntPtr parameter);
    private delegate void EventProc(IntPtr hook, uint eventId, IntPtr window, int objectId, int childId, uint thread, uint time);
    [StructLayout(LayoutKind.Sequential)] private struct Point { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] private struct Msg { public IntPtr Hwnd; public uint Message; public UIntPtr WParam; public IntPtr LParam; public uint Time; public Point Point; public uint Private; }
    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr parent, EnumProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder name, int count);
    [DllImport("user32.dll")] private static extern IntPtr GetThreadDesktop(uint thread);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder data, int length, out int needed);
    [DllImport("kernel32.dll")] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] private static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder name, ref uint length);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("oleacc.dll")] private static extern int AccessibleObjectFromWindow(IntPtr window, uint objectId, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object accessible);
    [DllImport("oleacc.dll")] private static extern int AccessibleChildren(IAccessible container, int start, int count, [Out, MarshalAs(UnmanagedType.LPArray, SizeParamIndex=2)] object[] children, out int obtained);
    [DllImport("oleacc.dll")] private static extern int WindowFromAccessibleObject(IAccessible accessible, out IntPtr window);
    [DllImport("oleacc.dll")] private static extern int ObjectFromLresult(IntPtr result, ref Guid iid, IntPtr wParam, [MarshalAs(UnmanagedType.Interface)] out object accessible);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module, EventProc callback, uint process, uint thread, uint flags);
    [DllImport("user32.dll")] private static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll")] private static extern bool PeekMessage(out Msg message, IntPtr window, uint min, uint max, uint remove);
    [DllImport("user32.dll")] private static extern bool TranslateMessage(ref Msg message);
    [DllImport("user32.dll")] private static extern IntPtr DispatchMessage(ref Msg message);
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static readonly Dictionary<string,int> Events = new Dictionary<string,int>();
    private static EventProc Callback;
    private static IntPtr Source;
    private static int Pid;
    private static string ExpectedPath;
    private static int EventCount;
    private static readonly Guid AccessibleIid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");

    [STAThread] private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        IntPtr hook = IntPtr.Zero;
        try
        {
            if (args.Length != 3) throw new ArgumentException();
            Source = new IntPtr(Convert.ToInt64(args[0],16));
            Pid = Int32.Parse(args[1]);
            ExpectedPath = Path.GetFullPath(args[2]);
            if (Desktop() != "Default" || !Valid(Source, "CefBrowserWindow")) throw new InvalidOperationException();
            List<IntPtr> windows = Targets();
            Emit(new Dictionary<string,object>{{"phase","before_hook"},{"targets",Capture(windows)},{"message_count",0}});
            Callback = delegate(IntPtr h, uint ev, IntPtr window, int obj, int child, uint thread, uint time) {
                if (!IsWindow(window) || !(window==Source || IsChild(Source,window))) return;
                uint pid; GetWindowThreadProcessId(window,out pid); if (pid != Pid || !PathMatches((int)pid)) return;
                string key=ev.ToString("x"); if (!Events.ContainsKey(key)) Events[key]=0; Events[key]++; EventCount++;
            };
            // OUTOFCONTEXT=0, SKIPOWNPROCESS=2; process-scoped, never DLL injection.
            hook=SetWinEventHook(0x8000,0x80ff,IntPtr.Zero,Callback,(uint)Pid,0,2);
            Emit(new Dictionary<string,object>{{"phase","hook_registered"},{"registered",hook!=IntPtr.Zero},{"scope","selected_cef_native_subtree"}});
            DateTime started=DateTime.UtcNow;
            int next=2;
            while ((DateTime.UtcNow-started).TotalSeconds<12)
            {
                Msg msg;
                while (PeekMessage(out msg,IntPtr.Zero,0,0,1)) { TranslateMessage(ref msg); DispatchMessage(ref msg); }
                if ((DateTime.UtcNow-started).TotalSeconds>=next)
                {
                    if (!Valid(Source,"CefBrowserWindow")) throw new InvalidOperationException();
                    Emit(new Dictionary<string,object>{{"phase","during_hook"},{"elapsed_seconds",next},{"targets",Capture(Targets())},{"event_count",EventCount}});
                    next+=4;
                }
                Thread.Sleep(30);
            }
            bool unhooked=hook!=IntPtr.Zero && UnhookWinEvent(hook); hook=IntPtr.Zero;
            Emit(new Dictionary<string,object>{{"phase","after_hook"},{"unhooked",unhooked},{"targets",Capture(Targets())},{"event_count",EventCount},{"event_types",Events},{"message_count",0},{"text_properties_read",false}});
            GC.KeepAlive(Callback);
            return 0;
        }
        catch(Exception e) { Emit(new Dictionary<string,object>{{"phase","error"},{"error_code",e.GetType().Name},{"message_count",0}}); return 2; }
        finally { if(hook!=IntPtr.Zero) UnhookWinEvent(hook); }
    }
    private static List<IntPtr> Targets()
    {
        List<IntPtr> result=new List<IntPtr>(); result.Add(Source);
        EnumChildWindows(Source,delegate(IntPtr window,IntPtr p) { if(Valid(window,"Chrome_WidgetWin_1") && IsChild(Source,window)) result.Add(window); return result.Count<8; },IntPtr.Zero);
        return result;
    }
    private static List<object> Capture(List<IntPtr> windows)
    {
        List<object> result=new List<object>();
        foreach(IntPtr window in windows)
        {
            if(!Valid(window,window==Source?"CefBrowserWindow":"Chrome_WidgetWin_1")) continue;
            result.Add(new Dictionary<string,object>{{"class_name",Class(window)},{"direct_msaa",Read(window,false)},{"standard_wm_getobject",Read(window,true)}});
        }
        return result;
    }
    private static object Read(IntPtr window,bool directMessage)
    {
        Dictionary<string,object> output=new Dictionary<string,object>(); object value=null;
        try
        {
            Guid iid=AccessibleIid; int hr;
            if(directMessage)
            {
                IntPtr result;
                IntPtr sent=SendMessageTimeout(window,0x003d,IntPtr.Zero,new IntPtr(-4),3,1000,out result);
                output["reply_received"]=sent!=IntPtr.Zero; output["object_result_nonzero"]=result!=IntPtr.Zero;
                if(sent==IntPtr.Zero || result==IntPtr.Zero) { output["status"]="no_native_object"; return output; }
                hr=ObjectFromLresult(result,ref iid,IntPtr.Zero,out value);
            }
            else hr=AccessibleObjectFromWindow(window,unchecked((uint)-4),ref iid,out value);
            output["hresult"]=hr;
            IAccessible accessible=value as IAccessible;
            if(hr<0 || accessible==null) { output["status"]="unavailable"; return output; }
            Dictionary<string,int> roles=new Dictionary<string,int>(), states=new Dictionary<string,int>();
            int nodes=0,errors=0; bool truncated=false;
            output["root_child_count"]=accessible.accChildCount;
            output["root_role"]=Role(accessible.get_accRole(0));
            object state=accessible.get_accState(0); output["root_state"]=state is int?(object)(int)state:null;
            Walk(accessible,0,roles,states,ref nodes,ref errors,ref truncated,0);
            output["status"]="available"; output["visited_nodes"]=nodes; output["provider_errors"]=errors; output["role_counts"]=roles; output["state_counts"]=states; output["truncated"]=truncated;
        }
        catch(Exception e) { output["status"]="provider_error"; output["error_code"]=e.GetType().Name; }
        finally { if(value!=null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value); }
        return output;
    }
    private static void Walk(IAccessible accessible,object child,Dictionary<string,int> roles,Dictionary<string,int> states,ref int nodes,ref int errors,ref bool truncated,int depth)
    {
        if(nodes>=400 || depth>30) { truncated=true; return; }
        try
        {
            IntPtr owner; uint pid;
            if(WindowFromAccessibleObject(accessible,out owner)<0) { errors++; return; }
            GetWindowThreadProcessId(owner,out pid);
            if(pid!=Pid || !PathMatches((int)pid) || !(owner==Source || IsChild(Source,owner))) { errors++; return; }
            nodes++;
            string role=Role(accessible.get_accRole(child)); Count(roles,role);
            object stateObject=accessible.get_accState(child); if(!(stateObject is int)) { errors++; return; }
            int state=(int)stateObject; Count(states,state.ToString("x"));
            if((state & (0x20000000|0x8000|0x10000))!=0 || role=="42" || role=="46") return;
            if(!(child is int) || (int)child!=0) return;
            int count=accessible.accChildCount;
            if(count<0 || count>200 || count>400-nodes) { truncated=true; return; }
            if(count==0) return;
            object[] children=new object[count]; int obtained;
            if(AccessibleChildren(accessible,0,count,children,out obtained)<0 || obtained!=count) { errors++; return; }
            for(int i=0;i<obtained;i++) { IAccessible next=children[i] as IAccessible; if(next!=null) Walk(next,0,roles,states,ref nodes,ref errors,ref truncated,depth+1); else if(children[i] is int) Walk(accessible,children[i],roles,states,ref nodes,ref errors,ref truncated,depth+1); else errors++; }
        }
        catch(Exception) { errors++; }
    }
    private static string Role(object role) { return role is int && (int)role>=0 && (int)role<=127?((int)role).ToString():"custom"; }
    private static void Count(Dictionary<string,int> values,string key) { if(!values.ContainsKey(key)) values[key]=0; values[key]++; }
    private static string Class(IntPtr window) { StringBuilder text=new StringBuilder(256); GetClassName(window,text,text.Capacity); return text.ToString(); }
    private static bool Valid(IntPtr window,string cls) { uint pid; GetWindowThreadProcessId(window,out pid); return IsWindow(window)&&IsWindowVisible(window)&&pid==Pid&&Class(window)==cls&&PathMatches((int)pid); }
    private static bool PathMatches(int pid)
    {
        IntPtr process=OpenProcess(0x1000,false,(uint)pid); if(process==IntPtr.Zero) return false;
        try { StringBuilder path=new StringBuilder(32768); uint length=(uint)path.Capacity; return QueryFullProcessImageName(process,0,path,ref length)&&String.Equals(Path.GetFullPath(path.ToString()),ExpectedPath,StringComparison.OrdinalIgnoreCase); }
        finally { CloseHandle(process); }
    }
    private static string Desktop() { StringBuilder text=new StringBuilder(256); int needed; return GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()),2,text,text.Capacity*2,out needed)?text.ToString():"unknown"; }
    private static void Emit(object value) { Console.WriteLine(Json.Serialize(value)); Console.Out.Flush(); }
}
