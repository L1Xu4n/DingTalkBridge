// Read-only, out-of-process UI Automation observer. No injection, memory reads,
// input simulation, UIA Invoke/Value writes, or DingTalk database access.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using Accessibility;

internal static class DingTalkUia
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static readonly object OutputLock = new object();
    private static readonly ManualResetEvent Stop = new ManualResetEvent(false);
    private static readonly AutoResetEvent Dirty = new AutoResetEvent(false);
    private static readonly Dictionary<long, Subscription> Subscriptions = new Dictionary<long, Subscription>();
    private static string ExpectedPath;
    private static int RequestedPid;
    private static int MaxNodes = 1500;
    private static int MaxTexts = 300;
    private static string StopFile;
    private static long Sequence;
    private static long EventNotifications;
    private static bool Diagnostics;

    private delegate bool EnumWindowProc(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder name, int count);
    [DllImport("user32.dll")] private static extern IntPtr GetThreadDesktop(uint threadId);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder information, int length, out int needed);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder filename, ref uint length);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] private static extern IntPtr GetThreadDpiAwarenessContext();
    [DllImport("user32.dll")] private static extern int GetAwarenessFromDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] private static extern bool AreDpiAwarenessContextsEqual(IntPtr first, IntPtr second);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out NativeRect rect);
    [DllImport("user32.dll")] private static extern bool GetClientRect(IntPtr window, out NativeRect rect);
    [DllImport("user32.dll")] private static extern bool ClientToScreen(IntPtr window, ref NativePoint point);
    [DllImport("user32.dll")] private static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] private static extern IntPtr GetParent(IntPtr window);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] private static extern IntPtr GetWindowLongPtr(IntPtr window,int index);
    [DllImport("user32.dll")] private static extern int GetWindowRgn(IntPtr window,IntPtr region);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateRectRgn(int left,int top,int right,int bottom);
    [DllImport("gdi32.dll")] private static extern int GetRgnBox(IntPtr region,out NativeRect rect);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr value);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool GetWindowDisplayAffinity(IntPtr window, out uint affinity);
    [DllImport("dwmapi.dll", EntryPoint = "DwmGetWindowAttribute")] private static extern int DwmRect(IntPtr window, uint attribute, out NativeRect rect, int size);
    [DllImport("dwmapi.dll", EntryPoint = "DwmGetWindowAttribute")] private static extern int DwmInt(IntPtr window, uint attribute, out int value, int size);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr window);
    [DllImport("oleacc.dll")] private static extern int AccessibleObjectFromWindow(IntPtr window, uint objectId, ref Guid interfaceId, [MarshalAs(UnmanagedType.Interface)] out object accessible);
    [DllImport("oleacc.dll")] private static extern int AccessibleChildren(IAccessible container, int start, int count, [Out, MarshalAs(UnmanagedType.LPArray, SizeParamIndex = 2)] object[] children, out int obtained);
    [DllImport("oleacc.dll")] private static extern int WindowFromAccessibleObject(IAccessible accessible, out IntPtr window);

    private sealed class Window
    {
        public IntPtr Handle;
        public int Pid;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect
    {
        public int Left, Top, Right, Bottom;
        public int Width { get { return Right - Left; } }
        public int Height { get { return Bottom - Top; } }
        public bool Valid { get { return Width > 0 && Height > 0; } }
        public Dictionary<string, int> Json { get { return new Dictionary<string, int> { {"x", Left}, {"y", Top}, {"width", Width}, {"height", Height} }; } }
        public bool Contains(NativeRect other) { return other.Left >= Left && other.Top >= Top && other.Right <= Right && other.Bottom <= Bottom; }
        public bool Intersects(NativeRect other) { return Left < other.Right && Right > other.Left && Top < other.Bottom && Bottom > other.Top; }
    }
    [StructLayout(LayoutKind.Sequential)] private struct NativePoint { public int X,Y; }
    private sealed class Subscription
    {
        public AutomationElement Element;
        public StructureChangedEventHandler Structure;
        public AutomationPropertyChangedEventHandler Property;
        public AutomationEventHandler Text;
    }
    private sealed class CaptureStats
    {
        public int Visited, Excluded, Errors;
        public bool Truncated;
        public int TextCharacters;
        public int TextCharacterLimit = 64000;
        public readonly List<object> Texts = new List<object>();
        public readonly Dictionary<string, int> ControlTypes = new Dictionary<string, int>();
        public readonly Dictionary<string, int> Frameworks = new Dictionary<string, int>();
        public readonly Dictionary<string, int> Classes = new Dictionary<string, int>();
        public readonly Dictionary<string, int> ExclusionReasons = new Dictionary<string, int>();
        public readonly Dictionary<string, int> Regions = new Dictionary<string, int>();
        public Dictionary<string, object> RootProfile;
        public readonly HashSet<string> Seen = new HashSet<string>();
    }

    [MTAThread]
    private static int Main(string[] args)
    {
        // This changes only the observer's coordinate interpretation, never
        // system settings or the target process. Geometry fails closed unless
        // per-monitor awareness is confirmed.
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            Dictionary<string, string> options = Parse(args);
            string mode = Get(options, "mode", "probe");
            if (mode != "probe" && mode != "snapshot" && mode != "watch" && mode != "regions" && mode != "scene") throw new ArgumentException("unsupported mode");
            ExpectedPath = Path.GetFullPath(Get(options, "process-path", ""));
            if (!String.Equals(Path.GetFileName(ExpectedPath), "DingTalk.exe", StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("process-path must name DingTalk.exe");
            RequestedPid = Integer(options, "pid", 0, 0, Int32.MaxValue);
            MaxNodes = Integer(options, "max-nodes", 1500, 20, 10000);
            MaxTexts = Integer(options, "max-texts", 300, 1, 2000);
            Diagnostics = Integer(options, "diagnostics", 0, 0, 1) == 1;
            double interval = Number(options, "interval", 5, 1, 3600);
            double duration = Number(options, "duration", 0, 0, 86400 * 365);
            StopFile = Get(options, "stop-file", null);
            Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; Stop.Set(); };
            if (mode == "regions") { Emit(FindChatRegions()); return 0; }
            if (mode == "scene") { Emit(FindToplevelScene()); return 0; }
            if (mode != "watch") { Capture(mode == "snapshot", "requested", false); return 0; }
            DateTime started = DateTime.UtcNow;
            DateTime nextPeriodic = DateTime.MinValue;
            DateTime lastCapture = DateTime.MinValue;
            while (!Stop.WaitOne(0))
            {
                if (StopFile != null && File.Exists(StopFile)) break;
                if (duration > 0 && (DateTime.UtcNow - started).TotalSeconds >= duration) break;
                DateTime now = DateTime.UtcNow;
                bool periodic = now >= nextPeriodic;
                bool changed = Dirty.WaitOne(0);
                if (periodic || (changed && (now - lastCapture).TotalMilliseconds >= 750))
                {
                    Capture(true, periodic ? "periodic" : "uia_event", true);
                    lastCapture = DateTime.UtcNow;
                    if (periodic) nextPeriodic = lastCapture.AddSeconds(interval);
                }
                else if (changed) Dirty.Set();
                Stop.WaitOne(100);
            }
            UnsubscribeAll();
            Emit(new Dictionary<string, object> { {"type", "status"}, {"status", "stopped"}, {"observed_at", Stamp()}, {"message_count", 0}, {"scope", "current_visible_windows"} });
            return 0;
        }
        catch (Exception e)
        {
            // Errors deliberately omit exception messages: UIA providers may put
            // user-visible text in them.
            Emit(new Dictionary<string, object> { {"type", "error"}, {"status", "collector_error"}, {"error_code", e.GetType().Name}, {"observed_at", Stamp()} });
            UnsubscribeAll();
            return 2;
        }
    }

    private static void Capture(bool includeText, string reason, bool subscribe)
    {
        HashSet<int> pids = ValidPids();
        string desktop = DesktopName();
        string desktopKind = String.Equals(desktop, "Default", StringComparison.OrdinalIgnoreCase) ? "interactive_default" : (desktop.StartsWith("CodexSandboxDesktop-", StringComparison.OrdinalIgnoreCase) ? "isolated_sandbox" : "other");
        int hiddenWindows;
        Dictionary<string, int> classes;
        List<Window> windows = VisibleWindows(pids, out hiddenWindows, out classes);
        List<object> sessions = new List<object>();
        foreach (int pid in pids)
        {
            try { using (Process process = Process.GetProcessById(pid)) sessions.Add(new Dictionary<string, object> { {"pid", pid}, {"session_id", process.SessionId} }); }
            catch (Exception) { }
        }
        if (subscribe) Rebind(windows);
        List<object> evidence = new List<object>();
        List<object> windowInfo = new List<object>();
        int visited = 0, excluded = 0, errors = 0;
        bool truncated = false;
        int remainingTextCharacters = 128000;
        foreach (Window window in windows)
        {
            CaptureStats stats = new CaptureStats { TextCharacterLimit = Math.Min(64000, remainingTextCharacters) };
            try
            {
                if (!ValidWindow(window)) continue;
                AutomationElement root = AutomationElement.FromHandle(window.Handle);
                if (root == null || root.Current.ProcessId != window.Pid) continue;
                if (Diagnostics)
                {
                    AutomationElement.AutomationElementInformation info = root.Current;
                    stats.RootProfile = new Dictionary<string, object> { {"is_offscreen", info.IsOffscreen}, {"is_password", info.IsPassword}, {"input_reason", InputReason(root, info, 0)}, {"has_raw_child", TreeWalker.RawViewWalker.GetFirstChild(root) != null} };
                }
                Walk(root, window.Pid, stats, includeText, 0);
            }
            catch (Exception) { stats.Errors++; }
            visited += stats.Visited; excluded += stats.Excluded; errors += stats.Errors; truncated |= stats.Truncated;
            remainingTextCharacters -= stats.TextCharacters;
            string windowId = window.Handle.ToInt64().ToString("x");
            Dictionary<string, object> windowRecord = new Dictionary<string, object> { {"pid", window.Pid}, {"window_id", windowId}, {"visited_nodes", stats.Visited}, {"safe_text_count", stats.Seen.Count}, {"evidence_region_counts", stats.Regions}, {"excluded_nodes", stats.Excluded}, {"exclusion_reasons", stats.ExclusionReasons}, {"control_types", stats.ControlTypes}, {"framework_ids", stats.Frameworks}, {"class_names", stats.Classes}, {"truncated", stats.Truncated} };
            if (Diagnostics) windowRecord["root_profile"] = stats.RootProfile;
            if (Diagnostics) windowRecord["accessibility_diagnostics"] = InspectNativeAndMsaa(window);
            windowInfo.Add(windowRecord);
            if (includeText && stats.Texts.Count > 0)
                evidence.Add(new Dictionary<string, object> { {"pid", window.Pid}, {"window_id", windowId}, {"texts", stats.Texts} });
        }
        Emit(new Dictionary<string, object> {
            {"schema_version", 1}, {"type", includeText ? "snapshot" : "probe"}, {"sequence", ++Sequence},
            {"observed_at", Stamp()}, {"source", "windows_uia"}, {"reason", reason},
            {"status", desktopKind == "isolated_sandbox" ? "isolated_desktop" : (pids.Count == 0 ? "process_not_running" : (windows.Count == 0 ? "no_visible_window" : "available"))},
            {"scope", "current_visible_windows"}, {"coverage", "visible_accessibility_text_only"},
            {"privacy_rule", "noneditable_visible_raw_leaf_names_only"},
            {"history_available", false}, {"message_count", 0}, {"identity_resolution", "unavailable"},
            {"process_ids", new List<int>(pids)}, {"windows", windowInfo}, {"evidence", evidence},
            {"bridge_session_id", Process.GetCurrentProcess().SessionId}, {"process_sessions", sessions}, {"desktop_name", desktop}, {"desktop_kind", desktopKind},
            {"target_window_count", windows.Count + hiddenWindows}, {"hidden_window_count", hiddenWindows}, {"window_class_counts", classes},
            {"visited_nodes", visited}, {"excluded_nodes", excluded}, {"provider_errors", errors}, {"truncated", truncated},
            {"event_subscriptions", Subscriptions.Count}, {"event_notifications", Interlocked.Read(ref EventNotifications)},
            {"limits", new Dictionary<string, object> { {"max_nodes_per_window", MaxNodes}, {"max_texts_per_window", MaxTexts}, {"max_text_characters_per_window", 64000}, {"max_text_characters_per_snapshot", 128000} }}
        });
    }

    private static void Walk(AutomationElement element, int pid, CaptureStats stats, bool includeText, int depth, string region = "window")
    {
        if (stats.Visited >= MaxNodes || stats.Seen.Count >= MaxTexts || stats.TextCharacters >= stats.TextCharacterLimit || depth > 60) { stats.Truncated = true; return; }
        try
        {
            AutomationElement.AutomationElementInformation info = element.Current;
            if (info.ProcessId != pid || !ValidPid(pid)) { stats.Excluded++; return; }
            stats.Visited++;
            string type = info.ControlType.ProgrammaticName.Replace("ControlType.", "");
            if (!stats.ControlTypes.ContainsKey(type)) stats.ControlTypes[type] = 0;
            stats.ControlTypes[type]++;
            Count(stats.Frameworks, info.FrameworkId ?? "");
            Count(stats.Classes, info.ClassName ?? "");
            if (info.ClassName == "im_chat::DTIMChatComponent") region = "chat_component";
            else if (info.ClassName == "ConvListView") region = "conversation_list";
            else if (info.ClassName == "ddesign::TopWindowToolBar") region = "window_toolbar";
            // Skip an entire editable subtree, including child text and labels.
            // Values and TextPattern.DocumentRange are never read.
            // Some Qt top-level providers report IsOffscreen=true for a
            // visible native host while exposing visible children. The caller
            // has independently verified that exact HWND is visible. Traverse
            // that host without reading its name; descendants keep their own
            // offscreen checks and editable-subtree exclusions.
            string excludedReason = info.IsPassword ? "password" : (info.IsOffscreen && depth > 0 ? "offscreen" : InputReason(element, info, depth));
            if (excludedReason != null) { stats.Excluded++; Count(stats.ExclusionReasons, excludedReason); return; }
            // Raw view preserves editable ancestors omitted from ControlView.
            // Parent Name can aggregate password/draft descendants, so only
            // leaf names are ever read, even for ListItem/DataItem/Text.
            AutomationElement child = TreeWalker.RawViewWalker.GetFirstChild(element);
            if (!info.IsOffscreen && child == null && IsEvidenceType(info.ControlType))
            {
                string name = info.Name;
                if (!String.IsNullOrWhiteSpace(name))
                {
                    name = name.Trim();
                    if (name.Length > 4000) { name = name.Substring(0, 4000); stats.Truncated = true; }
                    if (name.Length > stats.TextCharacterLimit - stats.TextCharacters) { name = name.Substring(0, stats.TextCharacterLimit - stats.TextCharacters); stats.Truncated = true; }
                    string key = type + "\n" + name;
                    if (stats.Seen.Add(key))
                    {
                        stats.TextCharacters += name.Length;
                        Count(stats.Regions, region);
                        if (includeText) stats.Texts.Add(new Dictionary<string, object> { {"text", name}, {"control_type", type}, {"depth", depth}, {"region", region}, {"class_name", info.ClassName} });
                    }
                }
            }
            while (child != null)
            {
                Walk(child, pid, stats, includeText, depth + 1, region);
                if (stats.Visited >= MaxNodes || stats.Seen.Count >= MaxTexts || stats.TextCharacters >= stats.TextCharacterLimit) { stats.Truncated = true; break; }
                child = TreeWalker.RawViewWalker.GetNextSibling(child);
            }
        }
        catch (ElementNotAvailableException) { stats.Errors++; }
        catch (InvalidOperationException) { stats.Errors++; }
        catch (COMException) { stats.Errors++; }
        catch (UnauthorizedAccessException) { stats.Errors++; }
    }

    private static string InputReason(AutomationElement element, AutomationElement.AutomationElementInformation info, int depth)
    {
        if (info.ControlType == ControlType.Edit || info.ControlType == ControlType.ComboBox) return "edit_control";
        string id = (info.AutomationId ?? "").ToLowerInvariant();
        string cls = (info.ClassName ?? "").ToLowerInvariant();
        string[] markers = { "password", "passwd", "search", "compose", "composer", "editor", "richedit", "input", "draft" };
        foreach (string marker in markers) if (id.Contains(marker) || cls.Contains(marker)) return "input_marker";
        object pattern;
        // The live Qt main host incorrectly exposes writable ValuePattern.
        // Only the independently verified visible top-level Window is exempt.
        // Generic writable Pane/Group/Custom controls remain excluded: a custom
        // rich-text editor may expose draft text through descendant Text nodes.
        bool verifiedTopLevelHost = depth == 0 && info.ControlType == ControlType.Window;
        if (!verifiedTopLevelHost && element.TryGetCurrentPattern(ValuePattern.Pattern, out pattern))
        {
            try { if (!((ValuePattern)pattern).Current.IsReadOnly) return "writable_value_pattern"; }
            catch (Exception) { return "unknown_value_pattern"; }
        }
        // Document controls may be editors even without a ValuePattern. Their
        // accessible descendants are not safe to classify without app knowledge.
        if (info.ControlType == ControlType.Document) return "document_control";
        return null;
    }

    private static bool IsEvidenceType(ControlType type)
    {
        return type == ControlType.Text || type == ControlType.ListItem || type == ControlType.DataItem || type == ControlType.Hyperlink || type == ControlType.HeaderItem;
    }

    private static void Count(Dictionary<string, int> counts, string key)
    {
        if (!counts.ContainsKey(key)) counts[key] = 0;
        counts[key]++;
    }

    // Metadata only. This does not initialize WGC, acquire a texture, capture
    // pixels, map a resource, or run OCR. A whole-top scene must re-mask the
    // complete composed tree and must not inherit the CEF-only overlay rule.
    private static object FindToplevelScene()
    {
        Dictionary<string, object> selection = FindChatRegions() as Dictionary<string, object>;
        Dictionary<string, object> report = new Dictionary<string, object> {
            {"type", "scene"}, {"status", "scene_incomplete"}, {"capture_permitted", false},
            {"observed_at", Stamp()}, {"metadata_only", true}, {"text_properties_read", false},
            {"capture_backend_policy", "toplevel_wgc_gpu_crop_and_redact_before_cpu_only"},
            {"coordinate_space", "physical_screen_pixels"}, {"dpi_awareness", "per_monitor_v2"}
        };
        if (selection == null) return report;
        report["desktop_kind"] = selection.ContainsKey("desktop_kind") ? selection["desktop_kind"] : "unknown";
        List<object> seeds = selection.ContainsKey("regions") ? selection["regions"] as List<object> : null;
        if ((string)selection["status"] != "available" || seeds == null || seeds.Count != 1) { report["reason"] = "no_unambiguous_verified_cef_selection"; return report; }
        Dictionary<string, object> seed = seeds[0] as Dictionary<string, object>;
        int pid = Convert.ToInt32(seed["pid"]);
        IntPtr message = new IntPtr(Convert.ToInt64((string)seed["message_hwnd"], 16));
        IntPtr semantic = new IntPtr(Convert.ToInt64((string)seed["hwnd"], 16));
        IntPtr top = GetAncestor(message, 2); uint topPid;
        GetWindowThreadProcessId(top, out topPid);
        if (top == IntPtr.Zero || topPid != pid || !ValidPid(pid) || !IsWindowVisible(top) || IsIconic(top) || !IsChild(top, message) || !IsChild(top, semantic) || !SameSession(pid, pid)) { report["reason"] = "invalid_toplevel_source_relationship"; return report; }
        NativeRect topRect, frame;
        int cloaked; uint affinity;
        bool windowRectOk = GetWindowRect(top, out topRect) && topRect.Valid;
        int frameHr = DwmRect(top, 9, out frame, Marshal.SizeOf(typeof(NativeRect)));
        int cloakHr = DwmInt(top, 14, out cloaked, sizeof(int));
        bool affinityOk = GetWindowDisplayAffinity(top, out affinity);
        NativeRect roi = RectFromJson(seed["message_rect_screen"]);
        Dictionary<string, object> topInfo = new Dictionary<string, object> {
            {"hwnd", top.ToInt64().ToString("x")}, {"pid", pid}, {"native_class", NativeClass(top)},
            {"verified_process_path", ExpectedPath}, {"session_id", Process.GetProcessById(pid).SessionId},
            {"visible", IsWindowVisible(top)}, {"iconic", IsIconic(top)}, {"window_rect_screen", topRect.Json},
            {"dwm_frame_rect_screen", frame.Json}, {"dwm_frame_hresult", frameHr}, {"dwm_cloak_hresult", cloakHr}, {"dwm_cloaked", cloaked},
            {"display_affinity_read_ok", affinityOk}, {"display_affinity", affinity}
        };
        AutomationElement root = AutomationElement.FromHandle(top);
        AutomationElement.AutomationElementInformation rootInfo = root.Current;
        topInfo["uia_class"] = rootInfo.ClassName; topInfo["framework_id"] = rootInfo.FrameworkId; topInfo["control_type"] = rootInfo.ControlType.ProgrammaticName;
        Dictionary<string, object> rootValue = new Dictionary<string, object> { {"supported", false}, {"is_read_only", null}, {"read_error", false} };
        object rootPattern;
        try { if (root.TryGetCurrentPattern(ValuePattern.Pattern, out rootPattern)) { rootValue["supported"] = true; rootValue["is_read_only"] = ((ValuePattern)rootPattern).Current.IsReadOnly; } }
        catch (Exception e) { rootValue["read_error"] = true; rootValue["error_code"] = e.GetType().Name; }
        topInfo["root_value_pattern"] = rootValue;
        List<NativeRect> inputs = new List<NativeRect>(); List<object> evidence = new List<object>();
        int scanned = 0, errors = 0;
        FindUiaInputRects(root, inputs, evidence, pid, top, top, top, true, ref scanned, ref errors, 0);
        int uiaNodes = scanned;
        Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71"); object accessible;
        if (AccessibleObjectFromWindow(top, unchecked((uint)-4), ref iid, out accessible) >= 0 && accessible is IAccessible)
            FindMsaaInputRects((IAccessible)accessible, 0, pid, top, inputs, evidence, ref scanned, ref errors, 0, true);
        else errors++;
        int nativeCount = 0, nativeErrors = 0;
        HashSet<long> selectedOwners = new HashSet<long>();
        foreach (object value in (List<object>)seed["source_native_owner_proof"])
        {
            Dictionary<string, object> item = value as Dictionary<string, object>;
            selectedOwners.Add(Convert.ToInt64((string)item["native_owner_hwnd"], 16));
        }
        EnumChildWindows(top, delegate(IntPtr child, IntPtr unused)
        {
            if (++nativeCount > 512) { nativeErrors++; GeometryError(evidence,"native","semantic_scan_incomplete","native_child_budget",false); return false; }
            if (!IsWindowVisible(child)) return true;
            uint childPid; GetWindowThreadProcessId(child, out childPid);
            bool known = IsWindow(child) && IsChild(top, child) && ImagePathMatches((int)childPid) && SameSession((int)childPid, pid) && (childPid == pid || selectedOwners.Contains(child.ToInt64()));
            if (!known)
            {
                nativeErrors++;
                GeometryError(evidence,"native","owner_identity","native_child_identity_not_verified",true)["native_owner_hwnd"] = child.ToInt64().ToString("x");
                NativeRect unknown;
                if (GetWindowRect(child, out unknown) && unknown.Valid) { inputs.Add(unknown); evidence.Add(new Dictionary<string, object> { {"provider", "native"}, {"reason", "unknown_native_child"}, {"decision", "mask"}, {"native_owner_hwnd", child.ToInt64().ToString("x")}, {"owner_pid", childPid}, {"owner_class", NativeClass(child)}, {"rect", unknown.Json} }); }
            }
            return true;
        }, IntPtr.Zero);
        errors += nativeErrors;
        NativeRect freshTop, freshSemantic, freshMessage;
        if (!GetWindowRect(top, out freshTop) || !SameRect(topRect, freshTop) || !GetWindowRect(semantic, out freshSemantic) || !SameRect(RectFromJson(seed["window_rect_screen"]), freshSemantic) || !GetWindowRect(message, out freshMessage) || !SameRect(RectFromJson(seed["message_window_rect_screen"]), freshMessage)) { errors++; GeometryError(evidence,"scene","coordinates","selected_scene_bounds_changed_or_unreadable",true); }
        bool technicalBoundaryFailure = !windowRectOk || frameHr != 0 || !frame.Valid || cloakHr != 0 || cloaked != 0 || !affinityOk || affinity != 0 || !topRect.Contains(roi) || !frame.Contains(roi);
        if (technicalBoundaryFailure || (bool)rootValue["read_error"]) { errors++; GeometryError(evidence,"scene",technicalBoundaryFailure?"technical_boundary":"unknown_semantic",technicalBoundaryFailure?"window_dwm_affinity_or_roi_failure":"root_value_pattern_unreadable",technicalBoundaryFailure); }
        long roiArea = roi.Valid ? (long)roi.Width * roi.Height : 0;
        long masked = MaskUnionArea(inputs, roi);
        List<object> masks = new List<object>();
        foreach (object value in evidence)
        {
            Dictionary<string, object> item = value as Dictionary<string, object>;
            if (item == null || !item.ContainsKey("rect")) continue;
            bool isMask = ((string)item["provider"] == "msaa") || (item.ContainsKey("decision") && (string)item["decision"] == "mask");
            if (!isMask) continue;
            Dictionary<string, object> mask = new Dictionary<string, object>(item);
            mask["roi_intersection_area"] = IntersectionArea(RectFromJson(item["rect"]), roi);
            masks.Add(mask);
        }
        bool allMasked = roiArea > 0 && masked >= roiArea;
        report["status"] = allMasked ? "no_safe_unmasked_region" : (errors > 0 ? "scene_incomplete" : "requires_reviewed_gpu_backend");
        report["capture_permitted"] = false;
        report["reason"] = allMasked ? "unknown_or_writable_composed_scene_masks_cover_entire_roi" : "metadata_only_no_capture_authorized";
        report["candidate_scene"] = new Dictionary<string, object> {
            {"toplevel", topInfo}, {"selected_cef_hwnd", message.ToInt64().ToString("x")}, {"selected_semantic_hwnd", semantic.ToInt64().ToString("x")},
            {"selected_cef_rect_screen", seed["message_window_rect_screen"]}, {"selected_cef_native_class", seed["message_class"]},
            {"selected_semantic_rect_screen", seed["window_rect_screen"]}, {"selected_semantic_native_class", seed["native_host_class"]}, {"selected_semantic_uia_class", seed["host_class"]},
            {"message_roi_screen", roi.Json}, {"masks", masks}, {"input_geometry_evidence", evidence},
            {"mask_coverage", new Dictionary<string, object> { {"roi_area", roiArea}, {"masked_union_area", masked}, {"unmasked_area", Math.Max(0, roiArea-masked)}, {"masked_fraction", roiArea > 0 ? (double)masked/roiArea : 1.0} }},
            {"coverage", new Dictionary<string, object> { {"uia_nodes", uiaNodes}, {"msaa_nodes", scanned-uiaNodes}, {"native_child_nodes", nativeCount}, {"provider_errors", errors}, {"native_child_errors", nativeErrors}, {"complete", errors == 0}, {"node_budget", MaxNodes}, {"native_child_budget", 512} }},
            {"calibration_gate", CalibrationGate(evidence,errors,roi)},
            {"outside_source_tree_ignored_count", 0}, {"layout_whitelist_exemptions", 0}, {"root_window_value_exception_applied", false},
            {"unknown_native_child_policy", "mask_entire_child_and_reject_scene"}, {"gpu_redaction_before_cpu_required", true}
        };
        List<object> alternatives = new List<object>(); HashSet<long> errorOwners = new HashSet<long>();
        foreach(object value in evidence)
        {
            Dictionary<string,object> item=value as Dictionary<string,object>;
            if(item!=null && item.ContainsKey("geometry_error") && (bool)item["geometry_error"] && item.ContainsKey("native_owner_hwnd")) errorOwners.Add(Convert.ToInt64((string)item["native_owner_hwnd"],16));
        }
        foreach(long owner in errorOwners)
        {
            Dictionary<string,object> alternative=NativeOwnerAlternative(new IntPtr(owner),top,roi,pid) as Dictionary<string,object>;
            List<object> bindings=new List<object>(); List<string> refs=new List<string>();
            foreach(object value in evidence)
            {
                Dictionary<string,object> item=value as Dictionary<string,object>;
                if(item!=null&&item.ContainsKey("geometry_error")&&(bool)item["geometry_error"]&&item.ContainsKey("native_owner_hwnd")&&Convert.ToInt64((string)item["native_owner_hwnd"],16)==owner)
                {
                    bindings.Add(new Dictionary<string,object>(item)); refs.Add((string)item["error_ref"]);
                }
            }
            alternative["resolves_error_refs"]=refs; alternative["source_error_bindings"]=bindings; alternatives.Add(alternative);
        }
        List<object> strongProof=new List<object>(); bool strongComplete=true;
        foreach(object value in evidence)
        {
            Dictionary<string,object> item=value as Dictionary<string,object>;
            if(item==null) continue;
            string reason=item.ContainsKey("reason")?item["reason"] as string:null;
            bool strong=reason=="password"||reason=="edit_control"||reason=="input_marker";
            if((string)item["provider"]=="msaa"&&item.ContainsKey("role")) {int role=Convert.ToInt32(item["role"]); strong=strong||role==42||role==46||(item.ContainsKey("protected")&&(bool)item["protected"]);}
            if(!strong||!item.ContainsKey("rect")) continue;
            NativeRect box=RectFromJson(item["rect"]);
            IntPtr owner=item.ContainsKey("native_owner_hwnd")?new IntPtr(Convert.ToInt64((string)item["native_owner_hwnd"],16)):IntPtr.Zero;
            uint ownerPid; GetWindowThreadProcessId(owner,out ownerPid); NativeRect ownerRect;
            bool valid=box.Valid&&owner!=IntPtr.Zero&&IsWindow(owner)&&IsWindowVisible(owner)&&ownerPid==pid&&ImagePathMatches((int)ownerPid)&&SameSession((int)ownerPid,pid)&&(owner==top||IsChild(top,owner))&&GetWindowRect(owner,out ownerRect)&&ownerRect.Valid&&ownerRect.Contains(box);
            if(!valid) strongComplete=false;
            Dictionary<string,object> proof=new Dictionary<string,object>(item); proof["technical_bbox_owner_valid"]=valid; proof["roi_intersection_area"]=IntersectionArea(box,roi); strongProof.Add(proof);
        }
        NativeRect finalTop,finalSemantic,finalMessage; uint finalAffinity;
        bool alternativeFresh=GetWindowRect(top,out finalTop)&&SameRect(topRect,finalTop)&&GetWindowRect(semantic,out finalSemantic)&&SameRect(RectFromJson(seed["window_rect_screen"]),finalSemantic)&&GetWindowRect(message,out finalMessage)&&SameRect(RectFromJson(seed["message_window_rect_screen"]),finalMessage)&&GetWindowDisplayAffinity(top,out finalAffinity)&&finalAffinity==affinity&&IsWindowVisible(top)&&ValidPid(pid)&&SameSession(pid,pid);
        ((Dictionary<string,object>)report["candidate_scene"])["alternative_technical_evidence"] = new Dictionary<string,object>{{"metadata_only",true},{"not_applied_to_strict_masks_or_gate",true},{"fresh_binding_verified",alternativeFresh},{"owners",alternatives},{"known_strong_input_proof",strongProof},{"known_strong_input_geometry_complete",strongComplete},{"superset_padding_pixels",8},{"capture_authorized",false}};
        return report;
    }
    private static object NativeOwnerAlternative(IntPtr owner,IntPtr top,NativeRect roi,int pid)
    {
        uint actualPid; GetWindowThreadProcessId(owner,out actualPid);
        NativeRect window,frame; int cloak;
        bool gwrCall=GetWindowRect(owner,out window),gwr=gwrCall&&window.Valid;
        int dwmHr=DwmRect(owner,9,out frame,Marshal.SizeOf(typeof(NativeRect))), cloakHr=DwmInt(owner,14,out cloak,sizeof(int));
        long style=GetWindowLongPtr(owner,-16).ToInt64(),exStyle=GetWindowLongPtr(owner,-20).ToInt64();
        NativeRect superset=window;
        if(gwr&&dwmHr==0&&frame.Valid) superset=new NativeRect{Left=Math.Min(window.Left,frame.Left),Top=Math.Min(window.Top,frame.Top),Right=Math.Max(window.Right,frame.Right),Bottom=Math.Max(window.Bottom,frame.Bottom)};
        List<object> ancestors=new List<object>(); IntPtr current=owner; int depth=0;
        while(current!=IntPtr.Zero && depth++<32)
        {
            NativeRect rect,client; uint currentPid; GetWindowThreadProcessId(current,out currentPid); bool bounds=GetWindowRect(current,out rect)&&rect.Valid;
            bool clientCall=ClientScreenRectRaw(current,out client),clientValid=clientCall&&client.Valid; IntPtr parent=GetParent(current);
            long currentStyle=GetWindowLongPtr(current,-16).ToInt64();
            long currentExStyle=GetWindowLongPtr(current,-20).ToInt64();
            ancestors.Add(new Dictionary<string,object>{{"hwnd",current.ToInt64().ToString("x")},{"pid",currentPid},{"native_class",NativeClass(current)},{"is_window",IsWindow(current)},{"image_path_matches",ImagePathMatches((int)currentPid)},{"same_session",SameSession((int)currentPid,pid)},{"is_window_visible",IsWindowVisible(current)},{"ws_visible",(currentStyle&0x10000000)!=0},{"ws_child",(currentStyle&0x40000000)!=0},{"ws_ex_layered",(currentExStyle&0x80000)!=0},{"ws_ex_transparent",(currentExStyle&0x20)!=0},{"bounds_valid",bounds},{"window_rect_screen",rect.Json},{"client_call_succeeded",clientCall},{"client_bounds_valid",clientValid},{"client_bounds_nonnegative",clientCall&&client.Width>=0&&client.Height>=0},{"client_rect_screen",client.Json},{"parent_hwnd",parent.ToInt64().ToString("x")},{"parent_is_real_native_parent",parent!=IntPtr.Zero&&IsChild(parent,current)},{"selected_top_relation",current==top||IsChild(top,current)}});
            if(current==top) break;
            current=GetParent(current);
        }
        int regionType=0,regionBoxType=0; NativeRect regionBox=new NativeRect(); IntPtr region=CreateRectRgn(0,0,0,0);
        if(region!=IntPtr.Zero) { try { regionType=GetWindowRgn(owner,region); if(regionType!=0) regionBoxType=GetRgnBox(region,out regionBox); } finally {DeleteObject(region);} }
        const int padding=8;
        bool nativeBoundsKnown=gwrCall&&window.Width>=0&&window.Height>=0;
        NativeRect padded=new NativeRect{Left=superset.Left-padding,Top=superset.Top-padding,Right=superset.Right+padding,Bottom=superset.Bottom+padding};
        bool clipChain=current==top;
        foreach(object value in ancestors)
        {
            Dictionary<string,object> item=(Dictionary<string,object>)value;
            bool atTop=(string)item["hwnd"]==top.ToInt64().ToString("x");
            if(!(bool)item["is_window"]||!(bool)item["image_path_matches"]||!(bool)item["same_session"]||!(bool)item["selected_top_relation"]||!(bool)item["client_bounds_nonnegative"]||(bool)item["ws_ex_layered"]||(bool)item["ws_ex_transparent"]||(!atTop&&(!(bool)item["ws_child"]||!(bool)item["parent_is_real_native_parent"]))) clipChain=false;
        }
        return new Dictionary<string,object>{
            {"native_owner_hwnd",owner.ToInt64().ToString("x")},{"owner_pid",actualPid},{"selected_pid_matches",actualPid==pid},{"image_path_matches",ImagePathMatches((int)actualPid)},{"same_session",SameSession((int)actualPid,pid)},
            {"is_window",IsWindow(owner)},{"is_window_visible",IsWindowVisible(owner)},{"is_top_or_child",owner==top||IsChild(top,owner)},{"native_class",NativeClass(owner)},
            {"style_hex",style.ToString("x")},{"ex_style_hex",exStyle.ToString("x")},{"ws_visible",(style&0x10000000)!=0},{"ws_child",(style&0x40000000)!=0},{"ws_clipchildren",(style&0x02000000)!=0},{"ws_clipsiblings",(style&0x04000000)!=0},{"ws_ex_layered",(exStyle&0x80000)!=0},{"ws_ex_transparent",(exStyle&0x20)!=0},
            {"gwr_call_succeeded",gwrCall},{"gwr_valid",gwr},{"native_bounds_nonnegative",nativeBoundsKnown},{"proven_empty_native_bounds",nativeBoundsKnown&&(window.Width==0||window.Height==0)},{"window_rect_screen",window.Json},{"dwm_hresult",dwmHr},{"dwm_frame_rect_screen",frame.Json},{"dwm_cloak_hresult",cloakHr},{"dwm_cloaked",cloak},
            {"window_roi_intersection_area",gwr?IntersectionArea(window,roi):-1},{"candidate_superset_screen",superset.Json},{"candidate_superset_roi_intersection_area",gwr?IntersectionArea(superset,roi):-1},
            {"superset_padding_pixels",padding},{"padded_superset_screen",padded.Json},{"padded_superset_roi_intersection_area",nativeBoundsKnown?IntersectionArea(padded,roi):-1},{"native_parent_client_clip_chain_complete",clipChain},
            {"rgn_result",regionType},{"rgn_box_result",regionBoxType},{"rgn_box_window_relative",regionBox.Json},{"rgn_used_to_shrink_mask",false},
            {"ancestor_chain",ancestors},{"ancestor_chain_reached_selected_top",current==top},{"readonly_semantics_inferred",false},{"applied_as_override",false}
        };
    }
    private static bool ClientScreenRectRaw(IntPtr window,out NativeRect rect)
    {
        rect=new NativeRect(); NativeRect local;
        if(!GetClientRect(window,out local)) return false;
        NativePoint first=new NativePoint{X=local.Left,Y=local.Top},last=new NativePoint{X=local.Right,Y=local.Bottom};
        if(!ClientToScreen(window,ref first)||!ClientToScreen(window,ref last)) return false;
        rect=new NativeRect{Left=first.X,Top=first.Y,Right=last.X,Bottom=last.Y}; return true;
    }
    private static object CalibrationGate(List<object> evidence,int providerErrors,NativeRect roi)
    {
        List<object> technical=new List<object>(), semantic=new List<object>(), strongRects=new List<object>();
        SortedSet<string> semanticCodes=new SortedSet<string>(); int classified=0;
        foreach(object value in evidence)
        {
            Dictionary<string,object> item=value as Dictionary<string,object>;
            if(item==null) continue;
            if(item.ContainsKey("geometry_error") && (bool)item["geometry_error"])
            {
                classified++;
                if((bool)item["technical_error"]) technical.Add(item);
                else { semantic.Add(item); semanticCodes.Add((string)item["error_category"]=="semantic_scan_incomplete"?"semantic_scan_incomplete":"unknown_semantic"); }
            }
            string reason=item.ContainsKey("reason")?item["reason"] as string:null;
            if(reason=="writable_value_pattern"||reason=="unknown_value_pattern"||reason=="document_control") semanticCodes.Add("unknown_semantic");
            bool strong=reason=="password"||reason=="edit_control"||reason=="input_marker";
            if((string)item["provider"]=="msaa" && item.ContainsKey("role")) { int role=Convert.ToInt32(item["role"]); strong=strong||role==42||role==46||(item.ContainsKey("protected")&&(bool)item["protected"]); }
            if(strong && item.ContainsKey("rect")) { NativeRect rect=RectFromJson(item["rect"]); if(rect.Valid) strongRects.Add(rect.Json); }
        }
        int unclassified=Math.Max(0,providerErrors-classified);
        if(unclassified>0) technical.Add(new Dictionary<string,object>{{"error_category","unknown_provider_error"},{"error_code","unclassified_provider_errors"},{"count",unclassified}});
        bool technicalComplete=technical.Count==0 && classified==providerErrors;
        return new Dictionary<string,object> {
            {"schema_version",1},{"technical_complete",technicalComplete},{"technical_errors",technical},
            {"known_strong_input_geometry_complete",technicalComplete},{"known_strong_input_rects_screen",strongRects},
            {"semantic_incomplete",semanticCodes.Count>0},{"semantic_errors",semantic},{"semantic_error_codes",new List<string>(semanticCodes)},
            {"classified_provider_error_count",classified},{"unclassified_provider_error_count",unclassified},
            {"strict_provider_errors",providerErrors},{"strict_masks_unchanged",true},{"capture_authorized",false}
        };
    }
    private static NativeRect RectFromJson(object value)
    {
        Dictionary<string, int> rect = value as Dictionary<string, int>;
        return new NativeRect { Left = rect["x"], Top = rect["y"], Right = rect["x"]+rect["width"], Bottom = rect["y"]+rect["height"] };
    }
    private static long IntersectionArea(NativeRect left, NativeRect right)
    {
        int width = Math.Max(0, Math.Min(left.Right, right.Right)-Math.Max(left.Left, right.Left));
        int height = Math.Max(0, Math.Min(left.Bottom, right.Bottom)-Math.Max(left.Top, right.Top));
        return (long)width*height;
    }
    private static long MaskUnionArea(List<NativeRect> masks, NativeRect roi)
    {
        if (!roi.Valid) return 0;
        List<NativeRect> clipped = new List<NativeRect>(); SortedSet<int> edges = new SortedSet<int>();
        foreach (NativeRect mask in masks)
        {
            if (!mask.Valid || !mask.Intersects(roi)) continue;
            if (mask.Contains(roi)) return (long)roi.Width*roi.Height;
            NativeRect rect = new NativeRect { Left = Math.Max(mask.Left,roi.Left), Top = Math.Max(mask.Top,roi.Top), Right = Math.Min(mask.Right,roi.Right), Bottom = Math.Min(mask.Bottom,roi.Bottom) };
            clipped.Add(rect); edges.Add(rect.Left); edges.Add(rect.Right);
        }
        List<int> xs = new List<int>(edges); long area = 0;
        for (int index=1; index<xs.Count; index++)
        {
            int x0=xs[index-1], x1=xs[index]; List<NativeRect> rows = new List<NativeRect>();
            foreach (NativeRect rect in clipped) if (rect.Left<x1 && rect.Right>x0) rows.Add(rect);
            rows.Sort(delegate(NativeRect a, NativeRect b) { return a.Top.CompareTo(b.Top); });
            int start=0,end=0; bool first=true; long height=0;
            foreach (NativeRect row in rows)
            {
                if (first) { start=row.Top; end=row.Bottom; first=false; }
                else if (row.Top<=end) end=Math.Max(end,row.Bottom);
                else { height+=end-start; start=row.Top; end=row.Bottom; }
            }
            if (!first) height+=end-start;
            area+=(long)(x1-x0)*height;
        }
        return area;
    }

    private static object FindChatRegions()
    {
        string desktop = DesktopName();
        string kind = String.Equals(desktop, "Default", StringComparison.OrdinalIgnoreCase) ? "interactive_default" : (desktop.StartsWith("CodexSandboxDesktop-", StringComparison.OrdinalIgnoreCase) ? "isolated_sandbox" : "other");
        List<object> regions = new List<object>();
        List<object> rejected = new List<object>();
        int awareness = -1;
        try { if (AreDpiAwarenessContextsEqual(GetThreadDpiAwarenessContext(), new IntPtr(-4))) awareness = GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext()); } catch (EntryPointNotFoundException) { }
        if (kind != "interactive_default" || awareness != 2)
            return new Dictionary<string, object> { {"type", "regions"}, {"status", kind == "isolated_sandbox" ? "isolated_desktop" : "unsafe_geometry"}, {"desktop_kind", kind}, {"coordinate_space", "unknown"}, {"regions", regions} };
        HashSet<int> pids = ValidPids(); int hidden; Dictionary<string, int> classes;
        List<Window> windows = VisibleWindows(pids, out hidden, out classes);
        foreach (Window top in windows)
        {
            EnumChildWindows(top.Handle, delegate(IntPtr handle, IntPtr parameter)
            {
                uint pid;
                GetWindowThreadProcessId(handle, out pid);
                if (!IsWindowVisible(handle) || !ValidPid((int)pid)) return true;
                try
                {
                    AutomationElement host = AutomationElement.FromHandle(handle);
                    AutomationElement.AutomationElementInformation info = host.Current;
                    if (info.FrameworkId != "Qt" || info.ClassName != "im_chat::DTIMChatComponent") return true;
                    NativeRect hostRect;
                    if (!GetWindowRect(handle, out hostRect) || !hostRect.Valid) return true;
                    List<NativeRect> inputs = new List<NativeRect>();
                    List<object> inputEvidence = new List<object>();
                    int scanned = 0, errors = 0;
                    List<NativeRect> composers = new List<NativeRect>();
                    List<object> candidates = new List<object>();
                    List<object> messageCandidates = new List<object>();
                    EnumChildWindows(top.Handle, delegate(IntPtr child, IntPtr unused)
                    {
                        uint childPid; GetWindowThreadProcessId(child, out childPid);
                        StringBuilder cls = new StringBuilder(256); GetClassName(child, cls, cls.Capacity);
                        NativeRect messageRect;
                        if (childPid != pid || !IsWindowVisible(child) || !ValidPid((int)childPid) || cls.ToString() != "CefBrowserWindow" || !GetWindowRect(child, out messageRect) || !messageRect.Valid) return true;
                        IntPtr common = GetParent(handle);
                        bool verifiedCommon = common != IntPtr.Zero && NativeClass(common) == "DingChatWnd" && IsChild(common, handle) && IsChild(common, child);
                        uint commonPid; GetWindowThreadProcessId(common, out commonPid);
                        verifiedCommon = verifiedCommon && commonPid == pid && ValidPid((int)commonPid) && IsWindowVisible(common);
                        messageCandidates.Add(new Dictionary<string, object> { {"class_name", cls.ToString()}, {"window_rect_screen", messageRect.Json}, {"contained_in_chat_host", hostRect.Contains(messageRect)}, {"native_child_of_chat_host", IsChild(handle, child)}, {"common_ancestor_class", NativeClass(common)}, {"common_ancestor_verified", verifiedCommon} });
                        if (!hostRect.Contains(messageRect) || !(IsChild(handle, child) || verifiedCommon)) return true;
                        inputs.Clear(); inputEvidence.Clear(); composers.Clear(); scanned = 0; errors = 0;
                        FindUiaInputRects(host, inputs, inputEvidence, (int)pid, child, top.Handle, handle, false, ref scanned, ref errors, 0);
                        int sourceBefore = scanned, sourceErrorsBefore = errors;
                        AutomationElement source = AutomationElement.FromHandle(child);
                        FindUiaInputRects(source, inputs, inputEvidence, (int)pid, child, top.Handle, child, true, ref scanned, ref errors, 0);
                        int sourceNodes = scanned - sourceBefore, sourceErrors = errors - sourceErrorsBefore;
                        Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71"); object accessible;
                        if (AccessibleObjectFromWindow(handle, unchecked((uint)-4), ref iid, out accessible) >= 0 && accessible is IAccessible)
                            FindMsaaInputRects((IAccessible)accessible, 0, (int)pid, handle, inputs, inputEvidence, ref scanned, ref errors, 0);
                        else errors++;
                        foreach (NativeRect rect in inputs)
                            if (rect.Valid && hostRect.Contains(rect) && rect.Width >= 100 && rect.Height >= 20 && rect.Top >= hostRect.Top + hostRect.Height / 2) composers.Add(rect);
                        int ignoredOutside = 0;
                        List<object> sourceOwners = new List<object>();
                        HashSet<int> sourcePids = new HashSet<int>();
                        foreach (object entry in inputEvidence)
                        {
                            Dictionary<string, object> item = entry as Dictionary<string, object>;
                            if (item != null && item.ContainsKey("decision") && (string)item["decision"] == "outside_source_tree") ignoredOutside++;
                            if (item != null && item.ContainsKey("decision") && (string)item["decision"] == "source_owner_verified") { sourceOwners.Add(item); sourcePids.Add(Convert.ToInt32(item["owner_pid"])); }
                        }
                        NativeRect crop = messageRect;
                        foreach (NativeRect editor in composers) crop.Bottom = Math.Min(crop.Bottom, editor.Top - 2);
                        bool safe = composers.Count > 0 && sourceNodes > 0 && sourceErrors == 0 && errors == 0 && crop.Width >= 100 && crop.Height >= 80 && messageRect.Contains(crop) && (IsChild(handle, child) || verifiedCommon);
                        foreach (NativeRect input in inputs) if (crop.Intersects(input)) safe = false;
                        NativeRect freshHost, freshMessage;
                        if (!GetWindowRect(handle, out freshHost) || !GetWindowRect(child, out freshMessage) || !SameRect(hostRect, freshHost) || !SameRect(messageRect, freshMessage)) { errors++; safe = false; }
                        if (!safe) return true;
                        List<object> exclusions = new List<object>();
                        foreach (NativeRect input in inputs) if (input.Valid) exclusions.Add(input.Json);
                        candidates.Add(new Dictionary<string, object> {
                            {"pid", pid}, {"hwnd", handle.ToInt64().ToString("x")}, {"host_class", "im_chat::DTIMChatComponent"},
                            {"native_host_class", NativeClass(handle)}, {"window_rect_screen", hostRect.Json},
                            {"common_ancestor_hwnd", common.ToInt64().ToString("x")}, {"common_ancestor_class", NativeClass(common)},
                            {"message_hwnd", child.ToInt64().ToString("x")}, {"message_class", cls.ToString()}, {"message_window_rect_screen", messageRect.Json}, {"message_rect_screen", crop.Json},
                            {"composer_exclusions_screen", exclusions}, {"composer_evidence_count", composers.Count}, {"input_exclusion_count", inputs.Count},
                            {"input_geometry_evidence", inputEvidence},
                            {"capture_source_policy", "cef_message_hwnd_printwindow_only"}, {"source_tree_scan_complete", true}, {"source_tree_scan_nodes", sourceNodes}, {"source_tree_provider_errors", sourceErrors},
                            {"outside_source_unknown_ignored_count", ignoredOutside}, {"native_owner_proof", "validated_hwnd_or_verified_raw_ancestor_in_selected_source_subtree"},
                            {"source_native_owner_proof", sourceOwners}, {"capture_source_process_ids", new List<int>(sourcePids)}, {"source_identity_scope", "selected_native_source_subtree"},
                            {"coordinate_space", "physical_screen_pixels"}, {"dpi_awareness", "per_monitor_v2"}, {"dpi", GetDpiForWindow(handle)},
                            {"verified_process_path", ExpectedPath}, {"geometry_evidence", "verified_qt_chat_host+cef_common_native_ancestor+confirmed_editor_boundary+no_edit_overlap"},
                            {"geometry_safe", true},
                            {"geometry_scan_nodes", scanned}, {"geometry_provider_errors", errors}
                        });
                        return true;
                    }, IntPtr.Zero);
                    if (candidates.Count == 1 && errors == 0) regions.Add(candidates[0]);
                    else rejected.Add(new Dictionary<string, object> { {"host_class", "im_chat::DTIMChatComponent"}, {"reason", candidates.Count == 0 ? "no_safe_message_region" : (errors > 0 ? "provider_errors" : "ambiguous_message_region")}, {"input_exclusion_count", inputs.Count}, {"composer_evidence_count", composers.Count}, {"geometry_provider_errors", errors}, {"window_rect_screen", hostRect.Json}, {"input_rectangles", RectsJson(inputs)}, {"input_geometry_evidence", inputEvidence}, {"message_candidates", messageCandidates} });
                }
                catch (Exception e) { rejected.Add(new Dictionary<string, object> { {"reason", "provider_error"}, {"error_code", e.GetType().Name} }); }
                return true;
            }, IntPtr.Zero);
        }
        return new Dictionary<string, object> { {"type", "regions"}, {"status", regions.Count > 0 ? "available" : "unsafe_geometry"}, {"observed_at", Stamp()}, {"desktop_kind", kind}, {"desktop_name", desktop}, {"coordinate_space", "physical_screen_pixels"}, {"dpi_awareness", "per_monitor_v2"}, {"regions", regions}, {"rejected", rejected} };
    }

    private static string NativeClass(IntPtr handle) { StringBuilder name = new StringBuilder(256); GetClassName(handle, name, name.Capacity); return name.ToString(); }
    private static bool SameRect(NativeRect left, NativeRect right) { return left.Left == right.Left && left.Top == right.Top && left.Right == right.Right && left.Bottom == right.Bottom; }
    private static List<object> RectsJson(List<NativeRect> rectangles) { List<object> result = new List<object>(); foreach (NativeRect rect in rectangles) result.Add(rect.Json); return result; }
    private static void FindUiaInputRects(AutomationElement element, List<NativeRect> inputs, List<object> evidence, int sourcePid, IntPtr sourceWindow, IntPtr topWindow, IntPtr inheritedOwner, bool sourceScan, ref int scanned, ref int errors, int depth)
    {
        if (scanned++ >= MaxNodes || depth > 60) { errors++; GeometryError(evidence,"uia","semantic_scan_incomplete","uia_node_or_depth_budget",false); return; }
        try
        {
            AutomationElement.AutomationElementInformation info = element.Current;
            IntPtr owner = info.NativeWindowHandle != 0 ? new IntPtr(info.NativeWindowHandle) : inheritedOwner;
            uint ownerPid;
            GetWindowThreadProcessId(owner, out ownerPid);
            bool ownerInSource = owner == sourceWindow || IsChild(sourceWindow, owner);
            bool renderer = IsNarrowRendererOwner(sourceScan, ownerInSource, NativeClass(owner), ImagePathMatches((int)ownerPid), SameSession((int)ownerPid, sourcePid), IsWindowVisible(owner));
            bool ownerValid = owner != IntPtr.Zero && IsWindow(owner) && IsWindowVisible(owner) && ((ownerPid == sourcePid && ValidPid((int)ownerPid)) || renderer) && (owner == topWindow || IsChild(topWindow, owner)) && (info.ProcessId == ownerPid || (info.ProcessId == 0 && info.NativeWindowHandle == 0));
            if (sourceScan && !ownerInSource) ownerValid = false;
            if (ownerValid && sourceScan && info.NativeWindowHandle != 0)
                evidence.Add(new Dictionary<string, object> { {"provider", "native_owner"}, {"decision", "source_owner_verified"}, {"native_owner_hwnd", owner.ToInt64().ToString("x")}, {"owner_pid", ownerPid}, {"owner_class", NativeClass(owner)}, {"renderer", renderer}, {"source_scan", true}, {"native_source_descendant", ownerInSource}, {"same_session", SameSession((int)ownerPid, sourcePid)}, {"verified_process_path", ExpectedPath}, {"owner_visible", true} });
            // A whole-top composed scene cannot inherit the text-walk host
            // exemption. Its root Window's actual ValuePattern is evaluated.
            int policyDepth = GeometryPolicyDepth(depth, sourceWindow == topWindow);
            string reason = info.IsPassword ? "password" : InputReason(element, info, policyDepth);
            bool strongInput = reason == "password" || reason == "edit_control" || reason == "input_marker";
            string decision = GeometryInputDecision(reason, strongInput, ownerValid, ownerInSource, !sourceScan && info.FrameworkId == "Qt");
            if (decision == "error") { errors++; Dictionary<string,object> detail=GeometryError(evidence,"uia","owner_identity","uia_owner_not_verified_in_scene",true); detail["decision"]="owner_error"; detail["owner_pid"]=ownerPid; detail["native_owner_hwnd"]=owner.ToInt64().ToString("x"); detail["class_name"]=info.ClassName; detail["framework_id"]=info.FrameworkId; detail["source_scan"]=sourceScan; return; }
            if (reason != null)
            {
                System.Windows.Rect rect = info.BoundingRectangle;
                NativeRect input = new NativeRect { Left = (int)Math.Floor(rect.Left), Top = (int)Math.Floor(rect.Top), Right = (int)Math.Ceiling(rect.Right), Bottom = (int)Math.Ceiling(rect.Bottom) };
                if (decision == "mask") { if (input.Valid) inputs.Add(input); else { errors++; Dictionary<string,object> detail=GeometryError(evidence,"uia","coordinates","invalid_input_bbox",true); detail["strong_input"]=strongInput; detail["diagnostic_bbox"]=input.Json; detail["input_reason"]=reason; detail["class_name"]=info.ClassName; detail["control_type"]=info.ControlType.ProgrammaticName; detail["framework_id"]=info.FrameworkId; detail["depth"]=depth; detail["is_offscreen"]=info.IsOffscreen; detail["native_owner_hwnd"]=owner.ToInt64().ToString("x"); detail["owner_pid"]=ownerPid; } }
                evidence.Add(new Dictionary<string, object> { {"provider", "uia"}, {"reason", reason}, {"decision", decision}, {"control_type", info.ControlType.ProgrammaticName}, {"class_name", info.ClassName}, {"framework_id", info.FrameworkId}, {"depth", depth}, {"rect", input.Json}, {"is_offscreen", info.IsOffscreen}, {"source_scan", sourceScan}, {"native_owner_hwnd", owner.ToInt64().ToString("x")}, {"owner_pid", ownerPid}, {"owner_evidence", info.NativeWindowHandle == 0 ? "verified_raw_ancestor" : "explicit_native_handle"} });
                if (strongInput) return;
            }
            AutomationElement child = TreeWalker.RawViewWalker.GetFirstChild(element);
            while (child != null) { FindUiaInputRects(child, inputs, evidence, sourcePid, sourceWindow, topWindow, owner, sourceScan, ref scanned, ref errors, depth + 1); child = TreeWalker.RawViewWalker.GetNextSibling(child); }
        }
        catch (Exception e) { errors++; Dictionary<string,object> detail=GeometryError(evidence,"uia","unknown_provider_error","uia_property_or_provider_exception",true); detail["error_code"]=e.GetType().Name; detail["hresult"]=e.HResult; }
    }
    private static string GeometryInputDecision(string reason, bool strongInput, bool ownerValid, bool ownerInSource, bool verifiedQtOverlay)
    {
        if (!ownerValid) return "error";
        if (reason == null) return "traverse";
        if (strongInput) return "mask";
        if (!ownerInSource && verifiedQtOverlay) return "outside_source_tree";
        return "mask";
    }
    private static int GeometryPolicyDepth(int depth, bool wholeTopComposedScene)
    {
        return wholeTopComposedScene ? Math.Max(1, depth) : depth;
    }
    private static bool IsNarrowRendererOwner(bool sourceScan, bool nativeSourceDescendant, string nativeClass, bool pathMatches, bool sameSession, bool visible)
    {
        return sourceScan && nativeSourceDescendant && nativeClass == "Intermediate D3D Window" && pathMatches && sameSession && visible;
    }
    private static bool SameSession(int pid, int sourcePid)
    {
        try
        {
            using (Process owner = Process.GetProcessById(pid))
            using (Process source = Process.GetProcessById(sourcePid))
            using (Process observer = Process.GetCurrentProcess())
                return owner.SessionId == source.SessionId && source.SessionId == observer.SessionId;
        }
        catch (Exception) { return false; }
    }
    private static void FindMsaaInputRects(IAccessible accessible, object childId, int pid, IntPtr chatHost, List<NativeRect> inputs, List<object> evidence, ref int scanned, ref int errors, int depth, bool sceneScan = false)
    {
        if (scanned++ >= MaxNodes || depth > 50) { errors++; GeometryError(evidence,"msaa","semantic_scan_incomplete","msaa_node_or_depth_budget",false); return; }
        try
        {
            IntPtr owner; uint ownerPid;
            if (!ValidPid(pid)) { errors++; GeometryError(evidence,"msaa","owner_identity","selected_process_invalid",true); return; }
            int ownerHr = WindowFromAccessibleObject(accessible, out owner);
            if (ownerHr < 0) { errors++; GeometryError(evidence,"msaa","owner_identity","accessible_owner_unavailable",true)["hresult"] = ownerHr; return; }
            GetWindowThreadProcessId(owner, out ownerPid);
            bool ownerPath = ValidPid((int)ownerPid), ownerWindow = IsWindow(owner), ownerVisible = IsWindowVisible(owner), ownerRelation = owner == chatHost || IsChild(chatHost, owner) || IsChild(owner, chatHost);
            if (ownerPid != pid || !ownerPath || !ownerWindow || !ownerVisible || !ownerRelation)
            {
                errors++;
                Dictionary<string,object> detail = GeometryError(evidence,"msaa","owner_identity","accessible_owner_not_in_verified_visible_scene",true);
                detail["native_owner_hwnd"] = owner.ToInt64().ToString("x"); detail["owner_pid"] = ownerPid;
                detail["pid_matches"] = ownerPid == pid; detail["path_matches"] = ownerPath; detail["is_window"] = ownerWindow; detail["owner_visible"] = ownerVisible; detail["native_relation_matches"] = ownerRelation;
                return;
            }
            object roleObject = accessible.get_accRole(childId), stateObject = accessible.get_accState(childId);
            if (!(roleObject is int) || !(stateObject is int))
            {
                errors++;
                Dictionary<string,object> detail = GeometryError(evidence,"msaa","unknown_semantic","noninteger_accessible_role_or_state",false);
                detail["role_is_integer"] = roleObject is int; detail["state_is_integer"] = stateObject is int;
                try { int x,y,w,h; accessible.accLocation(out x,out y,out w,out h,childId); NativeRect box = new NativeRect {Left=x,Top=y,Right=x+w,Bottom=y+h}; detail["diagnostic_bbox"] = box.Json; detail["bbox_valid"] = box.Valid; if(!box.Valid) { detail["technical_error"]=true; detail["error_category"]="coordinates"; } }
                catch(Exception e) { detail["bbox_valid"]=false; detail["technical_error"]=true; detail["error_category"]="coordinates"; detail["diagnostic_error_code"]=e.GetType().Name; }
                return;
            }
            int role = (int)roleObject, state = (int)stateObject;
            if (role == 42 || role == 46 || (state & 0x20000000) != 0 || (sceneScan && (role == 15 || role < 1 || role > 64)))
            {
                int left, top, width, height;
                accessible.accLocation(out left, out top, out width, out height, childId);
                NativeRect rect = new NativeRect { Left = left, Top = top, Right = left + width, Bottom = top + height };
                if (rect.Valid) inputs.Add(rect); else { errors++; Dictionary<string,object> detail=GeometryError(evidence,"msaa","coordinates","invalid_strong_or_unknown_input_bbox",true); detail["role"]=role; detail["strong_input"]=role==42||role==46||(state&0x20000000)!=0; detail["diagnostic_bbox"]=rect.Json; }
                if (sceneScan && (role < 1 || role > 64)) { errors++; GeometryError(evidence,"msaa","unknown_semantic","unrecognized_accessible_role",false); }
                evidence.Add(new Dictionary<string, object> { {"provider", "msaa"}, {"role", role}, {"protected", (state & 0x20000000) != 0}, {"depth", depth}, {"rect", rect.Json}, {"native_owner_hwnd", owner.ToInt64().ToString("x")}, {"owner_pid", ownerPid}, {"owner_class", NativeClass(owner)}, {"owner_evidence", "msaa_window_from_accessible_object"}, {"owner_visible", true}, {"source_scan", false} });
                return;
            }
            bool self = childId is int && (int)childId == 0;
            int total = self ? accessible.accChildCount : 0;
            if (total < 0 || total > 200 || total > MaxNodes - scanned) { errors++; Dictionary<string,object> detail=GeometryError(evidence,"msaa","semantic_scan_incomplete","msaa_child_batch_budget_or_invalid_count",false); detail["reported_children"]=total; detail["remaining_budget"]=MaxNodes-scanned; return; }
            int count = total;
            if (count <= 0) return;
            object[] children = new object[count]; int obtained;
            int childrenHr = AccessibleChildren(accessible, 0, count, children, out obtained);
            if (childrenHr < 0 || obtained != count) { errors++; Dictionary<string,object> detail=GeometryError(evidence,"msaa","semantic_scan_incomplete","partial_or_failed_accessible_children",false); detail["hresult"]=childrenHr; detail["requested_children"]=count; detail["obtained_children"]=obtained; return; }
            for (int index = 0; index < obtained; index++)
            {
                IAccessible child = children[index] as IAccessible;
                if (child != null) FindMsaaInputRects(child, 0, pid, chatHost, inputs, evidence, ref scanned, ref errors, depth + 1, sceneScan);
                else if (children[index] is int) FindMsaaInputRects(accessible, children[index], pid, chatHost, inputs, evidence, ref scanned, ref errors, depth + 1, sceneScan);
                else { errors++; GeometryError(evidence,"msaa","semantic_scan_incomplete","unknown_accessible_child_variant",false); }
            }
        }
        catch (Exception e) { errors++; Dictionary<string,object> detail=GeometryError(evidence,"msaa","unknown_provider_error","msaa_property_or_provider_exception",true); detail["error_code"]=e.GetType().Name; detail["hresult"]=e.HResult; }
    }

    private static Dictionary<string,object> GeometryError(List<object> evidence,string provider,string category,string code,bool technical)
    {
        Dictionary<string,object> detail=new Dictionary<string,object>{{"provider",provider},{"geometry_error",true},{"error_ref","geometry_error_"+evidence.Count.ToString()},{"error_category",category},{"error_code",code},{"technical_error",technical}};
        evidence.Add(detail); return detail;
    }

    // Opt-in structural diagnostics only. No IAccessible Name, Value,
    // Description, keyboard shortcut, selection, focus, or default action reads.
    private static Dictionary<string, object> InspectNativeAndMsaa(Window root)
    {
        Dictionary<string, int> childClasses = new Dictionary<string, int>();
        List<object> children = new List<object>();
        EnumChildWindows(root.Handle, delegate(IntPtr handle, IntPtr parameter)
        {
            uint pid;
            GetWindowThreadProcessId(handle, out pid);
            if (!IsWindowVisible(handle) || !ValidPid((int)pid)) return true;
            StringBuilder name = new StringBuilder(256);
            GetClassName(handle, name, name.Capacity);
            Count(childClasses, name.ToString());
            if (children.Count < 64)
            {
                Dictionary<string, object> item = new Dictionary<string, object> { {"class_name", name.ToString()}, {"pid", pid} };
                try
                {
                    AutomationElement element = AutomationElement.FromHandle(handle);
                    item["control_type"] = element.Current.ControlType.ProgrammaticName;
                    item["framework_id"] = element.Current.FrameworkId;
                    item["has_raw_child"] = TreeWalker.RawViewWalker.GetFirstChild(element) != null;
                }
                catch (Exception e) { item["error_code"] = e.GetType().Name; }
                item["msaa"] = InspectMsaa(handle, (int)pid);
                children.Add(item);
            }
            return true;
        }, IntPtr.Zero);
        return new Dictionary<string, object> { {"native_visible_child_class_counts", childClasses}, {"native_visible_children", children}, {"msaa_client", InspectMsaa(root.Handle, root.Pid)} };
    }

    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr parent, EnumWindowProc callback, IntPtr parameter);
    private static Dictionary<string, object> InspectMsaa(IntPtr handle, int pid)
    {
        Dictionary<string, object> result = new Dictionary<string, object>();
        try
        {
            if (!ValidPid(pid) || !IsWindowVisible(handle)) { result["status"] = "excluded"; return result; }
            Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
            object accessible;
            int hr = AccessibleObjectFromWindow(handle, unchecked((uint)-4), ref iid, out accessible);
            result["hresult"] = hr;
            IAccessible root = accessible as IAccessible;
            if (hr < 0 || root == null) { result["status"] = "unavailable"; return result; }
            Dictionary<string, int> roles = new Dictionary<string, int>();
            int visited = 0, errors = 0, excluded = 0;
            WalkMsaaStructure(root, 0, pid, roles, ref visited, ref errors, ref excluded, 0);
            result["status"] = "available";
            result["root_child_count"] = root.accChildCount;
            result["visited_nodes"] = visited;
            result["excluded_nodes"] = excluded;
            result["provider_errors"] = errors;
            result["role_counts"] = roles;
            result["truncated"] = visited >= MaxNodes;
        }
        catch (Exception e) { result["status"] = "provider_error"; result["error_code"] = e.GetType().Name; }
        return result;
    }

    private static void WalkMsaaStructure(IAccessible accessible, object childId, int pid, Dictionary<string, int> roles, ref int visited, ref int errors, ref int excluded, int depth)
    {
        if (visited >= MaxNodes || depth > 50 || !ValidPid(pid)) return;
        try
        {
            IntPtr owner;
            if (WindowFromAccessibleObject(accessible, out owner) < 0) { excluded++; return; }
            uint ownerPid;
            GetWindowThreadProcessId(owner, out ownerPid);
            if (ownerPid != pid || !ValidPid((int)ownerPid)) { excluded++; return; }
            visited++;
            object role = accessible.get_accRole(childId);
            Count(roles, role is int ? ((int)role).ToString() : "custom");
            object stateObject = accessible.get_accState(childId);
            int state = stateObject is int ? (int)stateObject : 0;
            if ((state & (0x20000000 | 0x8000 | 0x10000)) != 0) { excluded++; return; }
            if (!(childId is int) || (int)childId != 0) return;
            int count = Math.Min(accessible.accChildCount, Math.Min(200, MaxNodes - visited));
            if (count <= 0) return;
            object[] children = new object[count]; int obtained;
            if (AccessibleChildren(accessible, 0, count, children, out obtained) < 0) { errors++; return; }
            for (int index = 0; index < obtained; index++)
            {
                IAccessible child = children[index] as IAccessible;
                if (child != null) WalkMsaaStructure(child, 0, pid, roles, ref visited, ref errors, ref excluded, depth + 1);
                else if (children[index] is int) WalkMsaaStructure(accessible, children[index], pid, roles, ref visited, ref errors, ref excluded, depth + 1);
            }
        }
        catch (Exception) { errors++; }
    }

    private static HashSet<int> ValidPids()
    {
        HashSet<int> result = new HashSet<int>();
        foreach (Process process in Process.GetProcessesByName("DingTalk"))
        {
            try { if ((RequestedPid == 0 || RequestedPid == process.Id) && ValidPid(process.Id)) result.Add(process.Id); }
            finally { process.Dispose(); }
        }
        return result;
    }
    private static bool ValidPid(int pid)
    {
        if (RequestedPid != 0 && pid != RequestedPid) return false;
        return ImagePathMatches(pid);
    }
    private static bool ImagePathMatches(int pid)
    {
        if (pid <= 0) return false;
        IntPtr process = OpenProcess(0x1000, false, (uint)pid); // QUERY_LIMITED_INFORMATION only
        if (process == IntPtr.Zero) return false;
        try
        {
            StringBuilder filename = new StringBuilder(32768); uint length = (uint)filename.Capacity;
            return QueryFullProcessImageName(process, 0, filename, ref length) && String.Equals(Path.GetFullPath(filename.ToString()), ExpectedPath, StringComparison.OrdinalIgnoreCase);
        }
        finally { CloseHandle(process); }
    }
    private static bool ValidWindow(Window window)
    {
        uint pid;
        return IsWindow(window.Handle) && IsWindowVisible(window.Handle) && GetWindowThreadProcessId(window.Handle, out pid) != 0 && pid == window.Pid && ValidPid(window.Pid);
    }
    private static List<Window> VisibleWindows(HashSet<int> pids, out int hiddenWindows, out Dictionary<string, int> classes)
    {
        List<Window> result = new List<Window>();
        int hidden = 0;
        Dictionary<string, int> classCounts = new Dictionary<string, int>();
        EnumWindows(delegate(IntPtr handle, IntPtr parameter)
        {
            uint pid;
            GetWindowThreadProcessId(handle, out pid);
            if (pids.Contains((int)pid))
            {
                StringBuilder className = new StringBuilder(256);
                if (GetClassName(handle, className, className.Capacity) > 0)
                {
                    string name = className.ToString();
                    if (!classCounts.ContainsKey(name)) classCounts[name] = 0;
                    classCounts[name]++;
                }
                if (IsWindowVisible(handle)) result.Add(new Window { Handle = handle, Pid = (int)pid });
                else hidden++;
            }
            return true;
        }, IntPtr.Zero);
        hiddenWindows = hidden;
        classes = classCounts;
        return result;
    }
    private static string DesktopName()
    {
        StringBuilder name = new StringBuilder(256); int needed;
        IntPtr desktop = GetThreadDesktop(GetCurrentThreadId());
        return desktop != IntPtr.Zero && GetUserObjectInformation(desktop, 2, name, name.Capacity * 2, out needed) ? name.ToString() : "unknown";
    }

    private static void Rebind(List<Window> windows)
    {
        HashSet<long> present = new HashSet<long>();
        foreach (Window window in windows)
        {
            long key = window.Handle.ToInt64(); present.Add(key);
            if (Subscriptions.ContainsKey(key) || !ValidWindow(window)) continue;
            Subscription sub = new Subscription();
            try
            {
                sub.Element = AutomationElement.FromHandle(window.Handle);
                int pid = window.Pid;
                sub.Structure = delegate(object sender, StructureChangedEventArgs args) { OnEvent(sender, pid); };
                sub.Property = delegate(object sender, AutomationPropertyChangedEventArgs args) { OnEvent(sender, pid); };
                sub.Text = delegate(object sender, AutomationEventArgs args) { OnEvent(sender, pid); };
                Automation.AddStructureChangedEventHandler(sub.Element, TreeScope.Subtree, sub.Structure);
                Automation.AddAutomationPropertyChangedEventHandler(sub.Element, TreeScope.Subtree, sub.Property, AutomationElement.NameProperty, AutomationElement.IsOffscreenProperty);
                Automation.AddAutomationEventHandler(TextPattern.TextChangedEvent, sub.Element, TreeScope.Subtree, sub.Text);
                Subscriptions[key] = sub;
            }
            catch (Exception) { Unsubscribe(sub); }
        }
        List<long> removed = new List<long>();
        foreach (KeyValuePair<long, Subscription> item in Subscriptions) if (!present.Contains(item.Key)) { Unsubscribe(item.Value); removed.Add(item.Key); }
        foreach (long key in removed) Subscriptions.Remove(key);
    }
    private static void OnEvent(object sender, int pid)
    {
        try
        {
            AutomationElement element = sender as AutomationElement;
            if (element != null && ValidPid(pid) && element.Current.ProcessId == pid) { Interlocked.Increment(ref EventNotifications); Dirty.Set(); }
        }
        catch (Exception) { }
    }
    private static void Unsubscribe(Subscription sub)
    {
        if (sub.Element == null) return;
        try { if (sub.Structure != null) Automation.RemoveStructureChangedEventHandler(sub.Element, sub.Structure); } catch (Exception) { }
        try { if (sub.Property != null) Automation.RemoveAutomationPropertyChangedEventHandler(sub.Element, sub.Property); } catch (Exception) { }
        try { if (sub.Text != null) Automation.RemoveAutomationEventHandler(TextPattern.TextChangedEvent, sub.Element, sub.Text); } catch (Exception) { }
    }
    private static void UnsubscribeAll() { foreach (Subscription sub in Subscriptions.Values) Unsubscribe(sub); Subscriptions.Clear(); }
    private static void Emit(object data) { lock (OutputLock) { Console.WriteLine(Json.Serialize(data)); Console.Out.Flush(); } }
    private static string Stamp() { return DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'"); }
    private static Dictionary<string, string> Parse(string[] args)
    {
        Dictionary<string, string> options = new Dictionary<string, string>();
        HashSet<string> allowed = new HashSet<string>(new string[] {"mode", "process-path", "pid", "max-nodes", "max-texts", "interval", "duration", "stop-file", "diagnostics"});
        for (int i = 0; i < args.Length; i += 2)
        {
            if (!args[i].StartsWith("--") || i + 1 >= args.Length || !allowed.Contains(args[i].Substring(2))) throw new ArgumentException("invalid option");
            options[args[i].Substring(2)] = args[i + 1];
        }
        if (!options.ContainsKey("process-path")) throw new ArgumentException("process-path is required");
        return options;
    }
    private static string Get(Dictionary<string, string> options, string key, string fallback) { string value; return options.TryGetValue(key, out value) ? value : fallback; }
    private static int Integer(Dictionary<string, string> options, string key, int fallback, int min, int max)
    {
        int value; if (!Int32.TryParse(Get(options, key, fallback.ToString()), out value) || value < min || value > max) throw new ArgumentException("invalid integer"); return value;
    }
    private static double Number(Dictionary<string, string> options, string key, double fallback, double min, double max)
    {
        double value; if (!Double.TryParse(Get(options, key, fallback.ToString(System.Globalization.CultureInfo.InvariantCulture)), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out value) || Double.IsNaN(value) || Double.IsInfinity(value) || value < min || value > max) throw new ArgumentException("invalid number"); return value;
    }
}
