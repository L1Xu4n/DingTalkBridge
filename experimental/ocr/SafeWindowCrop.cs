using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Automation;

public sealed class OcrCropResult {
    public string Status = "capture_unavailable";
    public byte[] Png;
    public int Width;
    public int Height;
    public string ObservedAt;
}
public static class SafeWindowCrop {
    private const string ExpectedImage = @"D:\Program Files\DingDing\main\current_new\DingTalk.exe";
    private const string HostClass = "im_chat::DTIMChatComponent";
    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr value);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr value);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr value);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr value, uint flags);
    [DllImport("user32.dll")] private static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, StringBuilder value, int count);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out RECT value);
    [DllImport("user32.dll", SetLastError=true)] private static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    [DllImport("gdi32.dll")] private static extern bool SetViewportOrgEx(IntPtr dc, int x, int y, IntPtr old);
    [DllImport("gdi32.dll")] private static extern int IntersectClipRect(IntPtr dc, int left, int top, int right, int bottom);
    [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder value, ref uint size);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr value);

    public static bool EnterPhysicalPixels() {
        try { return SetThreadDpiAwarenessContext(new IntPtr(-4)) != IntPtr.Zero; }
        catch { return false; }
    }
    private static bool MatchesRect(IntPtr hwnd, int[] expected) {
        RECT r;
        return expected != null && expected.Length == 4 && GetWindowRect(hwnd, out r) &&
            r.Left == expected[0] && r.Top == expected[1] && r.Right == expected[2] && r.Bottom == expected[3];
    }
    private static bool ValidRect(int[] r) {
        return r != null && r.Length == 4 && r[2] > r[0] && r[3] > r[1] &&
            (long)r[2]-r[0] <= 16000 && (long)r[3]-r[1] <= 16000;
    }
    private static bool Contains(int[] outer, int[] inner) {
        return inner[0] >= outer[0] && inner[1] >= outer[1] && inner[2] <= outer[2] && inner[3] <= outer[3];
    }
    private static bool Overlaps(int[] a, int[] b) {
        return Math.Max(a[0], b[0]) < Math.Min(a[2], b[2]) && Math.Max(a[1], b[1]) < Math.Min(a[3], b[3]);
    }
    private static string ClassOf(IntPtr hwnd) {
        StringBuilder text = new StringBuilder(256);
        if (GetClassName(hwnd, text, text.Capacity) == 0) return "";
        return text.ToString();
    }
    private static bool ImageIsExpected(uint pid) {
        IntPtr process = OpenProcess(0x1000, false, pid);
        if (process == IntPtr.Zero) return false;
        try {
            uint count = 32768;
            StringBuilder path = new StringBuilder((int)count);
            return QueryFullProcessImageName(process, 0, path, ref count) &&
                String.Equals(Path.GetFullPath(path.ToString()), ExpectedImage, StringComparison.OrdinalIgnoreCase);
        } finally { CloseHandle(process); }
    }
    private static bool Verify(IntPtr host, IntPtr message, IntPtr common, uint pid, int[] hostRect, int[] messageRect) {
        uint hostPid, messagePid;
        GetWindowThreadProcessId(host, out hostPid);
        GetWindowThreadProcessId(message, out messagePid);
        // Do not query UIA on any handle until its PID and full image match.
        if (pid == 0 || hostPid != pid || messagePid != pid || !ImageIsExpected(pid) ||
            !IsWindow(host) || !IsWindow(message) || !IsWindowVisible(host) || !IsWindowVisible(message)) return false;
        string cls = ClassOf(message);
        bool related = IsChild(host,message);
        if (common != IntPtr.Zero) {
            uint commonPid;
            GetWindowThreadProcessId(common,out commonPid);
            related = commonPid == pid && ClassOf(common) == "DingChatWnd" &&
                IsWindowVisible(common) && IsChild(common,host) && IsChild(common,message);
        }
        AutomationElement.AutomationElementInformation semantic = AutomationElement.FromHandle(host).Current;
        bool verifiedSemantic = semantic.ProcessId == (int)pid && semantic.FrameworkId == "Qt" && semantic.ClassName == HostClass;
        return pid > 0 && hostPid == pid && messagePid == pid && ImageIsExpected(pid) &&
            IsWindow(host) && IsWindow(message) && IsWindowVisible(host) && IsWindowVisible(message) &&
            !IsIconic(GetAncestor(host, 2)) && related && verifiedSemantic &&
            (cls == "CefBrowserWindow" || cls == "Chrome_WidgetWin_1") &&
            MatchesRect(host, hostRect) && MatchesRect(message, messageRect);
    }
    public static bool ValidateIdentity(long host, long message, int pid, long common, int[] hostRect, int[] messageRect) {
        try { return EnterPhysicalPixels() && Verify(new IntPtr(host),new IntPtr(message),new IntPtr(common),(uint)pid,hostRect,messageRect); }
        catch { return false; }
    }
    public static OcrCropResult Capture(long hostHandle, long messageHandle, int expectedPid,
        long commonHandle, int[] hostRect, int[] messageRect, int[] cropRect, int[][] excludedRects, string stopFile) {
        OcrCropResult result = new OcrCropResult();
        try {
            if (!EnterPhysicalPixels()) { result.Status = "dpi_unavailable"; return result; }
            if (File.Exists(stopFile)) { result.Status = "stopped"; return result; }
            if (!ValidRect(hostRect) || !ValidRect(messageRect) || !ValidRect(cropRect) ||
                !Contains(hostRect,cropRect) || !Contains(messageRect,cropRect) ||
                excludedRects == null || excludedRects.Length == 0 || excludedRects.Length > 64) {
                result.Status = "unsafe_geometry"; return result;
            }
            foreach (int[] excluded in excludedRects) {
                if (!ValidRect(excluded) || Overlaps(cropRect, excluded)) { result.Status = "unsafe_geometry"; return result; }
            }
            int width = cropRect[2]-cropRect[0], height = cropRect[3]-cropRect[1];
            if (width < 100 || height < 40 || width > 4096 || height > 4096 || (long)width*height > 12000000) {
                result.Status = "unsafe_geometry"; return result;
            }
            IntPtr host = new IntPtr(hostHandle), message = new IntPtr(messageHandle), common = new IntPtr(commonHandle);
            if (!Verify(host,message,common,(uint)expectedPid,hostRect,messageRect)) { result.Status = "target_changed"; return result; }
            using (Bitmap bitmap = new Bitmap(width,height,PixelFormat.Format32bppArgb)) {
                bool captured;
                using (Graphics graphics = Graphics.FromImage(bitmap)) {
                    graphics.Clear(Color.White);
                    IntPtr dc = graphics.GetHdc();
                    try {
                        int left = cropRect[0]-messageRect[0], top = cropRect[1]-messageRect[1];
                        if (!SetViewportOrgEx(dc,-left,-top,IntPtr.Zero) ||
                            IntersectClipRect(dc,left,top,left+width,top+height) == 0) {
                            result.Status = "capture_unavailable"; return result;
                        }
                        // The DC bitmap is only the allowed crop; no full-window
                        // or desktop pixel buffer is ever created.
                        captured = PrintWindow(message,dc,0);
                    } finally { graphics.ReleaseHdc(dc); }
                }
                if (!captured || File.Exists(stopFile) || !Verify(host,message,common,(uint)expectedPid,hostRect,messageRect)) {
                    result.Status = File.Exists(stopFile) ? "stopped" : "capture_unavailable"; return result;
                }
                int minimum = 255, maximum = 0, varied = 0, samples = 0;
                for (int y=0;y<height;y+=Math.Max(1,height/80))
                    for (int x=0;x<width;x+=Math.Max(1,width/100)) {
                        Color c = bitmap.GetPixel(x,y);
                        int light = (c.R+c.G+c.B)/3;
                        minimum = Math.Min(minimum,light); maximum = Math.Max(maximum,light);
                        if (light > 12 && light < 243) varied++;
                        samples++;
                    }
                if (maximum-minimum < 12 || varied < 3) { result.Status = "blank_capture"; return result; }
                using (MemoryStream memory = new MemoryStream()) {
                    bitmap.Save(memory,ImageFormat.Png);
                    result.Png = memory.ToArray();
                }
            }
            result.Status = "available"; result.Width = width; result.Height = height;
            result.ObservedAt = DateTimeOffset.UtcNow.ToString("o");
            return result;
        } catch { result.Png = null; result.Status = "capture_unavailable"; return result; }
    }
}
