// Independent metadata-only semantic calibration. No capture/OCR APIs are used.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class CalibrationFailure : Exception {
    internal readonly string Code;
    internal CalibrationFailure(string code) : base(code) { Code = code; }
}

internal struct Box {
    internal int Left, Top, Right, Bottom;
    internal int Width { get { return Right - Left; } }
    internal int Height { get { return Bottom - Top; } }
    internal bool Valid { get { return (long)Right - Left >= 1 && (long)Right - Left <= 16000 && (long)Bottom - Top >= 1 && (long)Bottom - Top <= 16000; } }
    internal bool Contains(Box b) { return Valid && b.Valid && b.Left >= Left && b.Top >= Top && b.Right <= Right && b.Bottom <= Bottom; }
    internal bool Overlaps(Box b) { return Valid && b.Valid && Math.Max(Left,b.Left)<Math.Min(Right,b.Right) && Math.Max(Top,b.Top)<Math.Min(Bottom,b.Bottom); }
    internal bool Same(Box b) { return Left == b.Left && Top == b.Top && Right == b.Right && Bottom == b.Bottom; }
    internal Dictionary<string, object> Json { get { return new Dictionary<string, object> { {"left",Left}, {"top",Top}, {"right",Right}, {"bottom",Bottom} }; } }
    internal Rectangle Rectangle { get { return new Rectangle(Left, Top, Width, Height); } }
}

internal sealed class WindowBinding {
    internal long Hwnd;
    internal string ClassName;
    internal Box Bounds;
    internal Dictionary<string, object> Json { get { return new Dictionary<string, object> { {"hwnd",Hwnd.ToString("x")}, {"class_name",ClassName}, {"bounds",Bounds.Json} }; } }
}

internal sealed class SourceBinding {
    internal string Path, Sha256;
    internal long Length, WriteTicks;
    internal Dictionary<string, object> Document;
    internal Dictionary<string, object> Json { get { return new Dictionary<string, object> { {"path",Path}, {"sha256",Sha256} }; } }
}

internal sealed class Descriptor {
    internal const string Schema = "dingtalkbridge.region-calibration-descriptor.v1";
    internal const string ExpectedImage = @"D:\Program Files\DingDing\main\current_new\DingTalk.exe";
    internal string FilePath, FileSha256, AppVersion, AppSha256;
    internal long FileLength, FileWriteTicks, AppLength, AppWriteTicks;
    internal DateTimeOffset Issued, Expires, ProcessStart;
    internal int Pid, Session;
    internal bool SemanticIncomplete;
    internal bool RequiresBackendTechnicalReview;
    internal List<Box> StrongInputs = new List<Box>();
    internal WindowBinding Top, Semantic, Message;
    internal SourceBinding Scene, Wgc;
    internal Box Roi,FrameBounds;

    internal static void Need(bool condition, string code) { if (!condition) throw new CalibrationFailure(code); }
    internal static Dictionary<string, object> Obj(object o) { Dictionary<string, object> v = o as Dictionary<string, object>; Need(v != null, "invalid_object"); return v; }
    internal static object Field(Dictionary<string, object> o, string k) { object v; Need(o.TryGetValue(k, out v), "missing_" + k); return v; }
    internal static Dictionary<string, object> Child(Dictionary<string, object> o, string k) { return Obj(Field(o,k)); }
    internal static string Str(Dictionary<string, object> o, string k) { string v = Field(o,k) as string; Need(!String.IsNullOrWhiteSpace(v), "invalid_" + k); return v; }
    internal static long Int(Dictionary<string, object> o, string k) {
        object value = Field(o,k);
        Need(value is int || value is long, "invalid_" + k);
        return Convert.ToInt64(value, CultureInfo.InvariantCulture);
    }
    internal static void Bool(Dictionary<string, object> o, string k, bool v) { object x = Field(o,k); Need(x is bool && (bool)x == v, "unsafe_" + k); }
    internal static bool Boolean(Dictionary<string, object> o, string k) { object x=Field(o,k); Need(x is bool,"invalid_"+k); return (bool)x; }
    internal static object[] Array(Dictionary<string, object> o, string k) { object[] x=Field(o,k) as object[]; Need(x!=null,"invalid_"+k); return x; }
    internal static DateTimeOffset Time(Dictionary<string, object> o, string k) {
        DateTimeOffset t; string s = Str(o,k);
        Need((s.EndsWith("Z",StringComparison.Ordinal) || s.EndsWith("+00:00",StringComparison.Ordinal)) && DateTimeOffset.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out t), "invalid_"+k);
        return DateTimeOffset.Parse(s, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind);
    }
    internal static string Hash(string path) {
        using (FileStream f = new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read))
        using (SHA256 h = SHA256.Create()) return BitConverter.ToString(h.ComputeHash(f)).Replace("-","").ToLowerInvariant();
    }
    internal static string Sha(Dictionary<string, object> o, string k) {
        string s = Str(o,k); Need(s.Length == 64, "invalid_"+k);
        foreach (char c in s) Need((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'), "invalid_"+k);
        return s;
    }
    internal static Dictionary<string, object> Read(string path, long maxBytes) {
        FileInfo info = new FileInfo(path); Need(info.Exists && info.Length > 0 && info.Length <= maxBytes, "invalid_metadata_file");
        string json = File.ReadAllText(path, new UTF8Encoding(false,true));
        JavaScriptSerializer serializer = new JavaScriptSerializer(); serializer.MaxJsonLength = (int)maxBytes; serializer.RecursionLimit = 128;
        return Obj(serializer.DeserializeObject(json));
    }
    internal static string FullPath(string p) { Need(System.IO.Path.IsPathRooted(p), "absolute_path_required"); return System.IO.Path.GetFullPath(p); }
    internal static Box Rect(object o) {
        Dictionary<string, object> r = Obj(o); Box b = new Box();
        if (r.ContainsKey("left")) {
            b.Left=checked((int)Int(r,"left")); b.Top=checked((int)Int(r,"top")); b.Right=checked((int)Int(r,"right")); b.Bottom=checked((int)Int(r,"bottom"));
        } else {
            b.Left=checked((int)Int(r,"x")); b.Top=checked((int)Int(r,"y")); b.Right=checked(b.Left+(int)Int(r,"width")); b.Bottom=checked(b.Top+(int)Int(r,"height"));
        }
        Need(b.Valid, "invalid_rectangle"); return b;
    }
    internal static long Handle(string value) { long n; Need(Int64.TryParse(value,NumberStyles.AllowHexSpecifier,CultureInfo.InvariantCulture,out n) && n > 0, "invalid_hwnd"); return n; }
    internal static WindowBinding Window(Dictionary<string, object> o) {
        return new WindowBinding { Hwnd=Handle(Str(o,"hwnd")), ClassName=Str(o,"class_name"), Bounds=Rect(Field(o,"bounds")) };
    }
    internal static SourceBinding Source(Dictionary<string, object> o, DateTimeOffset issued) {
        SourceBinding s = new SourceBinding { Path=FullPath(Str(o,"path")), Sha256=Sha(o,"sha256") };
        FileInfo f = new FileInfo(s.Path); Need(f.Exists && f.Length > 0 && f.Length <= 8*1024*1024, "invalid_source_file");
        Need(f.LastWriteTimeUtc >= issued.UtcDateTime.AddSeconds(-90) && f.LastWriteTimeUtc <= issued.UtcDateTime.AddSeconds(30), "stale_source_metadata");
        s.Length=f.Length; s.WriteTicks=f.LastWriteTimeUtc.Ticks;
        Need(Hash(s.Path)==s.Sha256, "source_hash_mismatch"); s.Document=Read(s.Path,8*1024*1024); return s;
    }
    internal static Descriptor Load(string path, DateTimeOffset now, bool backendReviewOnly = false) {
        Descriptor d = new Descriptor(); d.FilePath=FullPath(path);
        FileInfo f=new FileInfo(d.FilePath); d.FileLength=f.Length; d.FileWriteTicks=f.LastWriteTimeUtc.Ticks;
        d.FileSha256=Hash(d.FilePath); Dictionary<string, object> o=Read(d.FilePath,64*1024);
        Need(Str(o,"schema")==Schema,"unsupported_schema"); Need(Str(o,"intent")=="pending_semantic_calibration_only","invalid_intent");
        Need(Str(o,"coordinate_space")=="physical_screen_pixels","invalid_coordinate_space"); Bool(o,"capture_permitted",false);
        if(o.ContainsKey("authorization_granted"))Bool(o,"authorization_granted",false); if(o.ContainsKey("usable"))Bool(o,"usable",false);
        d.Issued=Time(o,"issued_utc"); d.Expires=Time(o,"expires_utc"); CheckFreshness(d.Issued,d.Expires,now);
        Dictionary<string, object> t=Child(o,"target");
        Need(String.Equals(FullPath(Str(t,"path")),ExpectedImage,StringComparison.OrdinalIgnoreCase),"unexpected_app_path");
        d.AppVersion=Str(t,"file_version"); d.AppSha256=Sha(t,"file_sha256"); d.Pid=checked((int)Int(t,"pid")); d.Session=checked((int)Int(t,"session_id"));
        Need(d.Pid>0 && d.Session>=0,"invalid_process_identity"); d.ProcessStart=Time(t,"process_start_utc");
        long processFiletime; Need(Int64.TryParse(Str(t,"process_start_filetime"),NumberStyles.None,CultureInfo.InvariantCulture,out processFiletime) && processFiletime==d.ProcessStart.UtcDateTime.ToFileTimeUtc(),"invalid_process_start_filetime");
        d.Top=Window(Child(t,"toplevel")); d.Semantic=Window(Child(t,"semantic")); d.Message=Window(Child(t,"message")); d.Roi=Rect(Field(o,"candidate_roi_screen")); d.FrameBounds=Rect(Field(o,"dwm_frame_bounds_screen"));
        Need(d.Top.Hwnd!=d.Semantic.Hwnd && d.Top.Hwnd!=d.Message.Hwnd && d.Semantic.Hwnd!=d.Message.Hwnd,"ambiguous_window_relationship");
        Need(d.Message.ClassName=="CefBrowserWindow" || d.Message.ClassName=="Chrome_WidgetWin_1","unexpected_message_class");
        Need(d.Top.Bounds.Contains(d.Roi) && d.Message.Bounds.Contains(d.Roi) && d.Semantic.Bounds.Contains(d.Roi) && d.Roi.Width>=100 && d.Roi.Height>=40,"roi_outside_message_bounds");
        Dictionary<string, object> sources=Child(o,"sources"); d.Scene=Source(Child(sources,"scene"),d.Issued); d.Wgc=Source(Child(sources,"wgc"),d.Issued);
        Need(!String.Equals(d.Scene.Path,d.Wgc.Path,StringComparison.OrdinalIgnoreCase),"distinct_sources_required");
        d.ValidateScene(backendReviewOnly); d.ValidateWgc(); return d;
    }
    internal static void CheckFreshness(DateTimeOffset issued, DateTimeOffset expires, DateTimeOffset now) {
        Need(issued<=now.AddSeconds(30) && issued>=now.AddMinutes(-5) && expires>now && expires>issued && expires<=issued.AddMinutes(5),"expired_or_invalid_descriptor_time");
    }
    private void ValidateScene(bool backendReviewOnly) {
        Dictionary<string,object> s=Scene.Document; Need(Str(s,"type")=="scene","full_scene_metadata_required");
        Bool(s,"metadata_only",true); Bool(s,"text_properties_read",false); Bool(s,"capture_permitted",false);
        Need(Str(s,"coordinate_space")=="physical_screen_pixels" && Str(s,"dpi_awareness")=="per_monitor_v2","invalid_scene_coordinates");
        string status=Str(s,"status"); Need(status=="no_safe_unmasked_region" || status=="requires_reviewed_gpu_backend","incomplete_scene");
        DateTimeOffset observed=Time(s,"observed_at"); Need(observed>=Issued.AddSeconds(-90) && observed<=Issued.AddSeconds(30),"stale_scene");
        Dictionary<string,object> c=Child(s,"candidate_scene"), top=Child(c,"toplevel"), cov=Child(c,"coverage");
        bool complete=Boolean(cov,"complete"); long errors=Int(cov,"provider_errors");
        Need(errors>=0 && Int(cov,"native_child_errors")==0,"scene_technical_error");
        Dictionary<string,object> gate=Child(c,"calibration_gate");
        Need(Int(gate,"schema_version")==1,"unsupported_calibration_gate");
        bool primaryTechnical=Boolean(gate,"technical_complete"),primaryStrong=Boolean(gate,"known_strong_input_geometry_complete"); int technicalErrors=Array(gate,"technical_errors").Length;
        RequiresBackendTechnicalReview=!primaryTechnical || !primaryStrong || technicalErrors!=0;
        if(!backendReviewOnly) {Bool(gate,"technical_complete",true);Bool(gate,"known_strong_input_geometry_complete",true);Need(technicalErrors==0,"unclassified_or_technical_scene_error");}
        Need(Int(gate,"unclassified_provider_error_count")==0 && Int(gate,"classified_provider_error_count")==errors,"unclassified_or_technical_scene_error");
        SemanticIncomplete=Boolean(gate,"semantic_incomplete"); object[] semanticCodes=Array(gate,"semantic_error_codes");
        Need(semanticCodes.Length<=64,"invalid_semantic_error_codes");
        foreach(object code in semanticCodes) Need(code is string && ((string)code=="unknown_semantic" || (string)code=="semantic_scan_incomplete"),"unclassified_semantic_error");
        Need(complete ? errors==0 : SemanticIncomplete && errors>0 && semanticCodes.Length>0,"incomplete_scene_without_semantic_classification");
        object[] strong=Array(gate,"known_strong_input_rects_screen"); Need(strong.Length<=512,"strong_input_budget_exceeded");
        foreach(object input in strong) { Box box=Rect(input); Need(!box.Overlaps(Roi),"known_strong_input_overlaps_roi"); StrongInputs.Add(box); }
        Need(Int(c,"layout_whitelist_exemptions")==0 && Int(c,"outside_source_tree_ignored_count")==0,"scene_exemptions_forbidden");
        Bool(c,"root_window_value_exception_applied",false); Bool(c,"gpu_redaction_before_cpu_required",true);
        Need(Handle(Str(top,"hwnd"))==Top.Hwnd && Int(top,"pid")==Pid && Int(top,"session_id")==Session,"scene_target_mismatch");
        Need(Str(top,"native_class")==Top.ClassName && String.Equals(FullPath(Str(top,"verified_process_path")),ExpectedImage,StringComparison.OrdinalIgnoreCase),"scene_identity_mismatch");
        Bool(top,"visible",true); Bool(top,"iconic",false); Need(Int(top,"dwm_frame_hresult")==0 && Int(top,"dwm_cloak_hresult")==0 && Int(top,"dwm_cloaked")==0,"scene_window_unavailable");
        Bool(top,"display_affinity_read_ok",true); Need(Int(top,"display_affinity")==0,"display_affinity_restricted");
        Need(Rect(Field(top,"window_rect_screen")).Same(Top.Bounds) && Rect(Field(top,"dwm_frame_rect_screen")).Same(FrameBounds) && FrameBounds.Contains(Roi),"scene_geometry_mismatch");
        Need(Handle(Str(c,"selected_cef_hwnd"))==Message.Hwnd && Handle(Str(c,"selected_semantic_hwnd"))==Semantic.Hwnd && Rect(Field(c,"message_roi_screen")).Same(Roi),"scene_selection_mismatch");
        Need(Rect(Field(c,"selected_cef_rect_screen")).Same(Message.Bounds) && Rect(Field(c,"selected_semantic_rect_screen")).Same(Semantic.Bounds),"scene_selected_bounds_mismatch");
        Need(Str(c,"selected_cef_native_class")==Message.ClassName && Str(c,"selected_semantic_native_class")==Semantic.ClassName,"scene_selected_class_mismatch");
    }
    private void ValidateWgc() {
        Dictionary<string,object> w=Wgc.Document; Need(Str(w,"status")=="metadata_ready_review_required","wgc_metadata_not_ready");
        foreach(string k in new string[]{"capture_started","frame_pool_created","cpu_image_created","capture_ready","coordinate_mapping_ambiguous"}) Bool(w,k,false);
        foreach(string k in new string[]{"item_created","device_supported","display_affinity_read","same_session","verified_ancestry","size_matches_dwm","size_matches_window","roi_mapping_valid","native_metadata_stable","new_scene_proof_review_required"}) Bool(w,k,true);
        Need(Int(w,"create_hresult")==0 && Int(w,"display_affinity")==0,"wgc_creation_or_protection_invalid");
        Need(Str(w,"coordinate_basis")=="dwm_extended_frame_bounds" && Int(w,"toplevel_hwnd")==Top.Hwnd && Int(w,"toplevel_pid")==Pid && Str(w,"toplevel_class")==Top.ClassName,"wgc_target_mismatch");
        Need(Rect(Field(w,"window_rect_screen")).Same(Top.Bounds) && Rect(Field(w,"cef_rect_screen")).Same(Message.Bounds) && Rect(Field(w,"selected_roi_screen")).Same(Roi),"wgc_geometry_mismatch");
        Box frame=Rect(Field(w,"dwm_frame_bounds_screen")), texture=Rect(Field(w,"roi_texture_pixels"));
        Need(frame.Same(FrameBounds) && frame.Same(Rect(Field(Child(Child(Scene.Document,"candidate_scene"),"toplevel"),"dwm_frame_rect_screen"))),"frame_mapping_mismatch");
        Need(frame.Contains(Roi) && Int(w,"item_width")==frame.Width && Int(w,"item_height")==frame.Height && texture.Left==Roi.Left-frame.Left && texture.Top==Roi.Top-frame.Top && texture.Right==Roi.Right-frame.Left && texture.Bottom==Roi.Bottom-frame.Top,"texture_mapping_mismatch");
        Dictionary<string,object> app=Child(w,"application_identity"); Need(String.Equals(FullPath(Str(app,"path")),ExpectedImage,StringComparison.OrdinalIgnoreCase) && Str(app,"file_version")==AppVersion && Sha(app,"file_sha256")==AppSha256,"wgc_app_identity_mismatch");
    }
    internal void CheckFiles(bool fullHash) {
        CheckFreshness(Issued,Expires,DateTimeOffset.UtcNow);
        Need(Unchanged(FilePath,FileLength,FileWriteTicks) && Unchanged(Scene.Path,Scene.Length,Scene.WriteTicks) && Unchanged(Wgc.Path,Wgc.Length,Wgc.WriteTicks),"metadata_changed");
        if(fullHash) Need(Hash(FilePath)==FileSha256 && Hash(Scene.Path)==Scene.Sha256 && Hash(Wgc.Path)==Wgc.Sha256,"metadata_changed");
    }
    private static bool Unchanged(string path,long length,long ticks) { FileInfo f=new FileInfo(path); return f.Exists && f.Length==length && f.LastWriteTimeUtc.Ticks==ticks; }
    internal void VerifyLive(bool fullHash) {
        CheckFiles(fullHash); Native.Verify(this);
        FileInfo app=new FileInfo(ExpectedImage); Need(app.Exists,"app_unavailable");
        if(AppLength==0) { AppLength=app.Length; AppWriteTicks=app.LastWriteTimeUtc.Ticks; }
        Need(app.Length==AppLength && app.LastWriteTimeUtc.Ticks==AppWriteTicks,"app_file_changed");
        if(fullHash) Need(Hash(ExpectedImage)==AppSha256 && FileVersionInfo.GetVersionInfo(ExpectedImage).FileVersion==AppVersion,"app_fingerprint_changed");
    }
    internal Dictionary<string,object> TargetJson { get { return new Dictionary<string,object> { {"path",ExpectedImage},{"file_version",AppVersion},{"file_sha256",AppSha256},{"pid",Pid},{"session_id",Session},{"process_start_utc",ProcessStart.ToString("o")},{"process_start_filetime",ProcessStart.UtcDateTime.ToFileTimeUtc().ToString(CultureInfo.InvariantCulture)},{"toplevel",Top.Json},{"semantic",Semantic.Json},{"message",Message.Json} }; } }
}

internal static class Native {
    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left,Top,Right,Bottom; internal Box Box { get { return new Box{Left=Left,Top=Top,Right=Right,Bottom=Bottom}; } } }
    [DllImport("user32.dll",SetLastError=true)] private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsChild(IntPtr parent,IntPtr child);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr hwnd,uint flags);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern IntPtr GetTopWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr hwnd,uint command);
    [DllImport("user32.dll",EntryPoint="GetWindowLongPtrW")] private static extern IntPtr GetWindowLongPtr(IntPtr hwnd,int index);
    [DllImport("user32.dll")] private static extern bool GetLayeredWindowAttributes(IntPtr hwnd,out uint colorKey,out byte alpha,out uint flags);
    private delegate bool MonitorCallback(IntPtr monitor,IntPtr dc,ref RECT rect,IntPtr data);
    [DllImport("user32.dll")] private static extern bool EnumDisplayMonitors(IntPtr dc,IntPtr clip,MonitorCallback callback,IntPtr data);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd,StringBuilder value,int count);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd,out RECT rect);
    [DllImport("user32.dll")] private static extern bool GetWindowDisplayAffinity(IntPtr hwnd,out uint affinity);
    [DllImport("dwmapi.dll",EntryPoint="DwmGetWindowAttribute")] private static extern int DwmInt(IntPtr hwnd,int attribute,out int value,int size);
    [DllImport("dwmapi.dll",EntryPoint="DwmGetWindowAttribute")] private static extern int DwmRect(IntPtr hwnd,int attribute,out RECT value,int size);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern bool QueryFullProcessImageName(IntPtr process,uint flags,StringBuilder value,ref uint size);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [StructLayout(LayoutKind.Sequential)] private struct LASTINPUTINFO { public uint Size,Time; }
    [DllImport("user32.dll")] private static extern bool GetLastInputInfo(ref LASTINPUTINFO value);
    internal static Dictionary<string,object> InputFocusSnapshot() {
        LASTINPUTINFO info=new LASTINPUTINFO(); info.Size=(uint)Marshal.SizeOf(typeof(LASTINPUTINFO)); Descriptor.Need(GetLastInputInfo(ref info),"last_input_metadata_unavailable");
        return new Dictionary<string,object>{{"foreground_hwnd",GetForegroundWindow().ToInt64().ToString("x")},{"last_input_tick",info.Time}};
    }
    internal static void EnterPhysicalPixels() { Descriptor.Need(SetThreadDpiAwarenessContext(new IntPtr(-4))!=IntPtr.Zero,"dpi_context_unavailable"); }
    private static void Window(WindowBinding w,int pid) {
        IntPtr hwnd=new IntPtr(w.Hwnd); uint actual; GetWindowThreadProcessId(hwnd,out actual); RECT rect; StringBuilder cls=new StringBuilder(256);
        Descriptor.Need(IsWindow(hwnd) && IsWindowVisible(hwnd) && actual==(uint)pid,"window_identity_changed");
        Descriptor.Need(GetWindowRect(hwnd,out rect) && rect.Box.Same(w.Bounds),"window_geometry_changed");
        Descriptor.Need(GetClassName(hwnd,cls,cls.Capacity)>0 && cls.ToString()==w.ClassName,"window_class_changed");
    }
    internal static void Verify(Descriptor d) {
        using(Process p=Process.GetProcessById(d.Pid)) using(Process self=Process.GetCurrentProcess()) {
            Descriptor.Need(!p.HasExited && p.SessionId==d.Session && self.SessionId==d.Session && p.StartTime.ToUniversalTime()==d.ProcessStart.UtcDateTime,"process_or_session_changed");
        }
        IntPtr handle=OpenProcess(0x1000,false,(uint)d.Pid); Descriptor.Need(handle!=IntPtr.Zero,"process_path_unavailable");
        try { uint size=32768; StringBuilder path=new StringBuilder((int)size); Descriptor.Need(QueryFullProcessImageName(handle,0,path,ref size) && String.Equals(Path.GetFullPath(path.ToString()),Descriptor.ExpectedImage,StringComparison.OrdinalIgnoreCase),"unexpected_live_app_path"); }
        finally { CloseHandle(handle); }
        Window(d.Top,d.Pid); Window(d.Semantic,d.Pid); Window(d.Message,d.Pid);
        IntPtr top=new IntPtr(d.Top.Hwnd), semantic=new IntPtr(d.Semantic.Hwnd), message=new IntPtr(d.Message.Hwnd);
        Descriptor.Need(GetAncestor(top,2)==top && GetAncestor(semantic,2)==top && GetAncestor(message,2)==top && IsChild(top,semantic) && IsChild(top,message) && !IsIconic(top),"window_relationship_changed");
        int cloaked; uint affinity; RECT frame;
        Descriptor.Need(DwmInt(top,14,out cloaked,sizeof(int))==0 && cloaked==0 && GetWindowDisplayAffinity(top,out affinity) && affinity==0,"window_protected_or_hidden");
        Descriptor.Need(DwmRect(top,9,out frame,Marshal.SizeOf(typeof(RECT)))==0 && frame.Box.Same(Descriptor.Rect(Descriptor.Field(d.Wgc.Document,"dwm_frame_bounds_screen"))),"window_frame_changed");
    }
    internal static long CoveredArea(Box roi,List<Box> rectangles) {
        List<int> xs=new List<int>(); xs.Add(roi.Left); xs.Add(roi.Right);
        foreach(Box r in rectangles) if(roi.Overlaps(r)) { xs.Add(Math.Max(r.Left,roi.Left)); xs.Add(Math.Min(r.Right,roi.Right)); }
        xs.Sort(); long area=0;
        for(int i=1;i<xs.Count;i++) {
            int left=xs[i-1],right=xs[i]; if(left==right)continue; List<Box> spans=new List<Box>();
            foreach(Box r in rectangles) if(r.Left<right && r.Right>left && roi.Overlaps(r)) spans.Add(new Box{Top=Math.Max(roi.Top,r.Top),Bottom=Math.Min(roi.Bottom,r.Bottom)});
            spans.Sort(delegate(Box a,Box b){return a.Top.CompareTo(b.Top);}); int bottom=roi.Top; long height=0;
            foreach(Box span in spans) if(span.Bottom>bottom) {height+=span.Bottom-Math.Max(bottom,span.Top);bottom=span.Bottom;}
            area+=(long)(right-left)*height;
        }
        return area;
    }
    private static bool OurTransparentFrame(IntPtr hwnd,Descriptor d,IntPtr allowedFrame) {
        if(hwnd!=allowedFrame || hwnd==IntPtr.Zero)return false; uint pid,key,flags; byte alpha; RECT rect;
        GetWindowThreadProcessId(hwnd,out pid); long style=GetWindowLongPtr(hwnd,-20).ToInt64();
        return pid==(uint)Process.GetCurrentProcess().Id && (style&0x08080020L)==0x08080020L && GetWindowRect(hwnd,out rect) && rect.Box.Same(d.Roi) && GetLayeredWindowAttributes(hwnd,out key,out alpha,out flags) && (flags&1)==1 && key==0x00ff00ff;
    }
    internal static void VerifyRoiVisibility(Descriptor d,IntPtr allowedFrame) {
        IntPtr target=new IntPtr(d.Top.Hwnd); Descriptor.Need(GetAncestor(GetForegroundWindow(),2)==target,"user_must_foreground_dingtalk");
        List<Box> monitors=new List<Box>(); MonitorCallback callback=delegate(IntPtr monitor,IntPtr dc,ref RECT rect,IntPtr data){monitors.Add(rect.Box);return true;};
        Descriptor.Need(EnumDisplayMonitors(IntPtr.Zero,IntPtr.Zero,callback,IntPtr.Zero) && CoveredArea(d.Roi,monitors)==(long)d.Roi.Width*d.Roi.Height,"roi_not_fully_on_displays"); GC.KeepAlive(callback);
        int count=0; bool found=false;
        for(IntPtr hwnd=GetTopWindow(IntPtr.Zero);hwnd!=IntPtr.Zero;hwnd=GetWindow(hwnd,2)) {
            Descriptor.Need(++count<=4096,"zorder_proof_incomplete"); if(hwnd==target){found=true;break;}
            if(!IsWindowVisible(hwnd) || IsIconic(hwnd))continue;
            int cloaked; if(DwmInt(hwnd,14,out cloaked,sizeof(int))==0 && cloaked!=0)continue;
            if(OurTransparentFrame(hwnd,d,allowedFrame))continue;
            RECT rect; Descriptor.Need(GetWindowRect(hwnd,out rect),"occlusion_geometry_unavailable");
            Descriptor.Need(!rect.Box.Overlaps(d.Roi),"roi_occluded_by_another_window");
        }
        Descriptor.Need(found,"target_zorder_unavailable");
    }
}

internal static class Records {
    internal const string Statement = "框内仅有要读取的聊天记录，不含输入框、搜索框、密码等区域。";
    internal static string OutputPath(string path,Descriptor d) {
        string full=Descriptor.FullPath(path); Descriptor.Need(full.EndsWith(".pending.json",StringComparison.OrdinalIgnoreCase),"pending_json_extension_required");
        if(d!=null) foreach(string input in new string[]{d.FilePath,d.Scene.Path,d.Wgc.Path,Descriptor.ExpectedImage}) Descriptor.Need(!String.Equals(input,full,StringComparison.OrdinalIgnoreCase),"output_must_not_replace_input");
        return full;
    }
    internal static Dictionary<string,object> State(string status,string runId,Descriptor d,DateTimeOffset? confirmation,string reason) {
        DateTimeOffset now=DateTimeOffset.UtcNow;
        Dictionary<string,object> r=new Dictionary<string,object> {
            {"schema","dingtalkbridge.region-calibration-pending.v1"},{"status",status},{"run_id",runId},{"updated_utc",now.ToString("o")},
            {"authorization_granted",false},{"capture_permitted",false},{"usable",false},{"applies_automatically",false},{"ocr_started",false},{"pixels_read",false},
            {"user_semantic_confirmation",confirmation.HasValue && status=="pending_review"},{"reason",reason},{"monitor_pid",Process.GetCurrentProcess().Id},
            {"required_next_step","independent_review_and_fresh_full_scene_validation_before_any_capture"}
        };
        if(d!=null) {
            r["descriptor_sha256"]=d.FileSha256; r["descriptor_path"]=d.FilePath; r["expires_utc"]=d.Expires.ToString("o"); r["coordinate_space"]="physical_screen_pixels";
            r["target"]=d.TargetJson; r["candidate_roi_screen"]=d.Roi.Json;
            r["sources"]=new Dictionary<string,object>{{"scene",d.Scene.Json},{"wgc",d.Wgc.Json}};
            r["scene_hash_kind"]="full_root_scene_metadata_file_sha256_not_capture_authorization";
            r["requested_policy"]="user_confirmed_roi_once"; r["maximum_capture_attempts"]=1; r["attempt_consumed"]=false;
            r["automatic_semantic_classification_incomplete"]=d.SemanticIncomplete;
            r["dwm_frame_bounds_screen"]=Descriptor.Rect(Descriptor.Field(d.Wgc.Document,"dwm_frame_bounds_screen")).Json;
            r["live_guard_scope"]="source_file_identity_window_geometry_and_numeric_target_process_accessibility_events";
            r["monitor_process_start_utc"]=Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("o");
            List<object> strong=new List<object>(); foreach(Box b in d.StrongInputs) strong.Add(b.Json); r["known_strong_input_rects_screen"]=strong;
        }
        if(confirmation.HasValue && status=="pending_review") {
            r["confirmed_utc"]=confirmation.Value.ToString("o"); r["confirmation_statement"]=Statement;
            r["live_guard_until_utc"]=(now.AddSeconds(3)<d.Expires?now.AddSeconds(3):d.Expires).ToString("o");
            r["mask_policy_unchanged"]=true;
            r["confirmation_method"]="visible_local_panel_manual_checkbox_and_button";
        }
        return r;
    }
    internal static void Write(string path,Dictionary<string,object> state) {
        string directory=Path.GetDirectoryName(path); Descriptor.Need(Directory.Exists(directory),"output_directory_missing");
        string temp=Path.Combine(directory,".calibration-"+Guid.NewGuid().ToString("N")+".tmp");
        try {
            File.WriteAllText(temp,new JavaScriptSerializer().Serialize(state)+Environment.NewLine,new UTF8Encoding(false));
            if(File.Exists(path)) File.Replace(temp,path,null); else File.Move(temp,path);
        } finally { if(File.Exists(temp)) File.Delete(temp); }
    }
}

internal sealed class RegionFrame : Form {
    internal RegionFrame(Box roi) {
        Text="DingTalkBridge 消息区域边框"; FormBorderStyle=FormBorderStyle.None; ShowInTaskbar=false; TopMost=true;
        AutoScaleMode=AutoScaleMode.None;
        BackColor=Color.Magenta; TransparencyKey=Color.Magenta; StartPosition=FormStartPosition.Manual; Bounds=roi.Rectangle;
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams { get { CreateParams p=base.CreateParams; p.ExStyle|=0x08000000|0x20|0x80; return p; } }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e); using(Pen p=new Pen(Color.LimeGreen,4)) e.Graphics.DrawRectangle(p,2,2,Width-4,Height-4);
    }
}

// Out-of-context notifications only: no injection and no accessibility properties are read.
internal sealed class SceneEventGuard : IDisposable {
    private delegate void EventCallback(IntPtr hook,uint eventType,IntPtr hwnd,int objectId,int childId,uint threadId,uint eventTime);
    [DllImport("user32.dll",SetLastError=true)] private static extern IntPtr SetWinEventHook(uint eventMin,uint eventMax,IntPtr module,EventCallback callback,uint pid,uint thread,uint flags);
    [DllImport("user32.dll")] private static extern bool UnhookWinEvent(IntPtr hook);
    private IntPtr handle;
    private readonly EventCallback callback;
    internal static bool ShouldInvalidate(uint type,int objectId) { return objectId!=-8 && objectId!=-9 && type>=0x8000 && type<=0x8017; }
    internal SceneEventGuard(int pid,Action<string> changed) {
        callback=delegate(IntPtr hook,uint type,IntPtr hwnd,int objectId,int childId,uint thread,uint when) {
            // Includes NAMECHANGE, selection and CONTENTSCROLLED; no property is queried.
            if(ShouldInvalidate(type,objectId))changed("target_scene_or_input_event");
        };
        handle=SetWinEventHook(0x8000,0x8017,IntPtr.Zero,callback,(uint)pid,0,0x2);
        Descriptor.Need(handle!=IntPtr.Zero,"scene_event_guard_unavailable");
    }
    public void Dispose() { if(handle!=IntPtr.Zero) { UnhookWinEvent(handle); handle=IntPtr.Zero; } GC.KeepAlive(callback); }
}

// Mouse-operable controls that do not select/focus the no-activate panel.
internal sealed class NoActivateCheckBox : CheckBox {
    internal NoActivateCheckBox() {SetStyle(ControlStyles.Selectable,false);TabStop=false;}
    protected override void WndProc(ref Message m) {if(m.Msg==0x21){m.Result=new IntPtr(3);return;}base.WndProc(ref m);}
}
internal sealed class NoActivateButton : Button {
    internal NoActivateButton() {SetStyle(ControlStyles.Selectable,false);TabStop=false;}
    protected override void WndProc(ref Message m) {if(m.Msg==0x21){m.Result=new IntPtr(3);return;}base.WndProc(ref m);}
}

internal sealed class CalibrationPanel : Form {
    private readonly Descriptor descriptor;
    private readonly string output,runId;
    private readonly BackendBridge backend;
    private readonly RegionFrame frame;
    private readonly CheckBox semantic;
    private readonly Button confirm,cancel;
    private readonly Label status;
    private readonly Timer timer;
    private SceneEventGuard eventGuard;
    private DateTimeOffset? confirmed;
    private DateTimeOffset lastHeartbeat;
    private bool invalidated,closing;
    private bool onceStarted,onceFinished;
    private long sceneEpoch;
    private string BackendNotReadyText() {
        string code=backend==null?"":backend.Reason;
        if(code.Contains("technical") || code.Contains("geometry") || code.Contains("strong_input") || code.Contains("scene_incomplete"))return "不能读取：输入区域或窗口几何的技术证明不足。\r\n人工勾选不能跳过这项检查。";
        if(code.Contains("changed") || code.Contains("stale") || code.Contains("target"))return "不能读取：源窗口或区域证明已经变化，请重新校准。";
        if(code.Contains("digest") || code.Contains("hash") || code.Contains("binding"))return "不能读取：本机后端的代码校验尚未通过。";
        return "不能读取：一次读取后端尚未通过完整检查。";
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams { get { CreateParams p=base.CreateParams; p.ExStyle|=0x08000000; return p; } }
    protected override void WndProc(ref Message message) { if(message.Msg==0x21){message.Result=new IntPtr(3);return;}base.WndProc(ref message); }
    internal CalibrationPanel(Descriptor d,string outputPath,string id,BackendBridge onceBackend) {
        descriptor=d; output=outputPath; runId=id; backend=onceBackend;
        Text="DingTalkBridge · 消息区域校准"; Width=520; Height=354; AutoScaleMode=AutoScaleMode.Dpi;
        FormBorderStyle=FormBorderStyle.FixedDialog; MaximizeBox=false; MinimizeBox=false; TopMost=true; StartPosition=FormStartPosition.Manual;
        Font=new Font("Microsoft YaHei UI",9F); BackColor=Color.White;
        Label heading=new Label { Text="请核对绿色边框里的区域", Left=20,Top=18,Width=470,Height=28,Font=new Font(Font.FontFamily,12F,FontStyle.Bold) };
        Label explanation=new Label { Text=(d.SemanticIncomplete?"自动分类不完整，仅生成待处理校准记录，不开始采集。":"这里仅保存待审核区域，现在不会读取聊天文字。")+"\r\n后续启用后，仅在本机对核准的聊天记录做只读 OCR，\r\n不保存原始截图。请检查绿框是否包含输入、搜索或密码。",Left=20,Top=53,Width=470,Height=76 };
        if(backend!=null)explanation.Text=(d.SemanticIncomplete?"自动分类不完整，请您本人核对当前绿色边框。":"请您本人核对当前绿色边框。")+"\r\n点击后仅在本机 OCR 读取一次，聊天文本进入本机桥接\r\n供 MCP 使用。原始截图不保存、不上传。取消则不读取。";
        semantic=new NoActivateCheckBox { Text=Records.Statement,Left=20,Top=137,Width=470,Height=47,Checked=false,AutoCheck=true };
        if(backend!=null)semantic.Text=Records.Statement+"我确认只读取一次，并写入本机桥接供 MCP。";
        status=new Label { Text="未确认：这份区域配置不可用于读取。",Left=20,Top=195,Width=470,Height=45,ForeColor=Color.FromArgb(80,80,80) };
        confirm=new NoActivateButton { Text="确认区域并保存待审核配置",Left=20,Top=251,Width=285,Height=36,Enabled=false };
        if(backend!=null){confirm.Text="读取一次并提供本机 MCP";Text="DingTalkBridge · 本机只读一次";status.Text=backend.Ready?"尚未读取。请核对区域后勾选，再由您本人点击读取。":BackendNotReadyText();}
        cancel=new NoActivateButton { Text="取消",Left=323,Top=251,Width=164,Height=36 };
        Controls.AddRange(new Control[]{heading,explanation,semantic,status,confirm,cancel}); CancelButton=cancel;
        semantic.CheckedChanged+=delegate { confirm.Enabled=semantic.Checked && !invalidated && !confirmed.HasValue && !onceStarted && (backend==null || backend.Ready); };
        confirm.Click+=Confirm; cancel.Click+=delegate { Close(); };
        frame=new RegionFrame(d.Roi); timer=new Timer(); timer.Interval=250; timer.Tick+=Guard;
        Rectangle work=Screen.FromRectangle(d.Top.Bounds.Rectangle).WorkingArea;
        int left=work.Left+16, top=work.Top+16;
        Rectangle candidate=new Rectangle(left,top,Width,Height);
        // The panel never covers the selected ROI. If there is no room, refuse display.
        Rectangle[] options=new Rectangle[]{candidate,new Rectangle(work.Right-Width-16,work.Top+16,Width,Height),new Rectangle(work.Left+16,work.Bottom-Height-16,Width,Height),new Rectangle(work.Right-Width-16,work.Bottom-Height-16,Width,Height)};
        bool placed=false; foreach(Rectangle r in options) if(work.Contains(r) && !r.IntersectsWith(d.Roi.Rectangle)) { Location=r.Location; placed=true; break; }
        Descriptor.Need(placed,"no_panel_space_outside_roi");
        Shown+=delegate {
            try { eventGuard=new SceneEventGuard(d.Pid,delegate(string reason){sceneEpoch++;if((confirmed.HasValue || onceStarted) && !onceFinished && !invalidated && !closing)InvalidateProfile(reason);}); descriptor.VerifyLive(false); frame.Show(); timer.Start(); }
            catch(Exception e) { InvalidateProfile(ErrorCode(e)); }
        };
        FormClosing+=OnPanelClosing;
    }
    private async void Confirm(object sender,EventArgs args) {
        if(!semantic.Checked || confirmed.HasValue || invalidated || onceStarted) return;
        if(backend!=null) { await ConfirmOnce(); return; }
        try {
            descriptor.VerifyLive(true); Native.VerifyRoiVisibility(descriptor,frame.Handle); confirmed=DateTimeOffset.UtcNow;
            Records.Write(output,Records.State("pending_review",runId,descriptor,confirmed,"manual_semantic_confirmation_only"));
            semantic.Enabled=false; confirm.Enabled=false; cancel.Text="撤销并关闭";
            status.Text="已保存待审核配置，仍不可用于读取。\r\n移动窗口、改变大小或关闭此面板会撤销确认。"; lastHeartbeat=DateTimeOffset.UtcNow;
        } catch(Exception e) { InvalidateProfile(ErrorCode(e)); }
    }
    private Dictionary<string,object> CurrentOnceBinding() {
        Descriptor.Need(!invalidated && !closing && Visible && frame.Visible && semantic.Checked,"ui_confirmation_revoked"); descriptor.VerifyLive(true); Native.VerifyRoiVisibility(descriptor,frame.Handle);
        // Remove our own drawing before the backend may capture. No DingTalk activation occurs.
        frame.Hide(); Hide(); Native.VerifyRoiVisibility(descriptor,IntPtr.Zero);
        Dictionary<string,object> binding=Native.InputFocusSnapshot(); binding["ui_was_visible_for_manual_click"]=true;
        binding["target"]=descriptor.TargetJson; binding["candidate_roi_screen"]=descriptor.Roi.Json; binding["dwm_frame_bounds_screen"]=Descriptor.Rect(Descriptor.Field(descriptor.Wgc.Document,"dwm_frame_bounds_screen")).Json;
        binding["sources"]=new Dictionary<string,object>{{"scene",descriptor.Scene.Json},{"wgc",descriptor.Wgc.Json}};
        binding["scene_file_sha256"]=descriptor.Scene.Sha256; binding["input_focus_epoch"]=sceneEpoch;
        binding["ui_panel_hwnd"]=Handle.ToInt64().ToString("x"); binding["ui_frame_hwnd"]=frame.Handle.ToInt64().ToString("x");
        binding["confirmation_statement"]=Records.Statement+"同意仅本机 OCR 读取一次，文本进入本机桥接供 MCP；原始截图不保存、不上传。";
        return binding;
    }
    private async Task ConfirmOnce() {
        if(backend==null || !backend.Ready)return;
        try {
            descriptor.VerifyLive(true); Native.VerifyRoiVisibility(descriptor,frame.Handle);
            onceStarted=true; confirm.Enabled=false; semantic.Enabled=false; status.Text="正在执行这一次读取；任何失败都会停止，不会重试。";
            WriteAudit("attempt_started","manual_click_live_channel_only");
            Dictionary<string,object> result=await backend.RunFromClick(descriptor,CurrentOnceBinding);
            Descriptor.Need(!invalidated && !closing,"ui_confirmation_revoked");
            Dictionary<string,object> summary=Descriptor.Child(result,"summary"); string outcome=Descriptor.Str(summary,"status");
            onceFinished=true; timer.Stop(); if(eventGuard!=null)eventGuard.Dispose(); frame.Hide(); cancel.Text="关闭";
            status.Text=outcome=="completed"?"本次读取已结束。文本已由本机后端处理，未保存原始截图。":"本次读取已停止，没有自动重试。"; Show();
            WriteAudit(outcome,"backend_attempt_finished");
        } catch(Exception e) { if(!closing)InvalidateProfile(ErrorCode(e)); }
    }
    private void WriteAudit(string state,string reason) {
        Dictionary<string,object> r=new Dictionary<string,object>{{"schema","dingtalkbridge.roi-once-audit.v1"},{"status",state},{"updated_utc",DateTimeOffset.UtcNow.ToString("o")},{"reason",reason},{"run_id",runId},{"authorization_granted",false},{"usable",false},{"audit_only_not_authorization",true},{"attempt_started",onceStarted},{"descriptor_sha256",descriptor.FileSha256},{"target",descriptor.TargetJson},{"candidate_roi_screen",descriptor.Roi.Json},{"backend_digest",backend.BackendDigest},{"scene_binding_sha256",backend.SceneBinding}};
        Records.Write(output,r);
    }
    private void Guard(object sender,EventArgs args) {
        if(invalidated || closing) return;
        try {
            descriptor.VerifyLive(false);
            if(backend!=null && !backend.Ready && !onceStarted) {confirm.Enabled=false;status.Text=BackendNotReadyText();return;}
            try { Native.VerifyRoiVisibility(descriptor,frame.Handle); confirm.Enabled=semantic.Checked && !confirmed.HasValue && !onceStarted && (backend==null || backend.Ready); if(!confirmed.HasValue && !onceStarted)status.Text=backend==null?"未确认：这份区域配置不可用于读取。":(backend.Ready?"尚未读取。请核对区域后勾选，再由您本人点击读取。":BackendNotReadyText()); }
            catch(CalibrationFailure e) { if(confirmed.HasValue || onceStarted)throw; confirm.Enabled=false; status.Text=e.Code=="user_must_foreground_dingtalk"?"请您手动将钉钉切到前台，再核对绿色边框。":"绿框区域被其他窗口遮挡或未完全显示。\r\n请您手动移开遮挡的窗口。"; }
            if(confirmed.HasValue && (DateTimeOffset.UtcNow-lastHeartbeat).TotalSeconds>=1) {
                Records.Write(output,Records.State("pending_review",runId,descriptor,confirmed,"manual_semantic_confirmation_only")); lastHeartbeat=DateTimeOffset.UtcNow;
            }
        } catch(Exception e) { InvalidateProfile(ErrorCode(e)); }
    }
    private static string ErrorCode(Exception e) { CalibrationFailure f=e as CalibrationFailure; return f==null?"validation_or_write_failed":f.Code; }
    private void InvalidateProfile(string reason) {
        invalidated=true; timer.Stop(); frame.Hide(); semantic.Checked=false; semantic.Enabled=false; confirm.Enabled=false;
        if(eventGuard!=null) eventGuard.Dispose();
        if(backend!=null)backend.Cancel(reason);
        if(!Visible && !closing)Show();
        status.Text="区域已变化或校验已失效，确认已撤销。\r\n请关闭面板，重新生成最新区域配置。"; cancel.Text="关闭";
        try { if(backend==null)Records.Write(output,Records.State("invalidated",runId,descriptor,null,reason));else WriteAudit("invalidated",reason); } catch { status.Text+="\r\n撤销记录写入失败；旧记录仍不可用于读取。"; }
    }
    private void OnPanelClosing(object sender,FormClosingEventArgs args) {
        if(closing) return; closing=true; timer.Stop(); if(eventGuard!=null) eventGuard.Dispose(); frame.Close(); if(backend!=null)backend.Cancel("panel_closed_or_cancelled");
        try { if(backend==null)Records.Write(output,Records.State(invalidated?"invalidated":"cancelled",runId,descriptor,null,invalidated?"live_validation_failed":"panel_closed_or_cancelled"));else if(!onceFinished)WriteAudit(invalidated?"invalidated":"cancelled","panel_closed_or_cancelled"); }
        catch { /* All records are unusable and the confirmed heartbeat expires in <=3 seconds. */ }
    }
    protected override void Dispose(bool disposing) { if(disposing) { if(eventGuard!=null) eventGuard.Dispose(); if(timer!=null) timer.Dispose(); if(frame!=null) frame.Dispose(); } base.Dispose(disposing); }
}

internal static class Program {
    private static void Print(object o) { Console.WriteLine(new JavaScriptSerializer().Serialize(o)); }
    private static int SelfTest() {
        int count=0; Action<bool> check=delegate(bool ok){if(!ok)throw new CalibrationFailure("self_test_failed");count++;};
        check(Records.Statement.StartsWith("\u6846\u5185\u4ec5\u6709",StringComparison.Ordinal));
        check(SceneEventGuard.ShouldInvalidate(0x800C,-4)); check(SceneEventGuard.ShouldInvalidate(0x8015,-4));
        check(SceneEventGuard.ShouldInvalidate(0x800E,-4)); check(!SceneEventGuard.ShouldInvalidate(0x8002,-8)); check(!SceneEventGuard.ShouldInvalidate(3,0));
        Box outer=Descriptor.Rect(new Dictionary<string,object>{{"left",-200},{"top",100},{"right",800},{"bottom",900}});
        Box roi=Descriptor.Rect(new Dictionary<string,object>{{"x",0},{"y",200},{"width",500},{"height",400}});
        check(outer.Contains(roi)); check(roi.Same(new Box{Left=0,Top=200,Right=500,Bottom=600}));
        check(!roi.Contains(outer)); check(!outer.Contains(new Box{Left=-201,Top=100,Right=800,Bottom=900}));
        check(Native.CoveredArea(roi,new List<Box>{outer})==(long)roi.Width*roi.Height);
        check(Native.CoveredArea(roi,new List<Box>{new Box{Left=0,Top=200,Right=200,Bottom=600},new Box{Left=300,Top=200,Right=500,Bottom=600}})==160000);
        check(Native.CoveredArea(roi,new List<Box>{roi,roi})==(long)roi.Width*roi.Height);
        foreach(Dictionary<string,object> bad in new Dictionary<string,object>[] {new Dictionary<string,object>{{"left",0},{"top",0},{"right",0},{"bottom",100}},new Dictionary<string,object>{{"x",Int32.MaxValue},{"y",0},{"width",100},{"height",200}},new Dictionary<string,object>{{"left",0},{"top",0},{"right",16001},{"bottom",100}}}) {
            bool rejected=false;try{Descriptor.Rect(bad);}catch{rejected=true;}check(rejected);
        }
        DateTimeOffset now=DateTimeOffset.UtcNow;
        Descriptor.CheckFreshness(now,now.AddMinutes(1),now); count++;
        foreach(DateTimeOffset expires in new DateTimeOffset[]{now.AddSeconds(-1),now.AddMinutes(6)}) {bool rejected=false;try{Descriptor.CheckFreshness(now,expires,now);}catch(CalibrationFailure){rejected=true;}check(rejected);}
        foreach(string state in new string[]{"unconfirmed","cancelled","invalidated"}) {
            Dictionary<string,object> r=Records.State(state,"test",null,null,"self_test");
            check(!(bool)r["authorization_granted"] && !(bool)r["capture_permitted"] && !(bool)r["usable"] && !(bool)r["user_semantic_confirmation"] && !r.ContainsKey("confirmed_utc"));
        }
        string dir=Path.Combine(Path.GetTempPath(),"DingTalkBridge-calibration-test-"+Guid.NewGuid().ToString("N")); Directory.CreateDirectory(dir);
        try {
            string output=Records.OutputPath(Path.Combine(dir,"cancel.pending.json"),null);
            Records.Write(output,Records.State("cancelled","test",null,null,"explicit_cancel"));
            Dictionary<string,object> disk=Descriptor.Read(output,65536); check((string)disk["status"]=="cancelled" && !(bool)disk["usable"] && !(bool)disk["user_semantic_confirmation"]);
            Records.Write(output,Records.State("invalidated","test",null,null,"test")); check((string)Descriptor.Read(output,65536)["status"]=="invalidated");
            File.Delete(output);
        } finally { Directory.Delete(dir,false); }
        bool extensionRejected=false;try{Records.OutputPath(Path.Combine(Path.GetTempPath(),"profile.json"),null);}catch(CalibrationFailure){extensionRejected=true;}check(extensionRejected);
        string hash=new string('a',64),digest,scene,reason;
        Dictionary<string,object> ready=new Dictionary<string,object>{{"schema","dingtalkbridge.wgc-once-readiness.v1"},{"capture_started",false},{"pixels_read",false},{"authorization_granted",false},{"backend_digest",hash},{"profile_sha256",hash},{"scene_binding_sha256",hash},{"ready",false},{"technical_complete",false},{"known_strong_input_geometry_complete",false},{"reasons",new object[]{"scene_technical_incomplete"}}};
        check(!BackendBridge.ValidateReadiness(ready,hash,hash,out digest,out scene,out reason));
        ready["ready"]=true;ready["reasons"]=new object[0];
        bool technicalRejected=false;try{BackendBridge.ValidateReadiness(ready,hash,hash,out digest,out scene,out reason);}catch(CalibrationFailure){technicalRejected=true;}check(technicalRejected);
        ready["technical_complete"]=true;ready["known_strong_input_geometry_complete"]=true;
        check(BackendBridge.ValidateReadiness(ready,hash,hash,out digest,out scene,out reason));
        bool digestRejected=false;try{BackendBridge.ValidateReadiness(ready,new string('b',64),hash,out digest,out scene,out reason);}catch(CalibrationFailure){digestRejected=true;}check(digestRejected);
        ready["authorization_granted"]=true;
        bool preauthorizedRejected=false;try{BackendBridge.ValidateReadiness(ready,hash,hash,out digest,out scene,out reason);}catch(CalibrationFailure){preauthorizedRejected=true;}check(preauthorizedRejected);
        Print(new {status="self_test_passed",checks=count,ui_shown=false,pixels_read=false,ocr_started=false,authorization_granted=false}); return 0;
    }
    [STAThread] private static int Main(string[] args) {
        try {
            if(args.Length==0 || (args.Length==1 && args[0]=="--help")) {
                Print(new {tool="DingTalkBridge Region Calibration",usage="--self-test | --check --descriptor ABSOLUTE_JSON | --check-once-descriptor --descriptor ABSOLUTE_JSON | --check-live --descriptor ABSOLUTE_JSON | --show --descriptor ABSOLUTE_JSON --output ABSOLUTE.pending.json | --show-once --descriptor ABSOLUTE_JSON --backend-binding ABSOLUTE_JSON --output ABSOLUTE.once-audit.json | --cancel --output ABSOLUTE.pending.json",default_ui=false,confirmation_cli_available=false,pixels_read=false}); return args.Length==0?2:0;
            }
            if(args.Length==1 && args[0]=="--self-test") return SelfTest();
            if(args.Length==1 && args[0]=="--self-test-lifecycle") return LifecycleTests.Run();
            string mode=args[0]; Descriptor.Need(mode=="--check" || mode=="--check-once-descriptor" || mode=="--check-live" || mode=="--show" || mode=="--show-once" || mode=="--cancel","unsupported_mode");
            Dictionary<string,string> options=new Dictionary<string,string>();
            for(int i=1;i<args.Length;i+=2) { Descriptor.Need(i+1<args.Length && (args[i]=="--descriptor" || args[i]=="--output" || args[i]=="--backend-binding") && !options.ContainsKey(args[i]),"invalid_arguments"); options.Add(args[i],args[i+1]); }
            string runId=Guid.NewGuid().ToString("N");
            if(mode=="--cancel") {
                Descriptor.Need(options.Count==1 && options.ContainsKey("--output"),"cancel_requires_only_output");
                Records.Write(Records.OutputPath(options["--output"],null),Records.State("cancelled",runId,null,null,"explicit_cancel"));
                Print(new {status="cancelled",authorization_granted=false,usable=false,ui_shown=false,pixels_read=false}); return 0;
            }
            bool once=mode=="--show-once",show=mode=="--show" || once;
            Descriptor.Need(options.ContainsKey("--descriptor") && options.Count==(once?3:(show?2:1)) && (!show || options.ContainsKey("--output")) && (!once || options.ContainsKey("--backend-binding")) && (once || !options.ContainsKey("--backend-binding")),"invalid_arguments");
            Descriptor d=Descriptor.Load(options["--descriptor"],DateTimeOffset.UtcNow,once || mode=="--check-once-descriptor");
            if(mode=="--check") { Print(new {status="descriptor_valid_unconfirmed",authorization_granted=false,usable=false,ui_shown=false,pixels_read=false}); return 0; }
            if(mode=="--check-once-descriptor") { Print(new {status="once_preview_descriptor_valid_backend_required",requires_backend_technical_review=true,primary_gate_incomplete=d.RequiresBackendTechnicalReview,authorization_granted=false,usable=false,ui_shown=false,pixels_read=false}); return 0; }
            Native.EnterPhysicalPixels(); d.VerifyLive(true);
            if(mode=="--check-live") { Print(new {status="live_metadata_valid_unconfirmed",authorization_granted=false,usable=false,ui_shown=false,pixels_read=false}); return 0; }
            string output=once?Descriptor.FullPath(options["--output"]):Records.OutputPath(options["--output"],d);
            if(once) {
                Descriptor.Need(output.EndsWith(".once-audit.json",StringComparison.OrdinalIgnoreCase),"once_audit_extension_required");
                foreach(string input in new string[]{d.FilePath,d.Scene.Path,d.Wgc.Path,options["--backend-binding"],Descriptor.ExpectedImage}) Descriptor.Need(!String.Equals(Descriptor.FullPath(input),output,StringComparison.OrdinalIgnoreCase),"output_must_not_replace_input");
            }
            BackendBridge backend=null;
            try {
                if(once) { backend=new BackendBridge(options["--backend-binding"]); backend.ProtectAuditOutput(output); backend.Probe(d); }
                else Records.Write(output,Records.State("unconfirmed",runId,d,null,"awaiting_manual_semantic_confirmation"));
                Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                using(CalibrationPanel panel=new CalibrationPanel(d,output,runId,backend)) Application.Run(panel);
            } finally { if(backend!=null)backend.Dispose(); }
            return 0;
        } catch(Exception e) {
            CalibrationFailure f=e as CalibrationFailure;
            Print(new {status="rejected",reason=f==null?"invalid_metadata_or_unavailable_target":f.Code,authorization_granted=false,usable=false,ui_shown=false,pixels_read=false,ocr_started=false}); return 3;
        }
    }
}
